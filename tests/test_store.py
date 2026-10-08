from pathlib import Path
import tempfile
import unittest

from cnp2p.crypto import peer_record_signature
from cnp2p.identity import load_or_create_identity
from cnp2p.models import Peer
from cnp2p.store import ChatStore


class ChatStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        self.store = ChatStore(root / "history.sqlite3")
        identity = load_or_create_identity(root / "bob")
        self.bob = Peer(
            node_id=identity.node_id,
            host="127.0.0.1",
            port=9002,
            name="Bob",
            signing_key=identity.signing_public_key,
            encryption_key=identity.encryption_public_key,
            record_signature=peer_record_signature(identity, relay=False),
        )

    def tearDown(self) -> None:
        self.directory.cleanup()

    def add(self, message_id: str, direction: str, sent_at: float, status: str) -> None:
        self.store.add_message(
            message_id=message_id,
            peer_id=self.bob.node_id,
            direction=direction,
            kind="text",
            body=message_id,
            sent_at=sent_at,
            status=status,
        )

    def test_contacts_keep_latest_contact_time(self) -> None:
        self.store.upsert_contact(self.bob, last_seen=100.0)
        self.store.upsert_contact(self.bob, last_seen=0.0)
        self.assertEqual(self.store.get_contact(self.bob.node_id).last_seen, 100.0)
        self.assertTrue(self.store.set_nickname(self.bob.node_id, "  Bobby  "))
        self.assertEqual(self.store.contact(self.bob.node_id)["nickname"], "Bobby")
        self.assertIsNone(self.store.get_contact("ab" * 20))

    def test_status_only_moves_forward_unless_forced(self) -> None:
        self.add("m1", "outgoing", 1.0, "pending")
        self.assertEqual(self.store.set_status(["m1"], "read"), ["m1"])
        self.assertEqual(self.store.set_status(["m1"], "delivered"), [])
        self.assertEqual(self.store.get_message("m1")["status"], "read")
        self.assertEqual(self.store.set_status(["m1"], "failed", force=True), ["m1"])
        self.assertEqual(self.store.set_status(["m1"], "read", peer_id="cd" * 20), [])
        self.assertEqual(self.store.set_status(["m1"], "read", direction="incoming"), [])

    def test_history_unread_and_conversations(self) -> None:
        self.add("m1", "incoming", 1.0, "received")
        self.add("m2", "outgoing", 2.0, "delivered")
        self.add("m3", "incoming", 3.0, "received")
        self.assertFalse(
            self.store.add_message(
                message_id="m1",
                peer_id=self.bob.node_id,
                direction="incoming",
                kind="text",
                body="again",
                sent_at=4.0,
                status="received",
            )
        )

        self.assertEqual([m["message_id"] for m in self.store.history(self.bob.node_id)], ["m1", "m2", "m3"])
        self.assertEqual([m["message_id"] for m in self.store.history(self.bob.node_id, before=3.0, limit=1)], ["m2"])
        self.assertEqual(self.store.unread_total(), 2)
        conversation = self.store.list_conversations()[0]
        self.assertEqual(conversation["unread"], 2)
        self.assertEqual(conversation["last_message"]["message_id"], "m3")

        self.assertEqual(sorted(self.store.mark_read(self.bob.node_id)), ["m1", "m3"])
        self.assertEqual(self.store.unread_total(), 0)

        self.store.delete_conversation(self.bob.node_id)
        self.assertEqual(self.store.list_conversations(), [])

    def test_transfer_records_join_messages_without_keys(self) -> None:
        self.store.add_transfer(
            {
                "transfer_id": "t" * 32,
                "peer_id": self.bob.node_id,
                "message_id": "f1",
                "direction": "outgoing",
                "name": "photo.png",
                "size": 10,
                "mime": "image/png",
                "sha256": "0" * 64,
                "path": "/tmp/photo.png",
                "file_key": "secret",
                "chunk_size": 32768,
                "status": "waiting",
            }
        )
        self.store.add_message(
            message_id="f1",
            peer_id=self.bob.node_id,
            direction="outgoing",
            kind="file",
            body="photo.png",
            sent_at=1.0,
            status="pending",
            transfer_id="t" * 32,
        )
        message = self.store.get_message("f1")
        self.assertEqual(message["transfer"]["name"], "photo.png")
        self.assertNotIn("file_key", message["transfer"])
        self.assertEqual(len(self.store.pending_outgoing_transfers(self.bob.node_id)), 1)
        self.store.update_transfer("t" * 32, status="sending")
        self.store.interrupt_active_transfers()
        self.assertEqual(self.store.get_transfer("t" * 32)["status"], "waiting")


if __name__ == "__main__":
    unittest.main()
