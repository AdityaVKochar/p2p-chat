from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sys
import threading
from typing import Any

from .crypto import safety_number
from .models import ID_HEX_LENGTH, Peer, validate_node_id
from .nat import discover_public_endpoint
from .node import NodeError, P2PNode


ACTIONS = frozenset(
    {
        "start",
        "stop",
        "snapshot",
        "connect",
        "send",
        "ping",
        "offline",
        "nat",
        "conversations",
        "history",
        "send_file",
        "cancel_transfer",
        "retry",
        "mark_read",
        "rename_contact",
        "set_verified",
        "delete_conversation",
        "contact_info",
    }
)


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


def parse_invite(value: str) -> tuple[str | None, tuple[str, int]]:
    """Accept ``cnp2p:<node_id>@host:port`` invites as well as plain ``host:port``."""
    value = value.strip()
    if value.lower().startswith("cnp2p:"):
        node_id, separator, address = value[6:].partition("@")
        if not separator:
            raise ValueError("invite codes have the form cnp2p:<node id>@host:port")
        return validate_node_id(node_id.strip()), parse_address(address)
    return None, parse_address(value)


class DesktopBridge:
    def __init__(self) -> None:
        self.node: P2PNode | None = None
        self.stun_server: tuple[str, int] | None = None
        self.read_receipts = True
        self._write_lock = threading.Lock()

    def emit(self, event: str, data: Any) -> None:
        self._write({"kind": "event", "event": event, "data": data})

    def _write(self, message: dict[str, Any]) -> None:
        encoded = json.dumps(message, separators=(",", ":"), ensure_ascii=False)
        with self._write_lock:
            sys.stdout.write(encoded + "\n")
            sys.stdout.flush()

    def _peer_data(self, peer: Peer) -> dict[str, Any]:
        data = {
            "node_id": peer.node_id,
            "short_id": peer.short_id,
            "name": peer.name,
            "host": peer.host,
            "port": peer.port,
            "last_seen": 0.0,
            "relay": peer.relay,
            "online": False,
            "nickname": "",
            "verified": False,
        }
        node = self.node
        if node is not None and node.store is not None:
            contact = node.store.contact(peer.node_id)
            if contact is not None:
                data["nickname"] = contact["nickname"]
                data["verified"] = contact["verified"]
                data["last_seen"] = contact["last_seen"]
            data["online"] = node.is_online(peer.node_id)
        return data

    def _message_data(self, message: dict[str, Any]) -> dict[str, Any]:
        data = {key: value for key, value in message.items() if key != "sender"}
        sender = message.get("sender")
        if isinstance(sender, Peer):
            data["sender"] = self._peer_data(sender)
        return data

    def _invite(self) -> str | None:
        if self.node is None:
            return None
        peer = self.node.peer
        return f"cnp2p:{peer.node_id}@{peer.host}:{peer.port}"

    def _known_peers(self) -> list[Peer]:
        assert self.node is not None
        peers: dict[str, Peer] = {}
        if self.node.store is not None:
            for contact in self.node.store.contacts():
                peers[contact["peer"].node_id] = contact["peer"]
        for peer in self.node.routing.all_peers():
            peers[peer.node_id] = peer
        peers.pop(self.node.node_id, None)
        return sorted(peers.values(), key=lambda item: (item.name.lower(), item.node_id))

    def _snapshot(self) -> dict[str, Any]:
        if self.node is None:
            return {"running": False, "peers": []}
        store = self.node.store
        return {
            "running": True,
            "self": {**self._peer_data(self.node.peer), "online": True},
            "invite": self._invite(),
            "peers": [self._peer_data(peer) for peer in self._known_peers()],
            "relay_server": self.node.relay_enabled,
            "upnp": self.node._port_mapping is not None,
            "unread": store.unread_total() if store is not None else 0,
            "attachments_dir": str(self.node.transfers.attachments_dir) if self.node.transfers else None,
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
        self.read_receipts = bool(payload.get("read_receipts", True))
        max_file_mb = min(max(int(payload.get("max_file_mb", 100)), 1), 2_048)

        def on_message(message: dict[str, Any]) -> None:
            self.emit("message", self._message_data(message))

        def on_peer(peer: Peer) -> None:
            self.emit("peer", self._peer_data(peer))

        def on_status(status: dict[str, Any]) -> None:
            self.emit("status", status)

        def on_update(update: dict[str, Any]) -> None:
            self.emit(str(update["type"]), {key: value for key, value in update.items() if key != "type"})

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
            max_file_bytes=max_file_mb * 1024 * 1024,
            on_message=on_message,
            on_peer=on_peer,
            on_status=on_status,
            on_update=on_update,
        )
        await self.node.start()
        self.emit("status", {"category": "node", "state": "online", "detail": "Node started"})

        for value in payload.get("bootstrap", []):
            if not str(value).strip():
                continue
            try:
                _, (host, bootstrap_port) = parse_invite(str(value))
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

    def _require_store(self):  # type: ignore[no-untyped-def]
        store = self._require_node().store
        if store is None:
            raise RuntimeError("message history is disabled")
        return store

    @staticmethod
    def _node_id(payload: dict[str, Any]) -> str:
        return validate_node_id(str(payload["node_id"]))

    async def snapshot(self, payload: dict[str, Any]) -> dict[str, Any]:
        del payload
        return self._snapshot()

    async def connect(self, payload: dict[str, Any]) -> dict[str, Any]:
        node = self._require_node()
        expected_id, (host, port) = parse_invite(str(payload["address"]))
        peer = await node.bootstrap(host, port)
        if expected_id is not None and peer.node_id != expected_id:
            found = await node.find_node(expected_id)
            if found is None:
                raise NodeError("the node at that address is not the invited contact")
            peer = found
        return {"peer": self._peer_data(peer), "snapshot": self._snapshot()}

    async def send(self, payload: dict[str, Any]) -> dict[str, Any]:
        node = self._require_node()
        text = str(payload.get("text", ""))
        message_id = str(payload.get("message_id", "")) or None
        if message_id is not None and not 16 <= len(message_id) <= 64:
            raise ValueError("invalid message ID")
        receipt = await node.send_message(str(payload["node_id"]), text, message_id=message_id)
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

    async def conversations(self, payload: dict[str, Any]) -> dict[str, Any]:
        del payload
        store = self._require_store()
        return {"conversations": store.list_conversations(), "unread": store.unread_total()}

    async def history(self, payload: dict[str, Any]) -> dict[str, Any]:
        store = self._require_store()
        before = payload.get("before")
        messages = store.history(
            self._node_id(payload),
            before=float(before) if before is not None else None,
            limit=int(payload.get("limit", 50)),
        )
        return {"messages": messages}

    async def send_file(self, payload: dict[str, Any]) -> dict[str, Any]:
        node = self._require_node()
        return {"message": await node.send_file(self._node_id(payload), Path(str(payload["path"])))}

    async def cancel_transfer(self, payload: dict[str, Any]) -> dict[str, Any]:
        node = self._require_node()
        if node.transfers is None:
            raise RuntimeError("file transfer is disabled")
        await node.transfers.cancel(str(payload["transfer_id"]))
        return {"cancelled": True}

    async def retry(self, payload: dict[str, Any]) -> dict[str, Any]:
        node = self._require_node()
        receipt = await node.retry_message(str(payload["message_id"]))
        return {"mode": receipt.mode if receipt is not None else "file"}

    async def mark_read(self, payload: dict[str, Any]) -> dict[str, Any]:
        node = self._require_node()
        message_ids = node.mark_read(self._node_id(payload), send_receipt=self.read_receipts)
        store = self._require_store()
        return {"message_ids": message_ids, "unread": store.unread_total()}

    async def rename_contact(self, payload: dict[str, Any]) -> dict[str, Any]:
        store = self._require_store()
        if not store.set_nickname(self._node_id(payload), str(payload.get("nickname", ""))):
            raise ValueError("unknown contact")
        return await self.contact_info(payload)

    async def set_verified(self, payload: dict[str, Any]) -> dict[str, Any]:
        store = self._require_store()
        if not store.set_verified(self._node_id(payload), bool(payload.get("verified"))):
            raise ValueError("unknown contact")
        return await self.contact_info(payload)

    async def delete_conversation(self, payload: dict[str, Any]) -> dict[str, Any]:
        node = self._require_node()
        store = self._require_store()
        transfers = store.delete_conversation(self._node_id(payload))
        if node.transfers is not None:
            node.transfers.forget(transfers)
        return {"deleted": True, "unread": store.unread_total()}

    async def contact_info(self, payload: dict[str, Any]) -> dict[str, Any]:
        node = self._require_node()
        node_id = self._node_id(payload)
        peer = node.routing.get(node_id) or (node.store.get_contact(node_id) if node.store else None)
        if peer is None:
            raise ValueError("unknown contact")
        return {
            "peer": self._peer_data(peer),
            "safety_number": safety_number(node.identity.signing_public_key, peer.signing_key),
            "id_length": ID_HEX_LENGTH,
        }

    async def dispatch(self, request: dict[str, Any]) -> None:
        request_id = request.get("id")
        action = str(request.get("action", ""))
        payload = request.get("payload") or {}
        try:
            if action not in ACTIONS:
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
    pending: set[asyncio.Task[None]] = set()
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
            if request.get("action") in {"start", "stop"}:
                # Lifecycle requests wait for in-flight work so they never race it.
                if pending:
                    await asyncio.gather(*pending, return_exceptions=True)
                await bridge.dispatch(request)
                continue
            # Other requests run concurrently: a slow send to an offline peer
            # must not block the conversation list or a file transfer.
            task = asyncio.create_task(bridge.dispatch(request))
            pending.add(task)
            task.add_done_callback(pending.discard)
    finally:
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        await bridge.stop({})


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
