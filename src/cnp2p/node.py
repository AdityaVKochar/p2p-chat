from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
import secrets
import socket
import time
from typing import Any

from .crypto import (
    CryptoError,
    open_message,
    peer_record_signature,
    seal_message,
    signed_relay_action,
    verify_envelope_signature,
    verify_relay_action,
)
from .discovery import DiscoveryProtocol, start_discovery
from .identity import load_or_create_identity
from .models import ID_HEX_LENGTH, Peer, validate_node_id
from .protocol import MAX_FRAME_BYTES, ProtocolError, encode_frame, read_frame, request
from .relay import RelayStore, RelayStoreError
from .routing import RoutingTable


MAX_CHAT_CHARACTERS = 4_096
SEEN_MESSAGE_LIMIT = 2_048
SEEN_MESSAGE_TTL = 3_600.0
ROUTING_ENTRY_TTL = 30 * 60.0


class NodeError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class DeliveryReceipt:
    message_id: str
    mode: str
    relay: str | None = None


def detect_lan_ip() -> str:
    """Select the address the operating system would use for outbound traffic."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("192.0.2.1", 9))
        return str(probe.getsockname()[0])
    except OSError:
        return "127.0.0.1"
    finally:
        probe.close()


class P2PNode:
    def __init__(
        self,
        *,
        name: str,
        port: int,
        data_dir: Path,
        host: str = "0.0.0.0",
        advertise_host: str | None = None,
        lan_discovery: bool = True,
        relay_enabled: bool = False,
        relay_endpoints: list[tuple[str, int]] | None = None,
        offline_poll_interval: float = 10.0,
        on_message: Callable[[dict[str, Any]], None] | None = None,
        on_peer: Callable[[Peer], None] | None = None,
    ) -> None:
        if not 0 <= port <= 65535:
            raise ValueError("port must be between 0 and 65535")
        self.identity = load_or_create_identity(data_dir)
        self.node_id = self.identity.node_id
        self.name = name[:64] or "anonymous"
        self.host = host
        self.port = port
        self.advertise_host = advertise_host or detect_lan_ip()
        self.lan_discovery = lan_discovery
        self.relay_enabled = relay_enabled
        self.relay_endpoints = list(relay_endpoints or [])
        self.offline_poll_interval = max(1.0, offline_poll_interval)
        self.on_message = on_message
        self.on_peer = on_peer
        self.routing = RoutingTable(self.node_id)
        self.relay_store = RelayStore(data_dir / "relay.sqlite3") if relay_enabled else None

        self._server: asyncio.Server | None = None
        self._discovery_transport: asyncio.DatagramTransport | None = None
        self._discovery_protocol: DiscoveryProtocol | None = None
        self._background_tasks: set[asyncio.Task[Any]] = set()
        self._seen_messages: OrderedDict[str, float] = OrderedDict()

    @property
    def peer(self) -> Peer:
        return Peer(
            node_id=self.node_id,
            host=self.advertise_host,
            port=self.port,
            name=self.name,
            last_seen=time.time(),
            signing_key=self.identity.signing_public_key,
            encryption_key=self.identity.encryption_public_key,
            relay=self.relay_enabled,
            record_signature=peer_record_signature(self.identity, relay=self.relay_enabled),
        )

    async def start(self) -> None:
        if self._server is not None:
            return
        self._server = await asyncio.start_server(
            self._handle_connection,
            self.host,
            self.port,
            limit=MAX_FRAME_BYTES + 1,
        )
        sockets = self._server.sockets or []
        if not sockets:
            raise NodeError("server did not expose a listening socket")
        self.port = int(sockets[0].getsockname()[1])

        if self.lan_discovery:
            try:
                self._discovery_transport, self._discovery_protocol = await start_discovery(
                    self.peer,
                    self._remember_peer,
                )
                self._spawn(self._announcement_loop())
            except (OSError, ValueError, NotImplementedError):
                self._discovery_transport = None
                self._discovery_protocol = None
        self._spawn(self._maintenance_loop())
        self._spawn(self._offline_poll_loop())

    async def stop(self) -> None:
        for task in tuple(self._background_tasks):
            task.cancel()
        if self._background_tasks:
            await asyncio.gather(*self._background_tasks, return_exceptions=True)
        self._background_tasks.clear()
        if self._discovery_transport is not None:
            self._discovery_transport.close()
            self._discovery_transport = None
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    def _spawn(self, coroutine: Any) -> None:
        task = asyncio.create_task(coroutine)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    def _remember_peer(self, peer: Peer) -> None:
        if peer.node_id == self.node_id:
            return
        if not peer.is_authenticated:
            raise NodeError("refusing an unauthenticated peer record")
        self.routing.add(peer)
        if self.on_peer is not None:
            self.on_peer(peer)

    async def _announcement_loop(self) -> None:
        while True:
            if self._discovery_protocol is not None:
                self._discovery_protocol.announce()
            await asyncio.sleep(5)

    async def _maintenance_loop(self) -> None:
        while True:
            await asyncio.sleep(60)
            self.routing.purge_older_than(ROUTING_ENTRY_TTL)
            self._purge_seen_messages()
            if self.relay_store is not None:
                await asyncio.to_thread(self.relay_store.cleanup)

    async def _offline_poll_loop(self) -> None:
        await asyncio.sleep(1)
        while True:
            with suppress(ConnectionError, NodeError, ProtocolError, CryptoError):
                await self.fetch_offline_messages()
            await asyncio.sleep(self.offline_poll_interval)

    def _purge_seen_messages(self) -> None:
        cutoff = time.time() - SEEN_MESSAGE_TTL
        while self._seen_messages:
            _, seen_at = next(iter(self._seen_messages.items()))
            if seen_at >= cutoff and len(self._seen_messages) <= SEEN_MESSAGE_LIMIT:
                break
            self._seen_messages.popitem(last=False)

    async def _handle_connection(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        remote = writer.get_extra_info("peername")
        observed_host = str(remote[0]) if remote else None
        try:
            message = await asyncio.wait_for(read_frame(reader), timeout=5.0)
            response = await self._dispatch(message, observed_host)
        except (
            ProtocolError,
            NodeError,
            CryptoError,
            RelayStoreError,
            ValueError,
            KeyError,
            TypeError,
            asyncio.TimeoutError,
        ) as error:
            response = {"type": "ERROR", "error": str(error)[:256]}
        try:
            writer.write(encode_frame(response))
            await writer.drain()
        except (ConnectionError, OSError, ProtocolError):
            pass
        finally:
            writer.close()
            with suppress(OSError):
                await writer.wait_closed()

    async def _dispatch(self, message: dict[str, Any], observed_host: str | None) -> dict[str, Any]:
        message_type = message["type"]
        if message_type == "HELLO":
            sender = self._peer_from_message(message, observed_host)
            self._remember_peer(sender)
            return {
                "type": "HELLO_OK",
                "peer": self.peer.to_dict(),
                "nodes": [peer.to_dict() for peer in self.routing.closest(sender.node_id)],
            }
        if message_type == "FIND_NODE":
            sender = self._peer_from_message(message, observed_host)
            self._remember_peer(sender)
            target_id = validate_node_id(str(message["target_id"]))
            candidates = [self.peer, *self.routing.all_peers()]
            unique: dict[str, Peer] = {peer.node_id: peer for peer in candidates}
            ordered = sorted(
                unique.values(),
                key=lambda peer: int(peer.node_id, 16) ^ int(target_id, 16),
            )[:20]
            return {"type": "NODES", "nodes": [peer.to_dict() for peer in ordered]}
        if message_type == "CHAT":
            duplicate = self._receive_envelope(message["envelope"], observed_host, offline=False)
            return {
                "type": "ACK",
                "message_id": str(message["envelope"]["message_id"]),
                "duplicate": duplicate,
            }
        if message_type == "PING":
            sender = self._peer_from_message(message, observed_host)
            self._remember_peer(sender)
            return {"type": "PONG", "peer": self.peer.to_dict()}
        if message_type == "RELAY_STORE":
            return await self._relay_store_message(message["envelope"])
        if message_type == "RELAY_FETCH":
            return await self._relay_fetch_messages(message, observed_host)
        if message_type == "RELAY_DELETE":
            return await self._relay_delete_messages(message, observed_host)
        raise NodeError(f"unknown message type: {message_type}")

    def _peer_from_message(self, message: dict[str, Any], observed_host: str | None) -> Peer:
        return Peer.from_dict(message["peer"], observed_host=observed_host)

    def _receive_envelope(
        self,
        envelope: dict[str, Any],
        observed_host: str | None,
        *,
        offline: bool,
    ) -> bool:
        sender, text, sent_at = open_message(self.identity, envelope)
        if observed_host is not None:
            sender = Peer.from_dict(sender.to_dict(), observed_host=observed_host)
        self._remember_peer(sender)
        message_id = str(envelope["message_id"])
        if not text or len(text) > MAX_CHAT_CHARACTERS:
            raise NodeError(f"chat text must contain 1-{MAX_CHAT_CHARACTERS} characters")
        duplicate = message_id in self._seen_messages
        if not duplicate:
            self._seen_messages[message_id] = time.time()
            self._purge_seen_messages()
            if self.on_message is not None:
                self.on_message(
                    {
                        "message_id": message_id,
                        "sender": sender,
                        "text": text,
                        "sent_at": sent_at,
                        "offline": offline,
                        "encrypted": True,
                    }
                )
        return duplicate

    async def _relay_store_message(self, envelope: dict[str, Any]) -> dict[str, Any]:
        if self.relay_store is None:
            raise NodeError("this node is not configured as a relay")
        verify_envelope_signature(envelope)
        validate_node_id(str(envelope["recipient_id"]))
        queued = await asyncio.to_thread(self.relay_store.store, envelope)
        return {
            "type": "RELAY_STORED",
            "message_id": str(envelope["message_id"]),
            "queued": queued,
        }

    async def _relay_fetch_messages(
        self,
        message: dict[str, Any],
        observed_host: str | None,
    ) -> dict[str, Any]:
        if self.relay_store is None:
            raise NodeError("this node is not configured as a relay")
        peer = self._peer_from_message(message, observed_host)
        verify_relay_action(peer, message["authorization"], "fetch")
        self._remember_peer(peer)
        envelopes = await asyncio.to_thread(self.relay_store.fetch, peer.node_id)
        return {"type": "RELAY_MESSAGES", "envelopes": envelopes}

    async def _relay_delete_messages(
        self,
        message: dict[str, Any],
        observed_host: str | None,
    ) -> dict[str, Any]:
        if self.relay_store is None:
            raise NodeError("this node is not configured as a relay")
        peer = self._peer_from_message(message, observed_host)
        authorization = message["authorization"]
        verify_relay_action(peer, authorization, "delete")
        self._remember_peer(peer)
        message_ids = authorization.get("message_ids")
        if not isinstance(message_ids, list):
            raise NodeError("relay deletion requires message IDs")
        deleted = await asyncio.to_thread(self.relay_store.delete, peer.node_id, message_ids)
        return {"type": "RELAY_DELETED", "deleted": deleted}

    async def bootstrap(self, host: str, port: int) -> Peer:
        response = await request(host, port, {"type": "HELLO", "peer": self.peer.to_dict()})
        if response.get("type") != "HELLO_OK":
            raise NodeError(str(response.get("error", "peer rejected the handshake")))
        direct_peer = Peer.from_dict(response["peer"], observed_host=host)
        self._remember_peer(direct_peer)
        self._remember_node_records(response.get("nodes", []))
        await self.find_node(self.node_id)
        if direct_peer.relay:
            with suppress(ConnectionError, NodeError, ProtocolError, CryptoError):
                await self.fetch_offline_messages([(host, port)])
        return direct_peer

    def _remember_node_records(self, records: Any) -> list[Peer]:
        if not isinstance(records, list):
            return []
        peers: list[Peer] = []
        for record in records[:20]:
            try:
                peer = Peer.from_dict(record)
            except (KeyError, TypeError, ValueError):
                continue
            if peer.node_id != self.node_id:
                self._remember_peer(peer)
                peers.append(peer)
        return peers

    async def find_node(self, target_id: str, *, rounds: int = 8, alpha: int = 3) -> Peer | None:
        target_id = validate_node_id(target_id)
        if target_id == self.node_id:
            return self.peer
        known: dict[str, Peer] = {peer.node_id: peer for peer in self.routing.all_peers()}
        queried: set[str] = set()
        for _ in range(rounds):
            exact = known.get(target_id)
            if exact is not None:
                return exact
            candidates = sorted(
                (peer for peer in known.values() if peer.node_id not in queried),
                key=lambda peer: int(peer.node_id, 16) ^ int(target_id, 16),
            )[:alpha]
            if not candidates:
                break
            queried.update(peer.node_id for peer in candidates)
            results = await asyncio.gather(
                *(
                    request(
                        peer.host,
                        peer.port,
                        {
                            "type": "FIND_NODE",
                            "target_id": target_id,
                            "peer": self.peer.to_dict(),
                        },
                    )
                    for peer in candidates
                ),
                return_exceptions=True,
            )
            learned = False
            for peer, response in zip(candidates, results, strict=True):
                if isinstance(response, BaseException) or response.get("type") != "NODES":
                    continue
                peer.last_seen = time.time()
                self.routing.add(peer)
                for discovered in self._remember_node_records(response.get("nodes", [])):
                    if discovered.node_id not in known:
                        learned = True
                    known[discovered.node_id] = discovered
            if not learned and all(peer.node_id in queried for peer in known.values()):
                break
        return known.get(target_id)

    async def resolve_peer(self, identifier: str) -> Peer:
        identifier = identifier.strip().lower()
        if not identifier or len(identifier) > ID_HEX_LENGTH:
            raise NodeError("enter a valid node ID or ID prefix")
        try:
            int(identifier, 16)
        except ValueError as error:
            raise NodeError("node IDs contain only hexadecimal characters") from error
        if len(identifier) == ID_HEX_LENGTH:
            peer = self.routing.get(identifier) or await self.find_node(identifier)
            if peer is None:
                raise NodeError("peer could not be found in the network")
            return peer
        matches = self.routing.find_prefix(identifier)
        if not matches:
            raise NodeError("no known peer matches that ID prefix; use the full ID for a DHT lookup")
        if len(matches) > 1:
            raise NodeError("that ID prefix is ambiguous; enter more characters")
        return matches[0]

    def _relay_candidates(self) -> list[tuple[str, int]]:
        candidates = [*self.relay_endpoints]
        candidates.extend(peer.address for peer in self.routing.all_peers() if peer.relay)
        unique: list[tuple[str, int]] = []
        for candidate in candidates:
            if candidate not in unique and candidate != self.peer.address:
                unique.append(candidate)
        return unique

    async def _queue_with_relay(self, envelope: dict[str, Any]) -> str:
        candidates = self._relay_candidates()
        if not candidates:
            raise NodeError("direct delivery failed and no relay is configured")
        errors: list[str] = []
        for host, port in candidates:
            try:
                response = await request(host, port, {"type": "RELAY_STORE", "envelope": envelope})
                if (
                    response.get("type") == "RELAY_STORED"
                    and response.get("message_id") == envelope["message_id"]
                ):
                    return f"{host}:{port}"
                errors.append(str(response.get("error", "relay rejected the envelope")))
            except (ConnectionError, ProtocolError) as error:
                errors.append(str(error))
        raise NodeError("every configured relay rejected the message: " + "; ".join(errors))

    async def send_message(
        self,
        identifier: str,
        text: str,
        *,
        retries: int = 1,
    ) -> DeliveryReceipt:
        if not text or len(text) > MAX_CHAT_CHARACTERS:
            raise NodeError(f"chat text must contain 1-{MAX_CHAT_CHARACTERS} characters")
        peer = await self.resolve_peer(identifier)
        message_id = secrets.token_hex(16)
        envelope = seal_message(self.identity, self.peer, peer, message_id, text)
        payload = {"type": "CHAT", "envelope": envelope}
        for _ in range(retries + 1):
            try:
                response = await request(peer.host, peer.port, payload)
                if response.get("type") == "ACK" and response.get("message_id") == message_id:
                    peer.last_seen = time.time()
                    self.routing.add(peer)
                    return DeliveryReceipt(message_id, "direct")
                raise NodeError(str(response.get("error", "invalid acknowledgement")))
            except (ConnectionError, NodeError, ProtocolError):
                pass
        relay = await self._queue_with_relay(envelope)
        return DeliveryReceipt(message_id, "relay-queued", relay)

    async def fetch_offline_messages(
        self,
        endpoints: list[tuple[str, int]] | None = None,
    ) -> int:
        delivered = 0
        candidates = endpoints if endpoints is not None else self._relay_candidates()
        for host, port in dict.fromkeys(candidates):
            authorization = signed_relay_action(self.identity, "fetch")
            try:
                response = await request(
                    host,
                    port,
                    {
                        "type": "RELAY_FETCH",
                        "peer": self.peer.to_dict(),
                        "authorization": authorization,
                    },
                )
            except (ConnectionError, ProtocolError):
                continue
            if response.get("type") != "RELAY_MESSAGES":
                continue
            envelopes = response.get("envelopes")
            if not isinstance(envelopes, list):
                continue
            processed_ids: list[str] = []
            for envelope in envelopes[:100]:
                if not isinstance(envelope, dict):
                    continue
                message_id = str(envelope.get("message_id", ""))
                try:
                    duplicate = self._receive_envelope(envelope, None, offline=True)
                    if not duplicate:
                        delivered += 1
                except (CryptoError, NodeError, KeyError, TypeError, ValueError):
                    pass
                if message_id and len(message_id) <= 128:
                    processed_ids.append(message_id)
            if processed_ids:
                delete_authorization = signed_relay_action(
                    self.identity,
                    "delete",
                    message_ids=processed_ids,
                )
                with suppress(ConnectionError, ProtocolError):
                    await request(
                        host,
                        port,
                        {
                            "type": "RELAY_DELETE",
                            "peer": self.peer.to_dict(),
                            "authorization": delete_authorization,
                        },
                    )
        return delivered

    async def ping(self, peer: Peer) -> float:
        started = time.perf_counter()
        response = await request(
            peer.host,
            peer.port,
            {"type": "PING", "peer": self.peer.to_dict()},
        )
        if response.get("type") != "PONG":
            raise NodeError("peer returned an invalid ping response")
        peer.last_seen = time.time()
        self.routing.add(peer)
        return (time.perf_counter() - started) * 1_000
