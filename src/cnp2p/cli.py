from __future__ import annotations

import argparse
import asyncio
from datetime import datetime
from pathlib import Path
import shlex
import sys
import time

from .models import Peer
from .nat import StunError, discover_public_endpoint
from .node import NodeError, P2PNode
from .protocol import ProtocolError


HELP = """Commands:
  /id                         show this node's complete ID
  /peers                      list peers currently in the routing table
  /connect HOST:PORT          bootstrap through a known node
  /find NODE_ID               perform a DHT lookup using a full node ID
  /msg ID_OR_PREFIX MESSAGE   send a direct message
  /ping ID_OR_PREFIX          measure reachability and round-trip time
  /offline                    fetch queued messages from configured relays
  /nat                        ask the configured STUN server for the public UDP endpoint
  /help                       show this help
  /quit                       stop the node
"""


def parse_address(value: str) -> tuple[str, int]:
    if value.count(":") != 1:
        raise argparse.ArgumentTypeError("address must have the form HOST:PORT")
    host, raw_port = value.rsplit(":", 1)
    try:
        port = int(raw_port)
    except ValueError as error:
        raise argparse.ArgumentTypeError("port must be an integer") from error
    if not host or not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("address must contain a host and a port from 1 to 65535")
    return host, port


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run a CNP2P chat node")
    parser.add_argument("--name", required=True, help="display name advertised to peers")
    parser.add_argument("--host", default="0.0.0.0", help="local TCP bind address")
    parser.add_argument("--port", type=int, default=9000, help="local TCP port")
    parser.add_argument(
        "--advertise-host",
        help="address other peers should use (defaults to the detected LAN address)",
    )
    parser.add_argument(
        "--bootstrap",
        action="append",
        default=[],
        type=parse_address,
        metavar="HOST:PORT",
        help="known peer; may be specified more than once",
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=Path(".cnp2p"),
        help="directory containing the persistent node identity",
    )
    parser.add_argument(
        "--relay",
        action="append",
        default=[],
        type=parse_address,
        metavar="HOST:PORT",
        help="encrypted store-and-forward relay; may be specified more than once",
    )
    parser.add_argument(
        "--relay-server",
        action="store_true",
        help="allow this node to persist and forward encrypted envelopes",
    )
    parser.add_argument(
        "--stun",
        type=parse_address,
        metavar="HOST:PORT",
        help="RFC 5389 server used by /nat to observe the public UDP endpoint",
    )
    parser.add_argument("--no-lan", action="store_true", help="disable UDP LAN discovery")
    return parser


def render_peer(peer: Peer) -> str:
    age = max(0, int(time.time() - peer.last_seen))
    return f"{peer.short_id}  {peer.name:<16.16}  {peer.host}:{peer.port:<5}  seen {age}s ago"


async def run(args: argparse.Namespace) -> int:
    prompt_ready = asyncio.Event()

    def on_message(message: dict[str, object]) -> None:
        sender = message["sender"]
        assert isinstance(sender, Peer)
        sent_at = datetime.fromtimestamp(float(message["sent_at"])).strftime("%H:%M:%S")
        route = " via offline relay" if message.get("offline") else ""
        print(f"\n[{sent_at}] {sender.name} ({sender.short_id}){route}: {message['text']}")
        if prompt_ready.is_set():
            print("cnp2p> ", end="", flush=True)

    node = P2PNode(
        name=args.name,
        host=args.host,
        port=args.port,
        advertise_host=args.advertise_host,
        data_dir=args.data_dir,
        lan_discovery=not args.no_lan,
        relay_enabled=args.relay_server,
        relay_endpoints=args.relay,
        on_message=on_message,
    )
    try:
        await node.start()
    except OSError as error:
        print(f"Could not start node: {error}", file=sys.stderr)
        return 1

    print(f"CNP2P node {node.name} is listening on {node.advertise_host}:{node.port}")
    print(f"Node ID: {node.node_id}")
    print("LAN discovery:", "disabled" if args.no_lan else "enabled when supported")
    print("Encryption: Ed25519 + X25519 + ChaCha20-Poly1305")
    if args.relay_server:
        print(f"Relay server: enabled; ciphertext queue is in {args.data_dir / 'relay.sqlite3'}")
    elif args.relay:
        print("NAT/offline relay(s):", ", ".join(f"{host}:{port}" for host, port in args.relay))

    for host, port in args.bootstrap:
        try:
            peer = await node.bootstrap(host, port)
            print(f"Connected to {peer.name} ({peer.short_id}) at {host}:{port}")
        except (ConnectionError, NodeError, ValueError) as error:
            print(f"Could not bootstrap through {host}:{port}: {error}", file=sys.stderr)

    print("Type /help for commands.")
    prompt_ready.set()
    try:
        while True:
            try:
                raw = await asyncio.to_thread(input, "cnp2p> ")
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if not raw.strip():
                continue
            try:
                parts = shlex.split(raw)
            except ValueError as error:
                print(f"Invalid command: {error}")
                continue
            command = parts[0].lower()
            try:
                if command in {"/quit", "/exit"}:
                    break
                if command == "/help":
                    print(HELP)
                elif command == "/id":
                    print(node.node_id)
                elif command == "/peers":
                    peers = sorted(node.routing.all_peers(), key=lambda item: item.name.lower())
                    if not peers:
                        print("No peers are known yet.")
                    else:
                        print("ID        NAME              ADDRESS                 LAST CONTACT")
                        for peer in peers:
                            print(render_peer(peer))
                elif command == "/connect" and len(parts) == 2:
                    host, port = parse_address(parts[1])
                    peer = await node.bootstrap(host, port)
                    print(f"Connected to {peer.name} ({peer.short_id}); {len(node.routing)} peer(s) known.")
                elif command == "/find" and len(parts) == 2:
                    peer = await node.find_node(parts[1].lower())
                    print(render_peer(peer) if peer else "The node was not found.")
                elif command == "/msg" and len(parts) >= 3:
                    receipt = await node.send_message(parts[1], " ".join(parts[2:]))
                    if receipt.mode == "direct":
                        print(f"Encrypted message delivered and acknowledged ({receipt.message_id[:8]}).")
                    else:
                        print(
                            f"Recipient is unreachable; encrypted message queued at "
                            f"{receipt.relay} ({receipt.message_id[:8]})."
                        )
                elif command == "/ping" and len(parts) == 2:
                    peer = await node.resolve_peer(parts[1])
                    milliseconds = await node.ping(peer)
                    print(f"Reply from {peer.name} ({peer.short_id}) in {milliseconds:.1f} ms.")
                elif command == "/offline" and len(parts) == 1:
                    count = await node.fetch_offline_messages()
                    print(f"Fetched {count} new offline message(s).")
                elif command == "/nat" and len(parts) == 1:
                    if args.stun is None:
                        print("Configure an RFC 5389 server with --stun HOST:PORT first.")
                    else:
                        endpoint = await discover_public_endpoint(*args.stun)
                        print(f"STUN observed UDP endpoint {endpoint.host}:{endpoint.port}.")
                else:
                    print("Unknown or incomplete command. Type /help.")
            except (
                ConnectionError,
                NodeError,
                ProtocolError,
                StunError,
                ValueError,
                argparse.ArgumentTypeError,
            ) as error:
                print(f"Error: {error}")
    finally:
        prompt_ready.clear()
        await node.stop()
    return 0


def main() -> None:
    args = build_parser().parse_args()
    try:
        raise SystemExit(asyncio.run(run(args)))
    except KeyboardInterrupt:
        raise SystemExit(130) from None


if __name__ == "__main__":
    main()
