import unittest

from cnp2p.models import Peer
from cnp2p.routing import RoutingTable


def node_id(number: int) -> str:
    return f"{number:040x}"


class RoutingTableTests(unittest.TestCase):
    def test_returns_peers_by_xor_distance(self) -> None:
        table = RoutingTable(node_id(0))
        for number in (16, 2, 8, 4):
            table.add(Peer(node_id(number), "127.0.0.1", 9000 + number))

        closest = table.closest(node_id(3), count=3)

        self.assertEqual([int(peer.node_id, 16) for peer in closest], [2, 4, 8])

    def test_refreshes_existing_peer_record(self) -> None:
        table = RoutingTable(node_id(1))
        table.add(Peer(node_id(2), "127.0.0.1", 9001, "old"))
        table.add(Peer(node_id(2), "127.0.0.1", 9002, "new"))

        self.assertEqual(len(table), 1)
        self.assertEqual(table.get(node_id(2)).port, 9002)  # type: ignore[union-attr]


if __name__ == "__main__":
    unittest.main()

