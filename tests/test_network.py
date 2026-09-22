import asyncio
from pathlib import Path
import tempfile
import unittest

from cnp2p.crypto import seal_message
from cnp2p.node import P2PNode
from cnp2p.protocol import request


class NetworkTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        root = Path(self.temporary_directory.name)
        self.root = root
        self.received: list[dict[str, object]] = []
        self.alice = P2PNode(
            name="Alice",
            port=0,
            advertise_host="127.0.0.1",
            data_dir=root / "alice",
            lan_discovery=False,
            on_message=self.received.append,
        )
        self.bob = P2PNode(
            name="Bob",
            port=0,
            advertise_host="127.0.0.1",
            data_dir=root / "bob",
            lan_discovery=False,
        )
        self.carol = P2PNode(
            name="Carol",
            port=0,
            advertise_host="127.0.0.1",
            data_dir=root / "carol",
            lan_discovery=False,
        )
        await asyncio.gather(self.alice.start(), self.bob.start(), self.carol.start())

    async def asyncTearDown(self) -> None:
        await asyncio.gather(self.alice.stop(), self.bob.stop(), self.carol.stop())
        self.temporary_directory.cleanup()

    async def test_three_nodes_discover_and_deliver_directly(self) -> None:
        await self.bob.bootstrap("127.0.0.1", self.alice.port)
        await self.carol.bootstrap("127.0.0.1", self.bob.port)

        found = await self.carol.find_node(self.alice.node_id)
        self.assertIsNotNone(found)
        receipt = await self.carol.send_message(self.alice.node_id, "hello across the network")

        self.assertEqual(len(self.received), 1)
        self.assertEqual(receipt.mode, "direct")
        self.assertEqual(self.received[0]["message_id"], receipt.message_id)
        self.assertEqual(self.received[0]["text"], "hello across the network")
        self.assertTrue(self.received[0]["encrypted"])

    async def test_duplicate_message_is_acknowledged_but_not_displayed_twice(self) -> None:
        await self.bob.bootstrap("127.0.0.1", self.alice.port)
        envelope = seal_message(
            self.bob.identity,
            self.bob.peer,
            self.alice.peer,
            "stable-test-message-id",
            "only once",
            sent_at=1_700_000_000.0,
        )
        payload = {"type": "CHAT", "envelope": envelope}

        first = await request("127.0.0.1", self.alice.port, payload)
        second = await request("127.0.0.1", self.alice.port, payload)

        self.assertEqual(first["type"], "ACK")
        self.assertFalse(first["duplicate"])
        self.assertTrue(second["duplicate"])
        self.assertEqual(len(self.received), 1)

    async def test_offline_message_survives_at_encrypted_relay(self) -> None:
        relay = P2PNode(
            name="Relay",
            port=0,
            advertise_host="127.0.0.1",
            data_dir=self.root / "relay",
            lan_discovery=False,
            relay_enabled=True,
        )
        await relay.start()
        try:
            await self.alice.bootstrap("127.0.0.1", relay.port)
            await self.alice.stop()
            self.bob.relay_endpoints.append(("127.0.0.1", relay.port))
            await self.bob.bootstrap("127.0.0.1", relay.port)

            receipt = await self.bob.send_message(self.alice.node_id, "read this after reconnecting", retries=0)

            self.assertEqual(receipt.mode, "relay-queued")
            self.assertEqual(relay.relay_store.count(self.alice.node_id), 1)  # type: ignore[union-attr]
            stored = relay.relay_store.fetch(self.alice.node_id)  # type: ignore[union-attr]
            self.assertNotIn("read this after reconnecting", str(stored))

            await relay.stop()
            relay = P2PNode(
                name="Relay",
                port=0,
                advertise_host="127.0.0.1",
                data_dir=self.root / "relay",
                lan_discovery=False,
                relay_enabled=True,
            )
            await relay.start()
            self.assertEqual(relay.relay_store.count(self.alice.node_id), 1)  # type: ignore[union-attr]

            self.alice = P2PNode(
                name="Alice",
                port=0,
                advertise_host="127.0.0.1",
                data_dir=self.root / "alice",
                lan_discovery=False,
                relay_endpoints=[("127.0.0.1", relay.port)],
                on_message=self.received.append,
            )
            await self.alice.start()
            fetched = await self.alice.fetch_offline_messages()

            self.assertEqual(fetched, 1)
            self.assertEqual(self.received[-1]["text"], "read this after reconnecting")
            self.assertTrue(self.received[-1]["offline"])
            self.assertEqual(relay.relay_store.count(self.alice.node_id), 0)  # type: ignore[union-attr]
        finally:
            await relay.stop()


if __name__ == "__main__":
    unittest.main()
