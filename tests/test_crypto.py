import json
from pathlib import Path
import tempfile
import unittest

import os

from cnp2p.crypto import (
    CryptoError,
    open_chunk,
    open_message,
    open_payload,
    peer_record_signature,
    seal_chunk,
    seal_message,
    seal_payload,
)
from cnp2p.identity import load_or_create_identity
from cnp2p.models import Peer


def make_peer(identity, name: str, port: int) -> Peer:
    return Peer(
        node_id=identity.node_id,
        host="127.0.0.1",
        port=port,
        name=name,
        signing_key=identity.signing_public_key,
        encryption_key=identity.encryption_public_key,
        record_signature=peer_record_signature(identity, relay=False),
    )


class CryptoTests(unittest.TestCase):
    def test_envelope_is_confidential_and_authenticated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            alice_identity = load_or_create_identity(root / "alice")
            bob_identity = load_or_create_identity(root / "bob")
            alice = make_peer(alice_identity, "Alice", 9001)
            bob = make_peer(bob_identity, "Bob", 9002)

            envelope = seal_message(
                alice_identity,
                alice,
                bob,
                "message-1",
                "a secret sentence",
                sent_at=123.0,
            )
            sender, text, sent_at = open_message(bob_identity, envelope)

            self.assertNotIn("a secret sentence", json.dumps(envelope))
            self.assertEqual(sender.node_id, alice.node_id)
            self.assertEqual(text, "a secret sentence")
            self.assertEqual(sent_at, 123.0)

            tampered = dict(envelope)
            tampered["ciphertext"] = "A" + envelope["ciphertext"][1:]
            with self.assertRaises(CryptoError):
                open_message(bob_identity, tampered)

    def test_payload_kinds_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            alice_identity = load_or_create_identity(root / "alice")
            bob_identity = load_or_create_identity(root / "bob")
            alice = make_peer(alice_identity, "Alice", 9001)
            bob = make_peer(bob_identity, "Bob", 9002)

            receipt = {"kind": "receipt", "status": "read", "message_ids": ["a", "b"]}
            envelope = seal_payload(alice_identity, alice, bob, "receipt-1", receipt, sent_at=5.0)
            _, payload, sent_at = open_payload(bob_identity, envelope)
            self.assertEqual(payload, receipt)
            self.assertEqual(sent_at, 5.0)
            with self.assertRaises(CryptoError):
                open_message(bob_identity, envelope)

            # Text envelopes omit "kind" so that earlier versions can read them.
            legacy = seal_message(alice_identity, alice, bob, "legacy-1", "hi", sent_at=1.0)
            _, payload, _ = open_payload(bob_identity, legacy)
            self.assertEqual(payload, {"kind": "text", "text": "hi"})

            with self.assertRaises(CryptoError):
                seal_payload(alice_identity, alice, bob, "bad-1", {"kind": "unknown"})

    def test_file_chunks_are_bound_to_their_position(self) -> None:
        key = os.urandom(32)
        recipient = "ab" * 20
        nonce, ciphertext = seal_chunk(key, "transfer", 3, recipient, b"chunk data")
        self.assertEqual(open_chunk(key, "transfer", 3, recipient, nonce, ciphertext), b"chunk data")
        with self.assertRaises(CryptoError):
            open_chunk(key, "transfer", 4, recipient, nonce, ciphertext)
        with self.assertRaises(CryptoError):
            open_chunk(os.urandom(32), "transfer", 3, recipient, nonce, ciphertext)


if __name__ == "__main__":
    unittest.main()
