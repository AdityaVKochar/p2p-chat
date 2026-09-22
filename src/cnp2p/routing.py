from __future__ import annotations

from collections import deque
import threading
import time

from .models import ID_BITS, Peer, validate_node_id


class RoutingTable:
    """A bounded Kademlia-inspired table partitioned by XOR distance."""

    def __init__(self, local_id: str, bucket_size: int = 20) -> None:
        self.local_id = validate_node_id(local_id)
        self.bucket_size = bucket_size
        self._buckets: list[deque[Peer]] = [deque() for _ in range(ID_BITS)]
        self._lock = threading.RLock()

    def _bucket_index(self, node_id: str) -> int | None:
        distance = int(self.local_id, 16) ^ int(validate_node_id(node_id), 16)
        return distance.bit_length() - 1 if distance else None

    def add(self, peer: Peer) -> bool:
        index = self._bucket_index(peer.node_id)
        if index is None:
            return False
        peer.last_seen = time.time()
        with self._lock:
            bucket = self._buckets[index]
            for existing in tuple(bucket):
                if existing.node_id == peer.node_id:
                    bucket.remove(existing)
                    bucket.append(peer)
                    return True
            if len(bucket) >= self.bucket_size:
                bucket.popleft()
            bucket.append(peer)
        return True

    def remove(self, node_id: str) -> bool:
        index = self._bucket_index(node_id)
        if index is None:
            return False
        with self._lock:
            bucket = self._buckets[index]
            for peer in tuple(bucket):
                if peer.node_id == node_id:
                    bucket.remove(peer)
                    return True
        return False

    def get(self, node_id: str) -> Peer | None:
        index = self._bucket_index(node_id)
        if index is None:
            return None
        with self._lock:
            return next((peer for peer in self._buckets[index] if peer.node_id == node_id), None)

    def find_prefix(self, prefix: str) -> list[Peer]:
        prefix = prefix.lower()
        return [peer for peer in self.all_peers() if peer.node_id.startswith(prefix)]

    def all_peers(self) -> list[Peer]:
        with self._lock:
            return [peer for bucket in self._buckets for peer in bucket]

    def closest(self, target_id: str, count: int = 20) -> list[Peer]:
        target = int(validate_node_id(target_id), 16)
        return sorted(
            self.all_peers(),
            key=lambda peer: int(peer.node_id, 16) ^ target,
        )[:count]

    def purge_older_than(self, maximum_age: float) -> int:
        cutoff = time.time() - maximum_age
        removed = 0
        with self._lock:
            for bucket in self._buckets:
                stale = [peer for peer in bucket if peer.last_seen < cutoff]
                for peer in stale:
                    bucket.remove(peer)
                    removed += 1
        return removed

    def __len__(self) -> int:
        return sum(len(bucket) for bucket in self._buckets)

