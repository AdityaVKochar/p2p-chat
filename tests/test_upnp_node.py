from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, Mock, patch

from cnp2p.node import P2PNode


class UpnpNodeTests(unittest.IsolatedAsyncioTestCase):
    async def test_advertises_and_cleans_up_automatic_mapping(self) -> None:
        mapping = Mock()
        mapping.external_host = "203.0.113.20"
        mapping.external_port = 45_000
        mapping.lease_duration = 0
        mapping.created_at = time.time()

        with tempfile.TemporaryDirectory() as directory:
            node = P2PNode(
                name="Mapped",
                port=0,
                data_dir=Path(directory),
                lan_discovery=False,
                upnp=True,
            )
            with patch(
                "cnp2p.node.create_upnp_tcp_mapping",
                new=AsyncMock(return_value=mapping),
            ):
                await node.start()
                self.assertEqual(node.peer.host, "203.0.113.20")
                self.assertEqual(node.peer.port, 45_000)
                await node.stop()

        mapping.delete.assert_called_once()


if __name__ == "__main__":
    unittest.main()
