# Decentralized Peer-to-Peer Chat Application

## Abstract

This project presents the design and implementation of a decentralized
peer-to-peer chat application in which each participant operates as an
autonomous network node. Unlike conventional messaging systems that route all
communication through a central application server, the proposed system
prefers direct TCP connections and distributes peer-discovery information
among participating nodes. Each node has a persistent, cryptographically bound
160-bit identifier and maintains a Kademlia-inspired routing table based on XOR
distance. Nodes may enter the network through a manually supplied bootstrap
peer or discover nearby participants through UDP announcements. The protocol
provides authenticated end-to-end encryption, delivery acknowledgements,
bounded message frames, retry behavior, and duplicate suppression. When a
direct route is unavailable because of NAT restrictions or temporary
disconnection, the node first attempts automatic gateway port mapping and then
uses an optional relay that stores and forwards ciphertext without access to
the message content. A minimal Electron client presents these mechanisms
through a conventional desktop chat interface. The project therefore provides a practical study of
socket programming, distributed routing, applied cryptography, node discovery,
NAT-aware communication, and resilient message delivery.

## Objectives

1. To design and implement a decentralized messaging architecture in which
   each participant acts as both a client and a server, enabling direct text
   communication without routing message content through a central chat
   service whenever the peers are mutually reachable.

2. To develop and evaluate a peer-discovery mechanism that combines manual
   bootstrapping, local-network announcements, cryptographically bound node
   identities, and Kademlia-inspired distributed lookup so that nodes can
   locate one another as network membership changes.

3. To provide confidential and reliable communication through authenticated
   encryption, delivery acknowledgements, retransmission, duplicate
   suppression, and encrypted store-and-forward relaying for NAT-restricted or
   temporarily offline nodes.

## Implemented scope

The prototype implements direct text messaging, persistent key-derived node
IDs, Ed25519 signatures, X25519 key agreement, ChaCha20-Poly1305 encryption,
TCP request/response framing, delivery acknowledgements, retry behavior,
duplicate suppression, manual bootstrap, UDP LAN discovery, iterative
XOR-distance node lookup, RFC 5389 endpoint observation, and an optional
UPnP IGD TCP mapping, a persistent ciphertext relay, and an Electron desktop
client with an isolated renderer. No authoritative peer directory exists.

The DHT component is intentionally focused on node discovery. It does not store
chat messages or arbitrary user data. A lookup asks the closest known nodes for
their closest records and progressively approaches the requested 160-bit ID.
This captures the central routing behavior of Kademlia while keeping the system
small enough to inspect and demonstrate in an academic project.

The relay is separate from the DHT. It is an explicitly configured fallback
for peers that cannot accept inbound TCP connections and for recipients that
are temporarily offline. Both participants make outbound relay connections,
while the relay persists only signed encrypted envelopes with bounded queue
size and expiry.

## Current limitations

Local-network discovery does not cross routers, and the implementation does not
perform direct ICE-style hole punching. A UPnP-compatible router can expose a
direct TCP endpoint; otherwise a configured public relay acts as the fallback
path. Although the relay cannot decrypt content, it can observe
communication metadata. The static X25519 design also lacks forward secrecy
following a long-term private-key compromise. Production deployment would
require hardened key storage, revocation and device-management procedures,
relay abuse controls, traffic-analysis countermeasures, and a formal security
review.
