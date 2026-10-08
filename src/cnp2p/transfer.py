from __future__ import annotations

import asyncio
import base64
from contextlib import suppress
from dataclasses import dataclass, field
import hashlib
import json
import math
import mimetypes
import os
from pathlib import Path
import re
import secrets
import shutil
import time
from typing import TYPE_CHECKING, Any

from .crypto import CryptoError, open_chunk, open_payload, seal_chunk, seal_payload
from .models import Peer
from .protocol import ProtocolError, request
from .store import public_transfer

if TYPE_CHECKING:
    from .node import P2PNode


CHUNK_SIZE = 32 * 1024
DEFAULT_MAX_FILE_BYTES = 100 * 1024 * 1024
MAX_ACTIVE_INCOMING = 8
SEND_WINDOW = 4
WAITING_TTL = 7 * 24 * 60 * 60
PROGRESS_INTERVAL = 0.25
CHECKPOINT_EVERY = 32
TRANSFER_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
WINDOWS_RESERVED = re.compile(r"^(con|prn|aux|nul|com[1-9]|lpt[1-9])(\..*)?$", re.IGNORECASE)


class TransferError(Exception):
    pass


class TransferRejected(TransferError):
    """The recipient refused the transfer; retrying will not help."""


def sanitize_filename(name: str) -> str:
    """Reduce an untrusted file name to a safe single path component."""
    name = name.replace("\\", "/").rsplit("/", 1)[-1]
    name = "".join(character for character in name if character.isprintable())
    name = re.sub(r'[<>:"|?*\x00]', "_", name).strip().strip(".")
    if not name:
        name = "file"
    if WINDOWS_RESERVED.match(name):
        name = f"_{name}"
    stem, dot, suffix = name.rpartition(".")
    if dot and len(suffix) <= 16:
        return f"{stem[:120]}.{suffix}"
    return name[:128]


def unique_path(directory: Path, name: str) -> Path:
    candidate = directory / name
    stem, dot, suffix = name.rpartition(".")
    if not dot:
        stem, suffix = name, ""
    counter = 1
    while candidate.exists():
        candidate = directory / (f"{stem} ({counter}).{suffix}" if suffix else f"{stem} ({counter})")
        counter += 1
    return candidate


def _copy_and_hash(source: Path, destination: Path) -> str:
    digest = hashlib.sha256()
    with source.open("rb") as reader, destination.open("wb") as writer:
        while block := reader.read(1024 * 1024):
            digest.update(block)
            writer.write(block)
    return digest.hexdigest()


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as reader:
        while block := reader.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _read_chunk(path: Path, offset: int, length: int) -> bytes:
    with path.open("rb") as reader:
        reader.seek(offset)
        return reader.read(length)


def _write_chunk(path: Path, offset: int, data: bytes) -> None:
    with path.open("r+b") as writer:
        writer.seek(offset)
        writer.write(data)


@dataclass(slots=True)
class IncomingState:
    transfer_id: str
    sender_id: str
    key: bytes
    size: int
    chunk_size: int
    chunks: int
    sha256: str
    partial: Path
    have: set[int] = field(default_factory=set)
    since_checkpoint: int = 0

    @property
    def checkpoint(self) -> Path:
        return self.partial.with_suffix(".have")

    def chunk_length(self, index: int) -> int:
        if index == self.chunks - 1:
            return self.size - self.chunk_size * (self.chunks - 1)
        return self.chunk_size

    def save_checkpoint(self) -> None:
        self.checkpoint.write_text(json.dumps(sorted(self.have)), encoding="utf-8")
        self.since_checkpoint = 0


class TransferManager:
    """Chunked, end-to-end encrypted file transfer directly between peers.

    The sender announces a file with a sealed ``FILE_OFFER`` that carries a
    random per-transfer key. Chunks are then encrypted with that key and pushed
    with ``FILE_CHUNK`` frames, and ``FILE_COMPLETE`` asks the recipient to
    verify the SHA-256 digest. Transfers never use the relay: when the
    recipient is unreachable the sender keeps the transfer waiting and retries
    once the peer is seen again.
    """

    def __init__(self, node: P2PNode, attachments_dir: Path, *, max_file_bytes: int) -> None:
        self.node = node
        self.attachments_dir = attachments_dir
        self.sent_dir = attachments_dir / "sent"
        self.partial_dir = attachments_dir / ".partial"
        for directory in (self.attachments_dir, self.sent_dir, self.partial_dir):
            directory.mkdir(parents=True, exist_ok=True)
        self.max_file_bytes = max_file_bytes
        self._incoming: dict[str, IncomingState] = {}
        self._sending: dict[str, asyncio.Task[None]] = {}
        self._last_progress: dict[str, float] = {}

    @property
    def store(self):  # type: ignore[no-untyped-def]
        assert self.node.store is not None
        return self.node.store

    def _emit(self, transfer_id: str, *, force: bool = True) -> None:
        now = time.monotonic()
        if not force and now - self._last_progress.get(transfer_id, 0.0) < PROGRESS_INTERVAL:
            return
        self._last_progress[transfer_id] = now
        record = self.store.get_transfer(transfer_id)
        if record is not None:
            self.node._emit_update({"type": "transfer", "transfer": public_transfer(record)})

    async def stop(self) -> None:
        for task in tuple(self._sending.values()):
            task.cancel()
        if self._sending:
            await asyncio.gather(*self._sending.values(), return_exceptions=True)
        self._sending.clear()
        for state in self._incoming.values():
            with suppress(OSError):
                state.save_checkpoint()
        self._incoming.clear()

    # Sending --------------------------------------------------------------

    async def send_file(self, identifier: str, path: Path) -> dict[str, Any]:
        peer = await self.node.resolve_peer(identifier)
        source = Path(path).expanduser()
        if not source.is_file():
            raise TransferError("choose an existing file to send")
        size = source.stat().st_size
        if size == 0:
            raise TransferError("empty files cannot be sent")
        if size > self.max_file_bytes:
            raise TransferError(f"files are limited to {self.max_file_bytes // (1024 * 1024)} MB")

        transfer_id = secrets.token_hex(16)
        message_id = secrets.token_hex(16)
        name = sanitize_filename(source.name)
        destination = unique_path(self.sent_dir, f"{transfer_id[:8]}-{name}")
        digest = await asyncio.to_thread(_copy_and_hash, source, destination)
        mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
        sent_at = time.time()

        self.store.add_transfer(
            {
                "transfer_id": transfer_id,
                "peer_id": peer.node_id,
                "message_id": message_id,
                "direction": "outgoing",
                "name": name,
                "size": size,
                "mime": mime,
                "sha256": digest,
                "path": str(destination),
                "file_key": base64.b64encode(os.urandom(32)).decode("ascii"),
                "chunk_size": CHUNK_SIZE,
                "status": "waiting",
            }
        )
        self.store.add_message(
            message_id=message_id,
            peer_id=peer.node_id,
            direction="outgoing",
            kind="file",
            body=name,
            sent_at=sent_at,
            status="pending",
            transfer_id=transfer_id,
        )
        self.schedule(transfer_id)
        message = self.store.get_message(message_id)
        assert message is not None
        return message

    def schedule(self, transfer_id: str) -> None:
        if transfer_id in self._sending:
            return
        task = asyncio.create_task(self._run_send(transfer_id))
        self._sending[transfer_id] = task
        task.add_done_callback(lambda _task: self._sending.pop(transfer_id, None))

    def retry_for_peer(self, peer_id: str) -> None:
        for record in self.store.pending_outgoing_transfers(peer_id):
            self.schedule(record["transfer_id"])

    def retry_all(self) -> None:
        cutoff = time.time() - WAITING_TTL
        for record in self.store.pending_outgoing_transfers():
            if record["created_at"] < cutoff:
                self._finish_outgoing(record["transfer_id"], "failed", "recipient was offline for too long")
            else:
                self.schedule(record["transfer_id"])

    def retry(self, transfer_id: str) -> None:
        record = self.store.get_transfer(transfer_id)
        if record is None or record["direction"] != "outgoing":
            raise TransferError("unknown outgoing transfer")
        if record["status"] == "done":
            return
        self.store.update_transfer(transfer_id, status="waiting", error="", created_at=time.time())
        self.store.set_status([record["message_id"]], "pending", force=True)
        self._emit_message_status(record["message_id"], "pending")
        self._emit(transfer_id)
        self.schedule(transfer_id)

    def _emit_message_status(self, message_id: str, status: str) -> None:
        message = self.store.get_message(message_id)
        if message is not None:
            self.node._emit_update(
                {
                    "type": "message_status",
                    "peer_id": message["peer_id"],
                    "message_ids": [message_id],
                    "status": status,
                }
            )

    def _finish_outgoing(self, transfer_id: str, status: str, error: str = "") -> None:
        record = self.store.get_transfer(transfer_id)
        if record is None:
            return
        fields: dict[str, Any] = {"status": status, "error": error[:256]}
        if status == "done":
            fields["bytes_done"] = record["size"]
        self.store.update_transfer(transfer_id, **fields)
        message_status = {"done": "delivered", "failed": "failed", "cancelled": "failed"}.get(status)
        if message_status is not None:
            changed = self.store.set_status([record["message_id"]], message_status, force=status != "done")
            if changed:
                self._emit_message_status(record["message_id"], message_status)
        self._emit(transfer_id)

    def _peer_for(self, peer_id: str) -> Peer | None:
        return self.node.routing.get(peer_id) or self.store.get_contact(peer_id)

    async def _run_send(self, transfer_id: str) -> None:
        record = self.store.get_transfer(transfer_id)
        if record is None or record["status"] not in {"waiting", "sending"}:
            return
        peer = self._peer_for(record["peer_id"])
        if peer is None:
            return
        try:
            for _ in range(2):
                if await self._attempt_send(record, peer):
                    return
            self.store.update_transfer(transfer_id, status="waiting", error="the recipient lost the transfer")
            self._emit(transfer_id)
        except asyncio.CancelledError:
            current = self.store.get_transfer(transfer_id)
            if current is not None and current["status"] == "sending":
                self.store.update_transfer(transfer_id, status="waiting")
            raise
        except TransferRejected as error:
            self._finish_outgoing(transfer_id, "failed", str(error))
        except (ConnectionError, ProtocolError) as error:
            self.node.mark_unreachable(record["peer_id"])
            self.store.update_transfer(transfer_id, status="waiting", error=str(error)[:256])
            self._emit(transfer_id)
        except (TransferError, CryptoError, OSError) as error:
            self._finish_outgoing(transfer_id, "failed", str(error))

    async def _attempt_send(self, record: dict[str, Any], peer: Peer) -> bool:
        """Offer, stream, and complete a transfer.

        Returns False when the recipient lost track of the transfer and the
        offer should be repeated.
        """
        transfer_id = record["transfer_id"]
        path = Path(record["path"])
        if not path.is_file():
            raise TransferError("the local copy of this file is missing")
        key = base64.b64decode(record["file_key"])
        chunk_size = int(record["chunk_size"])
        chunks = math.ceil(record["size"] / chunk_size)
        message = self.store.get_message(record["message_id"])
        envelope = seal_payload(
            self.node.identity,
            self.node.peer,
            peer,
            record["message_id"],
            {
                "kind": "file",
                "transfer_id": transfer_id,
                "name": record["name"],
                "size": record["size"],
                "mime": record["mime"],
                "sha256": record["sha256"],
                "chunk_size": chunk_size,
                "chunks": chunks,
                "key": record["file_key"],
            },
            sent_at=message["sent_at"] if message else None,
        )
        response = await request(peer.host, peer.port, {"type": "FILE_OFFER", "envelope": envelope})
        self._raise_for_response(response, "FILE_ACCEPT")
        self.node._touch_peer(peer)

        have = {int(index) for index in response.get("have", []) if isinstance(index, int)}
        missing = [index for index in range(chunks) if index not in have]
        self.store.update_transfer(
            transfer_id,
            status="sending",
            error="",
            bytes_done=min(record["size"], len(have) * chunk_size),
        )
        if self.store.set_status([record["message_id"]], "sent"):
            self._emit_message_status(record["message_id"], "sent")
        self._emit(transfer_id)

        for _ in range(3):
            if not await self._send_chunks(record, peer, key, chunk_size, chunks, missing):
                return False
            response = await request(
                peer.host,
                peer.port,
                {"type": "FILE_COMPLETE", "transfer_id": transfer_id},
                timeout=30.0,
            )
            if response.get("type") == "FILE_INCOMPLETE":
                missing = [int(index) for index in response.get("missing", []) if isinstance(index, int)]
                continue
            if response.get("type") == "ERROR" and "unknown transfer" in str(response.get("error")):
                return False
            self._raise_for_response(response, "FILE_DONE")
            self._finish_outgoing(transfer_id, "done")
            return True
        raise TransferError("the recipient kept reporting missing chunks")

    async def _send_chunks(
        self,
        record: dict[str, Any],
        peer: Peer,
        key: bytes,
        chunk_size: int,
        chunks: int,
        indices: list[int],
    ) -> bool:
        transfer_id = record["transfer_id"]
        path = Path(record["path"])
        queue: asyncio.Queue[int] = asyncio.Queue()
        for index in indices:
            if 0 <= index < chunks:
                queue.put_nowait(index)
        sent_bytes = record["size"] - sum(
            min(chunk_size, record["size"] - index * chunk_size) for index in indices if 0 <= index < chunks
        )
        lost_transfer = False

        async def worker() -> None:
            nonlocal sent_bytes, lost_transfer
            while not queue.empty() and not lost_transfer:
                index = queue.get_nowait()
                data = await asyncio.to_thread(_read_chunk, path, index * chunk_size, chunk_size)
                nonce, ciphertext = seal_chunk(key, transfer_id, index, peer.node_id, data)
                response = await request(
                    peer.host,
                    peer.port,
                    {
                        "type": "FILE_CHUNK",
                        "transfer_id": transfer_id,
                        "index": index,
                        "nonce": nonce,
                        "data": ciphertext,
                    },
                    timeout=15.0,
                )
                if response.get("type") == "ERROR" and "unknown transfer" in str(response.get("error")):
                    lost_transfer = True
                    return
                self._raise_for_response(response, "FILE_CHUNK_OK")
                sent_bytes += len(data)
                self.store.update_transfer(transfer_id, bytes_done=sent_bytes)
                self._emit(transfer_id, force=False)

        workers = [asyncio.create_task(worker()) for _ in range(SEND_WINDOW)]
        try:
            await asyncio.gather(*workers)
        finally:
            for task in workers:
                task.cancel()
            await asyncio.gather(*workers, return_exceptions=True)
        return not lost_transfer

    @staticmethod
    def _raise_for_response(response: dict[str, Any], expected: str) -> None:
        kind = response.get("type")
        if kind == expected:
            return
        if kind == "FILE_REJECT":
            raise TransferRejected(str(response.get("reason", "the recipient declined the file")))
        if kind == "ERROR" and "unknown message type" in str(response.get("error")):
            raise TransferRejected("the recipient's app is too old to receive files")
        raise TransferError(str(response.get("error", f"unexpected response to file transfer: {kind}")))

    # Receiving ------------------------------------------------------------

    def handle_offer(self, envelope: dict[str, Any], observed_host: str | None) -> dict[str, Any]:
        sender, payload, sent_at = open_payload(self.node.identity, envelope)
        if payload["kind"] != "file":
            raise TransferError("file offer has the wrong payload kind")
        if observed_host is not None:
            sender = Peer.from_dict(sender.to_dict(), observed_host=observed_host)
        self.node._remember_peer(sender)
        offer = self._validate_offer(payload)
        transfer_id = offer["transfer_id"]
        message_id = str(envelope["message_id"])[:128]

        existing = self.store.get_transfer(transfer_id)
        if existing is not None:
            if existing["peer_id"] != sender.node_id or existing["direction"] != "incoming":
                return self._reject(transfer_id, "transfer ID conflict")
            if existing["status"] == "done":
                return {"type": "FILE_ACCEPT", "transfer_id": transfer_id, "have": list(range(offer["chunks"]))}
            if existing["status"] != "receiving":
                return self._reject(transfer_id, "the recipient cancelled this file")
            state = self._load_incoming(existing)
            if state is not None:
                return {"type": "FILE_ACCEPT", "transfer_id": transfer_id, "have": sorted(state.have)}
            # The partial file disappeared; start the download again from scratch.

        if offer["size"] > self.max_file_bytes:
            return self._reject(
                transfer_id,
                f"the recipient accepts files up to {self.max_file_bytes // (1024 * 1024)} MB",
            )
        if len(self._incoming) >= MAX_ACTIVE_INCOMING:
            return self._reject(transfer_id, "the recipient is busy; try again later", terminal=False)
        if shutil.disk_usage(self.attachments_dir).free < offer["size"] * 2:
            return self._reject(transfer_id, "the recipient does not have enough disk space")

        partial = self.partial_dir / f"{transfer_id}.part"
        with partial.open("wb") as handle:
            handle.truncate(offer["size"])
        state = IncomingState(
            transfer_id=transfer_id,
            sender_id=sender.node_id,
            key=base64.b64decode(offer["key"]),
            size=offer["size"],
            chunk_size=offer["chunk_size"],
            chunks=offer["chunks"],
            sha256=offer["sha256"],
            partial=partial,
        )
        state.save_checkpoint()
        self._incoming[transfer_id] = state
        if existing is not None:
            self.store.update_transfer(transfer_id, path=str(partial), bytes_done=0)
            self._emit(transfer_id)
            return {"type": "FILE_ACCEPT", "transfer_id": transfer_id, "have": []}
        self.store.add_transfer(
            {
                "transfer_id": transfer_id,
                "peer_id": sender.node_id,
                "message_id": message_id,
                "direction": "incoming",
                "name": offer["name"],
                "size": offer["size"],
                "mime": offer["mime"],
                "sha256": offer["sha256"],
                "path": str(partial),
                "file_key": offer["key"],
                "chunk_size": offer["chunk_size"],
                "status": "receiving",
            }
        )
        added = self.store.add_message(
            message_id=message_id,
            peer_id=sender.node_id,
            direction="incoming",
            kind="file",
            body=offer["name"],
            sent_at=sent_at,
            status="received",
            transfer_id=transfer_id,
        )
        if added:
            self.node._notify_message(self.store.get_message(message_id), sender, offline=False)
        return {"type": "FILE_ACCEPT", "transfer_id": transfer_id, "have": []}

    def _reject(self, transfer_id: str, reason: str, *, terminal: bool = True) -> dict[str, Any]:
        if not terminal:
            return {"type": "ERROR", "error": reason}
        return {"type": "FILE_REJECT", "transfer_id": transfer_id, "reason": reason}

    def _validate_offer(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            transfer_id = str(payload["transfer_id"])
            size = int(payload["size"])
            chunk_size = int(payload["chunk_size"])
            chunks = int(payload["chunks"])
            sha256 = str(payload["sha256"]).lower()
            key = str(payload["key"])
            mime = str(payload.get("mime", "application/octet-stream"))[:128]
            name = sanitize_filename(str(payload["name"]))
        except (KeyError, TypeError, ValueError) as error:
            raise TransferError("malformed file offer") from error
        if not TRANSFER_ID_PATTERN.match(transfer_id):
            raise TransferError("invalid transfer ID")
        if size < 1 or not 1024 <= chunk_size <= CHUNK_SIZE or chunks != math.ceil(size / chunk_size):
            raise TransferError("invalid file size or chunking")
        if not re.fullmatch(r"[0-9a-f]{64}", sha256):
            raise TransferError("invalid file digest")
        try:
            if len(base64.b64decode(key, validate=True)) != 32:
                raise ValueError
        except ValueError as error:
            raise TransferError("invalid file key") from error
        return {
            "transfer_id": transfer_id,
            "size": size,
            "chunk_size": chunk_size,
            "chunks": chunks,
            "sha256": sha256,
            "key": key,
            "mime": mime,
            "name": name,
        }

    def _load_incoming(self, record: dict[str, Any]) -> IncomingState | None:
        state = self._incoming.get(record["transfer_id"])
        if state is not None:
            return state
        partial = Path(record["path"])
        if record["status"] != "receiving" or not partial.is_file():
            return None
        state = IncomingState(
            transfer_id=record["transfer_id"],
            sender_id=record["peer_id"],
            key=base64.b64decode(record["file_key"]),
            size=int(record["size"]),
            chunk_size=int(record["chunk_size"]),
            chunks=math.ceil(int(record["size"]) / int(record["chunk_size"])),
            sha256=record["sha256"],
            partial=partial,
        )
        with suppress(OSError, ValueError, TypeError):
            state.have = {int(index) for index in json.loads(state.checkpoint.read_text(encoding="utf-8"))}
        self._incoming[state.transfer_id] = state
        return state

    def _incoming_state(self, transfer_id: str) -> IncomingState:
        record = self.store.get_transfer(transfer_id) if TRANSFER_ID_PATTERN.match(transfer_id) else None
        if record is None or record["direction"] != "incoming":
            raise TransferError("unknown transfer")
        if record["status"] == "cancelled":
            raise TransferRejected("the recipient cancelled this file")
        state = self._load_incoming(record)
        if state is None:
            raise TransferError("unknown transfer")
        return state

    async def handle_chunk(self, message: dict[str, Any]) -> dict[str, Any]:
        transfer_id = str(message["transfer_id"])
        try:
            state = self._incoming_state(transfer_id)
        except TransferRejected as error:
            return self._reject(transfer_id, str(error))
        index = int(message["index"])
        if not 0 <= index < state.chunks:
            raise TransferError("chunk index out of range")
        data = open_chunk(
            state.key,
            transfer_id,
            index,
            self.node.node_id,
            str(message["nonce"]),
            str(message["data"]),
        )
        if len(data) != state.chunk_length(index):
            raise TransferError("chunk has the wrong length")
        if index not in state.have:
            await asyncio.to_thread(_write_chunk, state.partial, index * state.chunk_size, data)
            state.have.add(index)
            state.since_checkpoint += 1
            if state.since_checkpoint >= CHECKPOINT_EVERY:
                await asyncio.to_thread(state.save_checkpoint)
            received = sum(state.chunk_length(item) for item in state.have)
            self.store.update_transfer(transfer_id, bytes_done=received)
            self._emit(transfer_id, force=False)
        return {"type": "FILE_CHUNK_OK", "transfer_id": transfer_id, "index": index}

    async def handle_complete(self, message: dict[str, Any]) -> dict[str, Any]:
        transfer_id = str(message["transfer_id"])
        record = self.store.get_transfer(transfer_id) if TRANSFER_ID_PATTERN.match(transfer_id) else None
        if record is not None and record["direction"] == "incoming" and record["status"] == "done":
            return {"type": "FILE_DONE", "transfer_id": transfer_id}
        try:
            state = self._incoming_state(transfer_id)
        except TransferRejected as error:
            return self._reject(transfer_id, str(error))
        missing = [index for index in range(state.chunks) if index not in state.have]
        if missing:
            return {"type": "FILE_INCOMPLETE", "transfer_id": transfer_id, "missing": missing[:2_000]}

        digest = await asyncio.to_thread(_hash_file, state.partial)
        if digest != state.sha256:
            self._discard_incoming(state)
            self.store.update_transfer(transfer_id, status="failed", error="checksum mismatch")
            self._emit(transfer_id)
            return self._reject(transfer_id, "the received file failed its integrity check")

        assert record is not None
        destination = unique_path(self.attachments_dir, record["name"])
        os.replace(state.partial, destination)
        with suppress(OSError):
            state.checkpoint.unlink()
        self._incoming.pop(transfer_id, None)
        self.store.update_transfer(transfer_id, status="done", path=str(destination), bytes_done=state.size)
        self._emit(transfer_id)
        return {"type": "FILE_DONE", "transfer_id": transfer_id}

    def _discard_incoming(self, state: IncomingState) -> None:
        self._incoming.pop(state.transfer_id, None)
        for path in (state.partial, state.checkpoint):
            with suppress(OSError):
                path.unlink()

    # Cancellation ---------------------------------------------------------

    async def cancel(self, transfer_id: str) -> None:
        record = self.store.get_transfer(transfer_id)
        if record is None:
            raise TransferError("unknown transfer")
        if record["status"] in {"done", "cancelled"}:
            return
        if record["direction"] == "outgoing":
            task = self._sending.pop(transfer_id, None)
            if task is not None:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
            self._finish_outgoing(transfer_id, "cancelled", "cancelled")
            return
        state = self._load_incoming(record)
        if state is not None:
            self._discard_incoming(state)
        self.store.update_transfer(transfer_id, status="cancelled", error="cancelled")
        self._emit(transfer_id)

    def forget(self, transfers: list[dict[str, Any]]) -> None:
        """Remove files belonging to deleted conversations."""
        for record in transfers:
            task = self._sending.pop(record["transfer_id"], None)
            if task is not None:
                task.cancel()
            state = self._incoming.pop(record["transfer_id"], None)
            if state is not None:
                self._discard_incoming(state)
            path = Path(record["path"])
            with suppress(OSError):
                if path.resolve().is_relative_to(self.attachments_dir.resolve()):
                    path.unlink()
