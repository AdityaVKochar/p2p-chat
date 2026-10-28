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

Version 0.4 adds a `kind` field inside the encrypted payload (`text`, `file`,
or `receipt`). The outer envelope, signature, and associated data are
unchanged. Text messages still omit `kind`, so earlier versions can read them.

## File transfer

A file offer is an ordinary sealed envelope with `kind: "file"`, sent in a
`FILE_OFFER` frame. It carries the file name, size, SHA-256 digest, chunk
size, and a fresh random 256-bit file key. That key is only ever transmitted
inside the end-to-end encrypted payload.

Each 32 KiB chunk is encrypted with ChaCha20-Poly1305 under the file key and
a fresh nonce. The associated data binds the transfer ID, the chunk index, and
the recipient ID, so a chunk cannot be replayed at another position, in
another transfer, or to another recipient. Chunk frames are not signed. Only
the two participants hold the file key, and the AEAD tag already
authenticates them. Before a file is published, the recipient checks every
chunk's length and the digest of the whole file. A failed check deletes the
partial file.

Untrusted file names are reduced to a single path component, stripped of
control and reserved characters, and given a unique name inside the
attachments directory. The desktop app serves inline previews through a
`cnp2p-media:` protocol that refuses any path outside that directory. Open,
reveal, and save actions apply the same check. Only raster image types are
previewed; SVG is never rendered.

Files are never placed on the relay. If the recipient is unreachable, the
sender keeps the transfer and retries when the peer is seen again.

## Receipts and presence

A direct `ACK` proves the recipient decrypted a message. Relayed messages are
confirmed with an encrypted `delivered` receipt. When enabled, an encrypted
`read` receipt is sent when the conversation is opened. Receipts are
end-to-end encrypted, but their timing tells the sender, and any relay that
carries them, when the recipient was active. Users who object can disable
read receipts in Settings. Delivered receipts are always sent.

Presence ("online" and "last seen") is inferred locally from direct traffic,
LAN announcements, and periodic pings to conversation partners. No presence
data is published to the network.

## Local storage

`history.sqlite3` stores message text, contact records, nicknames,
verification marks, and file-transfer state, including per-transfer file keys.
`attachments/` stores received files and copies of sent files. Neither is
encrypted at rest. Their protection relies on operating-system account
security, like `identity.json`.

## Contact verification

The safety number is a 60-digit value derived with SHA-512 from both signing
public keys, sorted so both users see the same digits. Comparing it out of
band (in person or on a call) detects a man-in-the-middle who substituted a
key during the first contact. The "verified" mark is local only.

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
encrypted local history, and stronger relay abuse controls.
