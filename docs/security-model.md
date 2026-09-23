# Security and Relay Model

## Identity and encryption

Every data directory contains one Ed25519 signing private key and one X25519
key-agreement private key. The 160-bit node ID is derived from the SHA-256
digest of the Ed25519 public key. The Ed25519 identity signs the X25519 public
key and relay capability, binding those values to the node ID.

For every chat message, the sender and recipient derive a shared secret with
X25519. HKDF-SHA256 derives a message-specific 256-bit key, and
ChaCha20-Poly1305 encrypts and authenticates the text and timestamp with a fresh
96-bit nonce. Ed25519 additionally signs the complete outer envelope. The
recipient verifies the signature before attempting decryption.

## Relay behavior

A relay accepts only correctly signed envelopes and stores their JSON
representation in SQLite. The encrypted text is opaque to the relay. Fetch and
delete operations must be signed by the recipient identity and expire after
five minutes, which prevents another node from removing the recipient's queue.
Messages expire after seven days, and each recipient queue is limited to 1,000
envelopes.

Both sender and recipient initiate outbound TCP connections to the relay. This
provides a usable fallback when either router blocks unsolicited inbound TCP
connections. It is relay-assisted traversal, not direct peer-to-peer hole
punching. A relay therefore needs a publicly reachable address or an
administrator-configured port mapping.

## NAT mapping

When enabled, the node discovers a local UPnP Internet Gateway Device and asks
it to map an external TCP port to the local listening socket. The public
endpoint is then advertised in peer records. Finite leases are renewed, and
the mapping is removed during clean shutdown. This mechanism depends on the
local gateway's access controls; it does not weaken message encryption, but it
does make the node's TCP listener reachable from the public endpoint. The
encrypted relay remains the fallback when no compatible gateway is available.

## Security boundaries

The relay and network observers can see node IDs, public keys, endpoints,
message sizes, and timing. They cannot read or silently alter valid message
content. A malicious relay can still drop, delay, replay, or refuse messages.
Duplicate IDs suppress replays during the active cache window.

Static X25519 identities make the project straightforward to study but do not
provide forward secrecy: compromise of a private key can expose previously
captured messages. A production design should use an audited session protocol
such as the Double Ratchet, secure operating-system key storage, key rotation,
device verification, and stronger relay abuse controls.
