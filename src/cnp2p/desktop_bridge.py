from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sys
import threading
from typing import Any

from .models import Peer
from .nat import discover_public_endpoint
from .node import P2PNode


def parse_address(value: str) -> tuple[str, int]:
    value = value.strip()
    if not value:
        raise ValueError("address is empty")
    host, separator, raw_port = value.rpartition(":")
    if not separator or not host:
        raise ValueError("address must have the form HOST:PORT")
    port = int(raw_port)
    if not 1 <= port <= 65_535:
        raise ValueError("port must be between 1 and 65535")
    return host, port


class DesktopBridge:
    def __init__(self) -> None:
        self.node: P2PNode | None = None
        self.stun_server: tuple[str, int] | None = None
        self._write_lock = threading.Lock()

    def emit(self, event: str, data: Any) -> None:
        self._write({"kind": "event", "event": event, "data": data})

    def _write(self, message: dict[str, Any]) -> None:
        encoded = json.dumps(message, separators=(",", ":"), ensure_ascii=False)
        with self._write_lock:
            sys.stdout.write(encoded + "\n")
            sys.stdout.flush()

    def _peer_data(self, peer: Peer) -> dict[str, Any]:
        return {
            "node_id": peer.node_id,
            "short_id": peer.short_id,
            "name": peer.name,
            "host": peer.host,
            "port": peer.port,
            "last_seen": peer.last_seen,
            "relay": peer.relay,
        }

    def _snapshot(self) -> dict[str, Any]:
        if self.node is None:
            return {"running": False, "peers": []}
        return {
            "running": True,
            "self": self._peer_data(self.node.peer),
            "peers": [
                self._peer_data(peer)
                for peer in sorted(
                    self.node.routing.all_peers(),
                    key=lambda item: (item.name.lower(), item.node_id),
                )
            ],
            "relay_server": self.node.relay_enabled,
            "upnp": self.node._port_mapping is not None,
        }

    async def start(self, payload: dict[str, Any]) -> dict[str, Any]:
        await self.stop({})
        name = str(payload.get("name", "")).strip()
        if not name:
            raise ValueError("display name is required")
        port = int(payload.get("port", 9000))
        relay_endpoints = [
            parse_address(str(address))
            for address in payload.get("relays", [])
            if str(address).strip()
        ]
        self.stun_server = (
            parse_address(str(payload["stun"])) if str(payload.get("stun", "")).strip() else None
        )

        def on_message(message: dict[str, Any]) -> None:
            sender = message["sender"]
            assert isinstance(sender, Peer)
            self.emit(
                "message",
                {
                    "message_id": message["message_id"],
                    "sender": self._peer_data(sender),
                    "text": message["text"],
                    "sent_at": message["sent_at"],
                    "offline": bool(message.get("offline")),
                    "encrypted": True,
                },
            )

        def on_peer(peer: Peer) -> None:
            self.emit("peer", self._peer_data(peer))

        def on_status(status: dict[str, Any]) -> None:
            self.emit("status", status)

        self.node = P2PNode(
            name=name,
            host=str(payload.get("host", "0.0.0.0")),
            port=port,
            advertise_host=str(payload.get("advertise_host", "")).strip() or None,
            data_dir=Path(str(payload["data_dir"])),
            lan_discovery=bool(payload.get("lan_discovery", True)),
            relay_enabled=bool(payload.get("relay_server", False)),
            relay_endpoints=relay_endpoints,
            upnp=bool(payload.get("upnp", True)),
            on_message=on_message,
            on_peer=on_peer,
            on_status=on_status,
        )
        await self.node.start()
        self.emit("status", {"category": "node", "state": "online", "detail": "Node started"})

        for value in payload.get("bootstrap", []):
            if not str(value).strip():
                continue
            try:
                host, bootstrap_port = parse_address(str(value))
                peer = await self.node.bootstrap(host, bootstrap_port)
                self.emit("peer", self._peer_data(peer))
            except Exception as error:
                self.emit(
                    "status",
                    {
                        "category": "bootstrap",
                        "state": "warning",
                        "detail": f"Could not connect to {value}: {error}",
                    },
                )
        return self._snapshot()

    async def stop(self, payload: dict[str, Any]) -> dict[str, Any]:
        del payload
        if self.node is not None:
            await self.node.stop()
            self.node = None
        return {"running": False}

    def _require_node(self) -> P2PNode:
        if self.node is None:
            raise RuntimeError("the node is not running")
        return self.node

    async def snapshot(self, payload: dict[str, Any]) -> dict[str, Any]:
        del payload
        return self._snapshot()

    async def connect(self, payload: dict[str, Any]) -> dict[str, Any]:
        node = self._require_node()
        host, port = parse_address(str(payload["address"]))
        peer = await node.bootstrap(host, port)
        return {"peer": self._peer_data(peer), "snapshot": self._snapshot()}

    async def send(self, payload: dict[str, Any]) -> dict[str, Any]:
        node = self._require_node()
        text = str(payload.get("text", ""))
        receipt = await node.send_message(str(payload["node_id"]), text)
        return {
            "message_id": receipt.message_id,
            "mode": receipt.mode,
            "relay": receipt.relay,
        }

    async def ping(self, payload: dict[str, Any]) -> dict[str, Any]:
        node = self._require_node()
        peer = await node.resolve_peer(str(payload["node_id"]))
        milliseconds = await node.ping(peer)
        return {"milliseconds": milliseconds}

    async def offline(self, payload: dict[str, Any]) -> dict[str, Any]:
        del payload
        count = await self._require_node().fetch_offline_messages()
        return {"count": count}

    async def nat(self, payload: dict[str, Any]) -> dict[str, Any]:
        del payload
        if self.stun_server is None:
            raise ValueError("configure a STUN server in Settings first")
        endpoint = await discover_public_endpoint(*self.stun_server)
        return {"host": endpoint.host, "port": endpoint.port}

    async def dispatch(self, request: dict[str, Any]) -> None:
        request_id = request.get("id")
        action = str(request.get("action", ""))
        payload = request.get("payload") or {}
        try:
            if action.startswith("_") or action not in {
                "start",
                "stop",
                "snapshot",
                "connect",
                "send",
                "ping",
                "offline",
                "nat",
            }:
                raise ValueError("unsupported desktop action")
            handler = getattr(self, action)
            result = await handler(payload)
            self._write({"kind": "response", "id": request_id, "ok": True, "data": result})
        except Exception as error:
            self._write(
                {
                    "kind": "response",
                    "id": request_id,
                    "ok": False,
                    "error": str(error) or error.__class__.__name__,
                }
            )


async def run() -> None:
    bridge = DesktopBridge()
    try:
        while True:
            line = await asyncio.to_thread(sys.stdin.readline)
            if not line:
                break
            try:
                request = json.loads(line)
                if not isinstance(request, dict):
                    raise ValueError("bridge request must be a JSON object")
            except (json.JSONDecodeError, ValueError) as error:
                bridge._write({"kind": "response", "id": None, "ok": False, "error": str(error)})
                continue
            await bridge.dispatch(request)
    finally:
        await bridge.stop({})


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
