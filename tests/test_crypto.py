import json
from pathlib import Path
import tempfile
import unittest

from cnp2p.crypto import CryptoError, open_message, peer_record_signature, seal_message
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


if __name__ == "__main__":
    unittest.main()
