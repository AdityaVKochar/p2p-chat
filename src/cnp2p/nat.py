from __future__ import annotations

import asyncio
from dataclasses import dataclass
import os
import socket
import struct
import time
from urllib.parse import urljoin
from urllib.request import Request, urlopen
from xml.etree import ElementTree
from xml.sax.saxutils import escape


MAGIC_COOKIE = 0x2112A442
BINDING_REQUEST = 0x0001
BINDING_RESPONSE = 0x0101
XOR_MAPPED_ADDRESS = 0x0020
MAPPED_ADDRESS = 0x0001


class StunError(ConnectionError):
    pass


class UpnpError(ConnectionError):
    pass


@dataclass(frozen=True, slots=True)
class PublicEndpoint:
    host: str
    port: int


@dataclass(slots=True)
class PortMapping:
    control_url: str
    service_type: str
    internal_host: str
    internal_port: int
    external_host: str
    external_port: int
    lease_duration: int
    created_at: float

    def renew(self) -> None:
        _add_port_mapping(
            self.control_url,
            self.service_type,
            self.internal_host,
            self.internal_port,
            self.external_port,
            self.lease_duration,
        )
        self.created_at = time.time()

    def delete(self) -> None:
        _soap_request(
            self.control_url,
            self.service_type,
            "DeletePortMapping",
            {
                "NewRemoteHost": "",
                "NewExternalPort": str(self.external_port),
                "NewProtocol": "TCP",
            },
        )


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


def _discover_igd_locations(timeout: float) -> list[str]:
    message = (
        "M-SEARCH * HTTP/1.1\r\n"
        "HOST: 239.255.255.250:1900\r\n"
        'MAN: "ssdp:discover"\r\n'
        "MX: 2\r\n"
        "ST: urn:schemas-upnp-org:device:InternetGatewayDevice:1\r\n\r\n"
    ).encode("ascii")
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(min(timeout, 0.4))
    locations: list[str] = []
    deadline = time.monotonic() + timeout
    try:
        sock.sendto(message, ("239.255.255.250", 1900))
        while time.monotonic() < deadline:
            try:
                response, _ = sock.recvfrom(8_192)
            except socket.timeout:
                continue
            headers: dict[str, str] = {}
            for line in response.decode("iso-8859-1", errors="replace").split("\r\n")[1:]:
                if ":" in line:
                    key, value = line.split(":", 1)
                    headers[key.strip().lower()] = value.strip()
            location = headers.get("location")
            if location and location not in locations:
                locations.append(location)
    except OSError as error:
        raise UpnpError(f"UPnP discovery failed: {error}") from error
    finally:
        sock.close()
    return locations


def _service_from_description(location: str, timeout: float = 3.0) -> tuple[str, str]:
    try:
        with urlopen(location, timeout=timeout) as response:
            root = ElementTree.fromstring(response.read(256_000))
    except (OSError, ElementTree.ParseError) as error:
        raise UpnpError(f"invalid UPnP device description: {error}") from error
    for service in root.iter():
        if service.tag.rsplit("}", 1)[-1] != "service":
            continue
        fields = {
            child.tag.rsplit("}", 1)[-1]: (child.text or "").strip()
            for child in service
        }
        service_type = fields.get("serviceType", "")
        if "WANIPConnection" in service_type or "WANPPPConnection" in service_type:
            control_url = fields.get("controlURL")
            if control_url:
                return urljoin(location, control_url), service_type
    raise UpnpError("gateway does not advertise a WAN connection service")


def _soap_request(
    control_url: str,
    service_type: str,
    action: str,
    arguments: dict[str, str],
    *,
    timeout: float = 4.0,
) -> bytes:
    argument_xml = "".join(
        f"<{name}>{escape(value)}</{name}>" for name, value in arguments.items()
    )
    body = (
        '<?xml version="1.0"?>'
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
        's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
        f"<s:Body><u:{action} xmlns:u=\"{service_type}\">{argument_xml}"
        f"</u:{action}></s:Body></s:Envelope>"
    ).encode("utf-8")
    request = Request(
        control_url,
        data=body,
        method="POST",
        headers={
            "Content-Type": 'text/xml; charset="utf-8"',
            "SOAPAction": f'"{service_type}#{action}"',
        },
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            return response.read(256_000)
    except OSError as error:
        raise UpnpError(f"UPnP {action} failed: {error}") from error


def _external_ip(control_url: str, service_type: str) -> str:
    response = _soap_request(control_url, service_type, "GetExternalIPAddress", {})
    try:
        root = ElementTree.fromstring(response)
        for element in root.iter():
            if element.tag.rsplit("}", 1)[-1] == "NewExternalIPAddress" and element.text:
                return str(element.text).strip()
    except ElementTree.ParseError as error:
        raise UpnpError("gateway returned malformed external-address data") from error
    raise UpnpError("gateway did not return an external IP address")


def _add_port_mapping(
    control_url: str,
    service_type: str,
    internal_host: str,
    internal_port: int,
    external_port: int,
    lease_duration: int,
) -> None:
    _soap_request(
        control_url,
        service_type,
        "AddPortMapping",
        {
            "NewRemoteHost": "",
            "NewExternalPort": str(external_port),
            "NewProtocol": "TCP",
            "NewInternalPort": str(internal_port),
            "NewInternalClient": internal_host,
            "NewEnabled": "1",
            "NewPortMappingDescription": "CNP2P encrypted chat",
            "NewLeaseDuration": str(lease_duration),
        },
    )


def _blocking_upnp_mapping(
    internal_host: str,
    internal_port: int,
    preferred_external_port: int | None,
    timeout: float,
) -> PortMapping:
    locations = _discover_igd_locations(timeout)
    if not locations:
        raise UpnpError("no UPnP Internet Gateway Device was found")
    errors: list[str] = []
    for location in locations:
        try:
            control_url, service_type = _service_from_description(location)
            external_host = _external_ip(control_url, service_type)
            starting_port = preferred_external_port or internal_port
            for external_port in range(starting_port, min(starting_port + 10, 65_536)):
                for lease_duration in (0, 3_600):
                    try:
                        _add_port_mapping(
                            control_url,
                            service_type,
                            internal_host,
                            internal_port,
                            external_port,
                            lease_duration,
                        )
                        return PortMapping(
                            control_url=control_url,
                            service_type=service_type,
                            internal_host=internal_host,
                            internal_port=internal_port,
                            external_host=external_host,
                            external_port=external_port,
                            lease_duration=lease_duration,
                            created_at=time.time(),
                        )
                    except UpnpError as error:
                        errors.append(str(error))
        except UpnpError as error:
            errors.append(str(error))
    detail = errors[-1] if errors else "gateway rejected the mapping"
    raise UpnpError(f"could not create a UPnP TCP mapping: {detail}")


async def create_upnp_tcp_mapping(
    internal_host: str,
    internal_port: int,
    *,
    preferred_external_port: int | None = None,
    timeout: float = 3.0,
) -> PortMapping:
    """Discover an IGD and map an external TCP port to this node."""
    return await asyncio.to_thread(
        _blocking_upnp_mapping,
        internal_host,
        internal_port,
        preferred_external_port,
        timeout,
    )
