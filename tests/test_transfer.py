import asyncio
import hashlib
import os
from pathlib import Path
import tempfile
import time
import unittest

from cnp2p.node import P2PNode
from cnp2p.protocol import request
from cnp2p.transfer import CHUNK_SIZE, sanitize_filename


def make_node(root: Path, name: str, **options) -> P2PNode:
    return P2PNode(
        name=name,
        port=0,
        advertise_host="127.0.0.1",
        data_dir=root / name.lower(),
        lan_discovery=False,
        **options,
    )


async def wait_for(predicate, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition was not met in time")
        await asyncio.sleep(0.02)


class TransferTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.received: list[dict] = []
        self.alice = make_node(self.root, "Alice", on_message=self.received.append)
        self.bob = make_node(self.root, "Bob")
        await asyncio.gather(self.alice.start(), self.bob.start())
        await self.bob.bootstrap("127.0.0.1", self.alice.port)

        self.payload = os.urandom(300_000)
        self.source = self.root / "holiday photo.png"
        self.source.write_bytes(self.payload)

    async def asyncTearDown(self) -> None:
        await asyncio.gather(self.alice.stop(), self.bob.stop())
        self.temporary_directory.cleanup()

    def transfer(self, node: P2PNode, transfer_id: str) -> dict:
        record = node.store.get_transfer(transfer_id)
        assert record is not None
        return record

    async def test_file_arrives_intact(self) -> None:
        message = await self.bob.send_file(self.alice.node_id, self.source)
        transfer_id = message["transfer"]["transfer_id"]
        await wait_for(lambda: self.transfer(self.bob, transfer_id)["status"] == "done")

        incoming = self.transfer(self.alice, transfer_id)
        self.assertEqual(incoming["status"], "done")
        self.assertEqual(Path(incoming["path"]).read_bytes(), self.payload)
        self.assertEqual(Path(incoming["path"]).name, "holiday photo.png")
        self.assertEqual(incoming["mime"], "image/png")
        self.assertEqual(self.bob.store.get_message(message["message_id"])["status"], "delivered")
        self.assertEqual(self.received[-1]["kind"], "file")
        self.assertEqual(self.received[-1]["text"], "holiday photo.png")

    async def test_tampered_chunk_is_rejected(self) -> None:
        self.bob.transfers.schedule = lambda transfer_id: None  # type: ignore[method-assign]
        message = await self.bob.send_file(self.alice.node_id, self.source)
        record = self.transfer(self.bob, message["transfer"]["transfer_id"])
        # Deliver only the offer so Alice knows the transfer.
        self.assertFalse(await self._offer_only(record))

        response = await request(
            "127.0.0.1",
            self.alice.port,
            {
                "type": "FILE_CHUNK",
                "transfer_id": record["transfer_id"],
                "index": 0,
                "nonce": "AAAAAAAAAAAAAAAA",
                "data": "AAAA" * 20,
            },
        )
        self.assertEqual(response["type"], "ERROR")
        self.assertIn("authenticated", response["error"])

    async def _offer_only(self, record: dict) -> list[int]:
        from cnp2p.crypto import seal_payload
        import math

        envelope = seal_payload(
            self.bob.identity,
            self.bob.peer,
            self.alice.peer,
            record["message_id"],
            {
                "kind": "file",
                "transfer_id": record["transfer_id"],
                "name": record["name"],
                "size": record["size"],
                "mime": record["mime"],
                "sha256": record["sha256"],
                "chunk_size": record["chunk_size"],
                "chunks": math.ceil(record["size"] / record["chunk_size"]),
                "key": record["file_key"],
            },
        )
        response = await request("127.0.0.1", self.alice.port, {"type": "FILE_OFFER", "envelope": envelope})
        self.assertEqual(response["type"], "FILE_ACCEPT")
        return response["have"]

    async def test_resume_sends_only_missing_chunks(self) -> None:
        sent_indices: list[int] = []
        original = self.bob.transfers._send_chunks

        async def recording(record, peer, key, chunk_size, chunks, indices):
            sent_indices.extend(indices)
            return await original(record, peer, key, chunk_size, chunks, indices)

        self.bob.transfers._send_chunks = recording  # type: ignore[method-assign]
        self.bob.transfers.schedule = lambda transfer_id: None  # type: ignore[method-assign]
        message = await self.bob.send_file(self.alice.node_id, self.source)
        record = self.transfer(self.bob, message["transfer"]["transfer_id"])

        # Simulate an interrupted transfer: Alice already holds the first three chunks.
        await self._offer_only(record)
        import base64

        key = base64.b64decode(record["file_key"])
        await original(record, self.alice.peer, key, CHUNK_SIZE, 10, [0, 1, 2])
        self.assertEqual(await self._offer_only(record), [0, 1, 2])

        del self.bob.transfers.schedule
        self.bob.transfers.schedule(record["transfer_id"])
        await wait_for(lambda: self.transfer(self.bob, record["transfer_id"])["status"] == "done")
        self.assertEqual(sorted(sent_indices), list(range(3, 10)))
        self.assertEqual(
            hashlib.sha256(Path(self.transfer(self.alice, record["transfer_id"])["path"]).read_bytes()).hexdigest(),
            hashlib.sha256(self.payload).hexdigest(),
        )

    async def test_offline_recipient_receives_file_when_it_returns(self) -> None:
        await self.alice.stop()
        message = await self.bob.send_file(self.alice.node_id, self.source)
        transfer_id = message["transfer"]["transfer_id"]
        await wait_for(lambda: self.bob.transfers._sending.get(transfer_id) is None)
        self.assertEqual(self.transfer(self.bob, transfer_id)["status"], "waiting")
        self.assertEqual(self.bob.store.get_message(message["message_id"])["status"], "pending")

        # Alice comes back on a new port and says hello; Bob retries automatically.
        self.alice = make_node(self.root, "Alice", on_message=self.received.append)
        await self.alice.start()
        await self.alice.bootstrap("127.0.0.1", self.bob.port)
        await wait_for(lambda: self.transfer(self.bob, transfer_id)["status"] == "done")
        self.assertEqual(Path(self.transfer(self.alice, transfer_id)["path"]).read_bytes(), self.payload)

    async def test_oversized_offer_is_rejected_without_relay(self) -> None:
        self.alice.transfers.max_file_bytes = 100_000
        message = await self.bob.send_file(self.alice.node_id, self.source)
        transfer_id = message["transfer"]["transfer_id"]
        await wait_for(lambda: self.transfer(self.bob, transfer_id)["status"] == "failed")
        self.assertIn("up to 0 MB", self.transfer(self.bob, transfer_id)["error"])
        self.assertEqual(self.bob.store.get_message(message["message_id"])["status"], "failed")
        self.assertIsNone(self.alice.store.get_transfer(transfer_id))

    async def test_recipient_cancel_stops_sender(self) -> None:
        self.bob.transfers.schedule = lambda transfer_id: None  # type: ignore[method-assign]
        message = await self.bob.send_file(self.alice.node_id, self.source)
        record = self.transfer(self.bob, message["transfer"]["transfer_id"])
        await self._offer_only(record)
        await self.alice.transfers.cancel(record["transfer_id"])

        del self.bob.transfers.schedule
        self.bob.transfers.schedule(record["transfer_id"])
        await wait_for(lambda: self.transfer(self.bob, record["transfer_id"])["status"] == "failed")
        self.assertIn("cancelled", self.transfer(self.bob, record["transfer_id"])["error"])

    def test_filenames_are_sanitized(self) -> None:
        self.assertEqual(sanitize_filename("../../etc/passwd"), "passwd")
        self.assertEqual(sanitize_filename("..\\..\\boot.ini"), "boot.ini")
        self.assertEqual(sanitize_filename("CON.txt"), "_CON.txt")
        self.assertEqual(sanitize_filename("a<b>:c?.png"), "a_b__c_.png")
        self.assertEqual(sanitize_filename("..."), "file")


if __name__ == "__main__":
    unittest.main()
