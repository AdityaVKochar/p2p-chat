from __future__ import annotations

import base64
from contextlib import suppress
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from .models import validate_node_id


IDENTITY_FILENAME = "identity.json"


def _b64encode(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


def _b64decode(value: str) -> bytes:
    return base64.b64decode(value.encode("ascii"), validate=True)


def node_id_from_signing_key(public_key: bytes) -> str:
    return hashlib.sha256(public_key).hexdigest()[:40]


@dataclass(frozen=True, slots=True)
class NodeIdentity:
    signing_private_key: Ed25519PrivateKey
    encryption_private_key: X25519PrivateKey

    @property
    def signing_public_bytes(self) -> bytes:
        return self.signing_private_key.public_key().public_bytes(
            serialization.Encoding.Raw,
            serialization.PublicFormat.Raw,
        )

    @property
    def encryption_public_bytes(self) -> bytes:
        return self.encryption_private_key.public_key().public_bytes(
            serialization.Encoding.Raw,
            serialization.PublicFormat.Raw,
        )

    @property
    def signing_public_key(self) -> str:
        return _b64encode(self.signing_public_bytes)

    @property
    def encryption_public_key(self) -> str:
        return _b64encode(self.encryption_public_bytes)

    @property
    def node_id(self) -> str:
        return node_id_from_signing_key(self.signing_public_bytes)

    def sign(self, data: bytes) -> str:
        return _b64encode(self.signing_private_key.sign(data))

    def to_dict(self) -> dict[str, str | int]:
        return {
            "version": 2,
            "node_id": self.node_id,
            "signing_private_key": _b64encode(
                self.signing_private_key.private_bytes(
                    serialization.Encoding.Raw,
                    serialization.PrivateFormat.Raw,
                    serialization.NoEncryption(),
                )
            ),
            "encryption_private_key": _b64encode(
                self.encryption_private_key.private_bytes(
                    serialization.Encoding.Raw,
                    serialization.PrivateFormat.Raw,
                    serialization.NoEncryption(),
                )
            ),
        }

    @classmethod
    def generate(cls) -> "NodeIdentity":
        return cls(Ed25519PrivateKey.generate(), X25519PrivateKey.generate())

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> "NodeIdentity":
        identity = cls(
            Ed25519PrivateKey.from_private_bytes(_b64decode(str(payload["signing_private_key"]))),
            X25519PrivateKey.from_private_bytes(_b64decode(str(payload["encryption_private_key"]))),
        )
        stored_id = validate_node_id(str(payload["node_id"]))
        if stored_id != identity.node_id:
            raise ValueError("stored node ID does not match the signing key")
        return identity


def _write_identity(identity_path: Path, identity: NodeIdentity) -> None:
    temporary_path = identity_path.with_suffix(".tmp")
    temporary_path.write_text(json.dumps(identity.to_dict(), indent=2) + "\n", encoding="utf-8")
    with suppress(OSError):
        os.chmod(temporary_path, 0o600)
    os.replace(temporary_path, identity_path)


def load_or_create_identity(data_dir: Path) -> NodeIdentity:
    """Return a persistent authenticated identity stored in *data_dir*."""
    data_dir.mkdir(parents=True, exist_ok=True)
    identity_path = data_dir / IDENTITY_FILENAME

    if identity_path.exists():
        try:
            payload = json.loads(identity_path.read_text(encoding="utf-8"))
            if "signing_private_key" in payload and "encryption_private_key" in payload:
                return NodeIdentity.from_dict(payload)
            # Version 1 identities contained only a random ID and could not
            # authenticate it. Upgrade to a key-bound identity.
            identity = NodeIdentity.generate()
            _write_identity(identity_path, identity)
            return identity
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as error:
            raise RuntimeError(f"invalid identity file: {identity_path}") from error

    identity = NodeIdentity.generate()
    try:
        with identity_path.open("x", encoding="utf-8") as handle:
            json.dump(identity.to_dict(), handle, indent=2)
            handle.write("\n")
        with suppress(OSError):
            os.chmod(identity_path, 0o600)
    except FileExistsError:
        return load_or_create_identity(data_dir)
    return identity
