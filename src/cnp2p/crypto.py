from __future__ import annotations

import base64
import json
import os
import time
from typing import Any

from cryptography.exceptions import InvalidSignature, InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from .identity import NodeIdentity
from .models import Peer, validate_node_id


class CryptoError(ValueError):
    pass


def _b64encode(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


def _b64decode(value: str, expected_length: int | None = None) -> bytes:
    try:
        decoded = base64.b64decode(value.encode("ascii"), validate=True)
    except (UnicodeEncodeError, ValueError) as error:
        raise CryptoError("invalid base64 cryptographic value") from error
    if expected_length is not None and len(decoded) != expected_length:
        raise CryptoError("invalid cryptographic value length")
    return decoded


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def peer_record_signature(identity: NodeIdentity, *, relay: bool) -> str:
    return identity.sign(
        canonical_json(
            {
                "encryption_key": identity.encryption_public_key,
                "node_id": identity.node_id,
                "relay": relay,
                "signing_key": identity.signing_public_key,
            }
        )
    )


def _associated_data(sender_id: str, recipient_id: str, message_id: str) -> bytes:
    return canonical_json(
        {
            "message_id": message_id,
            "recipient_id": validate_node_id(recipient_id),
            "sender_id": validate_node_id(sender_id),
            "version": 1,
        }
    )


def _derive_key(shared_secret: bytes, message_id: str, sender_id: str, recipient_id: str) -> bytes:
    digest = hashes.Hash(hashes.SHA256())
    digest.update(message_id.encode("ascii"))
    salt = digest.finalize()
    return HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        info=b"cnp2p-e2e-v1\0" + sender_id.encode("ascii") + recipient_id.encode("ascii"),
    ).derive(shared_secret)


def envelope_signature_data(envelope: dict[str, Any]) -> bytes:
    signed_fields = {
        "ciphertext": envelope["ciphertext"],
        "message_id": envelope["message_id"],
        "nonce": envelope["nonce"],
        "recipient_id": envelope["recipient_id"],
        "sender": envelope["sender"],
        "version": envelope.get("version", 1),
    }
    return canonical_json(signed_fields)


def verify_envelope_signature(envelope: dict[str, Any]) -> Peer:
    try:
        sender = Peer.from_dict(envelope["sender"])
        if envelope.get("version") != 1:
            raise CryptoError("unsupported encrypted-envelope version")
        validate_node_id(str(envelope["recipient_id"]))
        message_id = str(envelope["message_id"])
        if not message_id or len(message_id) > 128:
            raise CryptoError("invalid message ID")
        public_key = Ed25519PublicKey.from_public_bytes(_b64decode(sender.signing_key, 32))
        public_key.verify(
            _b64decode(str(envelope["signature"]), 64),
            envelope_signature_data(envelope),
        )
        return sender
    except (KeyError, TypeError, ValueError, InvalidSignature) as error:
        if isinstance(error, CryptoError):
            raise
        raise CryptoError("encrypted envelope signature is invalid") from error


def seal_message(
    identity: NodeIdentity,
    sender: Peer,
    recipient: Peer,
    message_id: str,
    text: str,
    *,
    sent_at: float | None = None,
) -> dict[str, Any]:
    if not recipient.is_authenticated:
        raise CryptoError("recipient does not have an authenticated encryption key")
    timestamp = time.time() if sent_at is None else sent_at
    recipient_public_key = X25519PublicKey.from_public_bytes(_b64decode(recipient.encryption_key, 32))
    shared_secret = identity.encryption_private_key.exchange(recipient_public_key)
    key = _derive_key(shared_secret, message_id, sender.node_id, recipient.node_id)
    nonce = os.urandom(12)
    aad = _associated_data(sender.node_id, recipient.node_id, message_id)
    plaintext = canonical_json({"sent_at": timestamp, "text": text})
    ciphertext = ChaCha20Poly1305(key).encrypt(nonce, plaintext, aad)
    envelope: dict[str, Any] = {
        "version": 1,
        "message_id": message_id,
        "sender": sender.to_dict(),
        "recipient_id": recipient.node_id,
        "nonce": _b64encode(nonce),
        "ciphertext": _b64encode(ciphertext),
    }
    envelope["signature"] = identity.sign(envelope_signature_data(envelope))
    return envelope


def open_message(identity: NodeIdentity, envelope: dict[str, Any]) -> tuple[Peer, str, float]:
    sender = verify_envelope_signature(envelope)
    recipient_id = validate_node_id(str(envelope["recipient_id"]))
    if recipient_id != identity.node_id:
        raise CryptoError("encrypted message is addressed to another node")
    message_id = str(envelope["message_id"])
    sender_public_key = X25519PublicKey.from_public_bytes(_b64decode(sender.encryption_key, 32))
    shared_secret = identity.encryption_private_key.exchange(sender_public_key)
    key = _derive_key(shared_secret, message_id, sender.node_id, recipient_id)
    aad = _associated_data(sender.node_id, recipient_id, message_id)
    try:
        plaintext = ChaCha20Poly1305(key).decrypt(
            _b64decode(str(envelope["nonce"]), 12),
            _b64decode(str(envelope["ciphertext"])),
            aad,
        )
        payload = json.loads(plaintext.decode("utf-8"))
        text = payload["text"]
        sent_at = float(payload["sent_at"])
        if not isinstance(text, str):
            raise CryptoError("decrypted message text is invalid")
        return sender, text, sent_at
    except (KeyError, TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError, InvalidTag) as error:
        if isinstance(error, CryptoError):
            raise
        raise CryptoError("encrypted message could not be authenticated or decrypted") from error


def signed_relay_action(
    identity: NodeIdentity,
    action: str,
    *,
    message_ids: list[str] | None = None,
    timestamp: float | None = None,
    nonce: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "action": action,
        "node_id": identity.node_id,
        "nonce": nonce or _b64encode(os.urandom(16)),
        "timestamp": time.time() if timestamp is None else timestamp,
    }
    if message_ids is not None:
        payload["message_ids"] = message_ids
    payload["signature"] = identity.sign(canonical_json(payload))
    return payload


def verify_relay_action(peer: Peer, payload: dict[str, Any], expected_action: str) -> None:
    try:
        signature = _b64decode(str(payload["signature"]), 64)
        unsigned = dict(payload)
        del unsigned["signature"]
        if unsigned.get("action") != expected_action or unsigned.get("node_id") != peer.node_id:
            raise CryptoError("relay authorization does not match the requesting peer")
        timestamp = float(unsigned["timestamp"])
        if abs(time.time() - timestamp) > 300:
            raise CryptoError("relay authorization has expired")
        Ed25519PublicKey.from_public_bytes(_b64decode(peer.signing_key, 32)).verify(
            signature,
            canonical_json(unsigned),
        )
    except (KeyError, TypeError, ValueError, InvalidSignature) as error:
        if isinstance(error, CryptoError):
            raise
        raise CryptoError("relay authorization signature is invalid") from error
