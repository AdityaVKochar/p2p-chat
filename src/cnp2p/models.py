from __future__ import annotations

from dataclasses import asdict, dataclass
import base64
import hashlib
import ipaddress
import json
import time
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey


ID_BITS = 160
ID_HEX_LENGTH = ID_BITS // 4


def validate_node_id(value: str) -> str:
    value = value.lower()
    if len(value) != ID_HEX_LENGTH:
        raise ValueError(f"node ID must contain {ID_HEX_LENGTH} hexadecimal characters")
    int(value, 16)
    return value


@dataclass(slots=True)
class Peer:
    node_id: str
    host: str
    port: int
    name: str = "anonymous"
    last_seen: float = 0.0
    signing_key: str = ""
    encryption_key: str = ""
    relay: bool = False
    record_signature: str = ""

    def __post_init__(self) -> None:
        self.node_id = validate_node_id(self.node_id)
        if not 1 <= int(self.port) <= 65535:
            raise ValueError("port must be between 1 and 65535")
        self.port = int(self.port)
        self.name = str(self.name)[:64] or "anonymous"
        if not self.host or len(self.host) > 255:
            raise ValueError("invalid host")
        # Accept hostnames as well as IP literals, but reject whitespace and
        # malformed IP-looking values early.
        if any(character.isspace() for character in self.host):
            raise ValueError("host cannot contain whitespace")
        try:
            ipaddress.ip_address(self.host)
        except ValueError:
            if not all(part and part.replace("-", "").isalnum() for part in self.host.split(".")):
                raise ValueError("invalid hostname") from None
        if self.signing_key or self.encryption_key:
            if not self.is_authenticated:
                raise ValueError("peer cryptographic identity is invalid")

    @classmethod
    def from_dict(cls, data: dict[str, Any], *, observed_host: str | None = None) -> "Peer":
        host = observed_host if observed_host is not None else str(data["host"])
        return cls(
            node_id=str(data["node_id"]),
            host=host,
            port=int(data["port"]),
            name=str(data.get("name", "anonymous")),
            last_seen=float(data.get("last_seen", time.time())),
            signing_key=str(data["signing_key"]),
            encryption_key=str(data["encryption_key"]),
            relay=bool(data.get("relay", False)),
            record_signature=str(data["record_signature"]),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def short_id(self) -> str:
        return self.node_id[:8]

    @property
    def address(self) -> tuple[str, int]:
        return self.host, self.port

    @property
    def is_authenticated(self) -> bool:
        try:
            signing_key = base64.b64decode(self.signing_key, validate=True)
            encryption_key = base64.b64decode(self.encryption_key, validate=True)
            signature = base64.b64decode(self.record_signature, validate=True)
            if len(signing_key) != 32 or len(encryption_key) != 32 or len(signature) != 64:
                return False
            if hashlib.sha256(signing_key).hexdigest()[:ID_HEX_LENGTH] != self.node_id:
                return False
            binding = json.dumps(
                {
                    "encryption_key": self.encryption_key,
                    "node_id": self.node_id,
                    "relay": self.relay,
                    "signing_key": self.signing_key,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            Ed25519PublicKey.from_public_bytes(signing_key).verify(signature, binding)
            return True
        except (ValueError, TypeError, InvalidSignature):
            return False
