import asyncio
import socket
import struct
import threading
import unittest

from cnp2p.nat import BINDING_RESPONSE, MAGIC_COOKIE, XOR_MAPPED_ADDRESS, discover_public_endpoint


class NatTests(unittest.TestCase):
    def test_parses_rfc5389_xor_mapped_address(self) -> None:
        server = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        server.bind(("127.0.0.1", 0))
        server.settimeout(2)
        port = server.getsockname()[1]

        def respond() -> None:
            try:
                request, address = server.recvfrom(2_048)
                transaction_id = request[8:20]
                mapped_port = 54_321 ^ (MAGIC_COOKIE >> 16)
                cookie = struct.pack("!I", MAGIC_COOKIE)
                mapped_ip = socket.inet_aton("203.0.113.7")
                encoded_ip = bytes(left ^ right for left, right in zip(mapped_ip, cookie, strict=True))
                value = b"\x00\x01" + struct.pack("!H", mapped_port) + encoded_ip
                attribute = struct.pack("!HH", XOR_MAPPED_ADDRESS, len(value)) + value
                response = (
                    struct.pack(
                        "!HHI12s",
                        BINDING_RESPONSE,
                        len(attribute),
                        MAGIC_COOKIE,
                        transaction_id,
                    )
                    + attribute
                )
                server.sendto(response, address)
            finally:
                server.close()

        thread = threading.Thread(target=respond)
        thread.start()
        endpoint = asyncio.run(discover_public_endpoint("127.0.0.1", port))
        thread.join(timeout=2)

        self.assertEqual(endpoint.host, "203.0.113.7")
        self.assertEqual(endpoint.port, 54_321)


if __name__ == "__main__":
    unittest.main()
