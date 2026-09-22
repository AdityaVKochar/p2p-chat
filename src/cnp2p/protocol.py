from __future__ import annotations

import asyncio
import json
from typing import Any


MAX_FRAME_BYTES = 65_536
PROTOCOL_VERSION = 1


class ProtocolError(Exception):
    pass


def encode_frame(message: dict[str, Any]) -> bytes:
    message = {"version": PROTOCOL_VERSION, **message}
    encoded = json.dumps(message, separators=(",", ":"), ensure_ascii=False).encode("utf-8") + b"\n"
    if len(encoded) > MAX_FRAME_BYTES:
        raise ProtocolError("message exceeds maximum frame size")
    return encoded


async def read_frame(reader: asyncio.StreamReader) -> dict[str, Any]:
    try:
        raw = await reader.readline()
    except (ValueError, asyncio.LimitOverrunError) as error:
        raise ProtocolError("message exceeds maximum frame size") from error
    if not raw:
        raise ProtocolError("connection closed before a message was received")
    if len(raw) > MAX_FRAME_BYTES or not raw.endswith(b"\n"):
        raise ProtocolError("message exceeds maximum frame size or is incomplete")
    try:
        message = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ProtocolError("invalid JSON message") from error
    if not isinstance(message, dict):
        raise ProtocolError("protocol frame must be a JSON object")
    if message.get("version") != PROTOCOL_VERSION:
        raise ProtocolError("unsupported protocol version")
    if not isinstance(message.get("type"), str):
        raise ProtocolError("message type is required")
    return message


async def request(
    host: str,
    port: int,
    message: dict[str, Any],
    *,
    timeout: float = 4.0,
) -> dict[str, Any]:
    writer: asyncio.StreamWriter | None = None
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port, limit=MAX_FRAME_BYTES + 1),
            timeout=timeout,
        )
        writer.write(encode_frame(message))
        await asyncio.wait_for(writer.drain(), timeout=timeout)
        return await asyncio.wait_for(read_frame(reader), timeout=timeout)
    except (OSError, asyncio.TimeoutError) as error:
        raise ConnectionError(f"could not contact {host}:{port}: {error}") from error
    finally:
        if writer is not None:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass

