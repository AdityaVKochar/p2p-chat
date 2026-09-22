from __future__ import annotations

import asyncio
import json
import socket
from typing import Callable

from .models import Peer
from .protocol import PROTOCOL_VERSION


DISCOVERY_PORT = 37_020
MAX_DATAGRAM_BYTES = 2_048


class DiscoveryProtocol(asyncio.DatagramProtocol):
    def __init__(self, local_peer: Peer, on_peer: Callable[[Peer], None]) -> None:
        self.local_peer = local_peer
        self.on_peer = on_peer
        self.transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = transport  # type: ignore[assignment]
        sock = transport.get_extra_info("socket")
        if sock is not None:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        if len(data) > MAX_DATAGRAM_BYTES:
            return
        try:
            message = json.loads(data.decode("utf-8"))
            if message.get("version") != PROTOCOL_VERSION or message.get("type") != "DISCOVER":
                return
            peer = Peer.from_dict(message["peer"], observed_host=addr[0])
        except (KeyError, TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError):
            return
        if peer.node_id != self.local_peer.node_id:
            self.on_peer(peer)

    def announce(self) -> None:
        if self.transport is None:
            return
        payload = json.dumps(
            {
                "version": PROTOCOL_VERSION,
                "type": "DISCOVER",
                "peer": self.local_peer.to_dict(),
            },
            separators=(",", ":"),
        ).encode("utf-8")
        self.transport.sendto(payload, ("255.255.255.255", DISCOVERY_PORT))


async def start_discovery(
    local_peer: Peer,
    on_peer: Callable[[Peer], None],
) -> tuple[asyncio.DatagramTransport, DiscoveryProtocol]:
    loop = asyncio.get_running_loop()
    # Supplying the socket ourselves is portable to Windows, where asyncio's
    # reuse_port option is unavailable. SO_REUSEADDR also lets several local
    # demonstration nodes listen on the shared discovery port.
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.bind(("0.0.0.0", DISCOVERY_PORT))
        sock.setblocking(False)
        transport, protocol = await loop.create_datagram_endpoint(
            lambda: DiscoveryProtocol(local_peer, on_peer),
            sock=sock,
        )
    except BaseException:
        sock.close()
        raise
    return transport, protocol  # type: ignore[return-value]
