from __future__ import annotations

import asyncio
from dataclasses import dataclass
import os
import socket
import struct


MAGIC_COOKIE = 0x2112A442
BINDING_REQUEST = 0x0001
BINDING_RESPONSE = 0x0101
XOR_MAPPED_ADDRESS = 0x0020
MAPPED_ADDRESS = 0x0001


class StunError(ConnectionError):
    pass


@dataclass(frozen=True, slots=True)
class PublicEndpoint:
    host: str
    port: int


def _parse_address(attribute_type: int, value: bytes, transaction_id: bytes) -> PublicEndpoint:
    if len(value) < 8 or value[1] != 0x01:
        raise StunError("STUN server did not return an IPv4 address")
    port = struct.unpack("!H", value[2:4])[0]
    address = value[4:8]
    if attribute_type == XOR_MAPPED_ADDRESS:
        port ^= MAGIC_COOKIE >> 16
        cookie = struct.pack("!I", MAGIC_COOKIE)
        address = bytes(left ^ right for left, right in zip(address, cookie, strict=True))
    return PublicEndpoint(socket.inet_ntoa(address), port)


def _blocking_stun_lookup(host: str, port: int, timeout: float) -> PublicEndpoint:
    transaction_id = os.urandom(12)
    request = struct.pack("!HHI12s", BINDING_REQUEST, 0, MAGIC_COOKIE, transaction_id)
    addresses = socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_DGRAM)
    if not addresses:
        raise StunError("STUN server address could not be resolved")
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    try:
        sock.sendto(request, addresses[0][4])
        response, _ = sock.recvfrom(2_048)
    except OSError as error:
        raise StunError(f"STUN request failed: {error}") from error
    finally:
        sock.close()
    if len(response) < 20:
        raise StunError("STUN response is incomplete")
    message_type, length, cookie, response_transaction = struct.unpack("!HHI12s", response[:20])
    if (
        message_type != BINDING_RESPONSE
        or cookie != MAGIC_COOKIE
        or response_transaction != transaction_id
        or len(response) < 20 + length
    ):
        raise StunError("STUN response header is invalid")
    offset = 20
    fallback: PublicEndpoint | None = None
    while offset + 4 <= 20 + length:
        attribute_type, attribute_length = struct.unpack("!HH", response[offset : offset + 4])
        value = response[offset + 4 : offset + 4 + attribute_length]
        if len(value) != attribute_length:
            break
        if attribute_type == XOR_MAPPED_ADDRESS:
            return _parse_address(attribute_type, value, transaction_id)
        if attribute_type == MAPPED_ADDRESS:
            fallback = _parse_address(attribute_type, value, transaction_id)
        offset += 4 + ((attribute_length + 3) & ~3)
    if fallback is not None:
        return fallback
    raise StunError("STUN response did not contain a mapped address")


async def discover_public_endpoint(host: str, port: int, *, timeout: float = 3.0) -> PublicEndpoint:
    """Use an explicitly configured RFC 5389 server to observe the UDP endpoint."""
    return await asyncio.to_thread(_blocking_stun_lookup, host, port, timeout)

