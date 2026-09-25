# Rustfire

An **in-progress** Rust port of [ONCE Campfire](https://github.com/basecamp/once-campfire). The compatibility target is upstream commit `91d294f4a09f9bbe37f9548959bfcb43645678fb`. This repository is **not yet a 1:1 replacement** and no speed or capacity advantage at feature parity has been established.

## Run

```sh
cargo run --release
# Visit http://127.0.0.1:3000 and create the first administrator.
```

With Docker:

```sh
docker build -t rustfire .
docker run --rm -p 3000:3000 -v rustfire-data:/data rustfire
```

Configuration:

- `RUSTFIRE_ADDR` — bind address, default `127.0.0.1:3000`
- `RUSTFIRE_DB` — SQLite file, default `data/rustfire.db`
- `RUSTFIRE_UPLOAD_DIR` — uploaded files, default `data/uploads`
- `RUSTFIRE_SECURE_COOKIES` — set to `true` when serving over HTTPS
- `RUSTFIRE_PUBLIC_URL` — public origin for invite and device sign-in links, for example `https://chat.example.com`; otherwise request Host and the secure-cookie setting determine the origin
- `RUSTFIRE_DISABLE_WEBHOOKS` — set to `true` to suppress outbound bot webhook delivery
- `RUSTFIRE_DISABLE_PUSH` — set to `true` to suppress outbound Web Push delivery
- `RUSTFIRE_VAPID_KEY_FILE` — persistent VAPID private-key path; defaults beside the SQLite database as a `.vapid.der` file. Keep this file when moving or restoring an installation so existing browser subscriptions remain valid.
- `RUSTFIRE_TRUSTED_PROXY_IPS` — comma-separated IPs of reverse proxies that append `X-Forwarded-For`; empty by default. Session IPs and bans use the direct peer unless it is listed here.

The server creates its SQLite schema at startup and enables WAL mode. Run behind a TLS proxy for network use. Back up the database and upload directory together.

## Implemented

- Initial account setup, password sign in and sign out, invite link registration and rotation; requested room links survive sign-in, the last visited room is restored, and sign-out revokes the current session and its device push subscription
- QR codes for invite and device sign-in links
- Open and private rooms, room membership and settings, direct-message rooms; private-room membership revisions preserve unchanged members' notification and unread state, and creators may remove themselves. Direct-room reuse uses an indexed participant key and a write transaction so concurrent requests create one ping, returning the upstream 302 redirect and sending each participant a rendered sidebar link.
- Optional administrator-only room creation
- Paginated message history with upward scrolling, links that open a room around a message, local day separators, five-minute message grouping, a return-to-latest control, posting, inline editing, deleting, file upload, authenticated byte-range downloads, cached image thumbnails, lightbox viewing, and video playback with generated posters; attachment files and cached variants are removed with their message or room
- Room unread markers, per-room notification preference storage, and live sidebar updates when rooms are created, renamed, deleted, or membership changes; visible pings are ordered by recent room activity
- SQLite FTS5 search limited to rooms the user can access, with recent searches
- Bot creation, renaming, avatar editing, key rotation, deletion, message read/post/update/delete API, boost create/delete API, and outbound webhooks with text or common image/PDF replies
- Built-in `/play` sound messages with the upstream audio and image assets
- Trix rich-text composition and editing with server-side HTML sanitization; formatted messages retain searchable plain text. Pasted links can request Open Graph previews through a public-IP-only fetcher. Saved preview attachments are rendered from validated attributes, dropping invalid, address-based, and same-host link or image URLs.
- Mention autocomplete in the composer, validated mention IDs on messages, and bot webhook dispatch for selected bot mentions
- Basic boosts, profile name/email/password/bio and avatar editing, account logos and custom CSS, administrator role changes, deactivation and bans, four-hour session transfer links, live message delivery over `/cable`; deactivation preserves direct-room history while revoking sessions and sockets
- Session and room access control
- CSRF checks for authenticated writes and public setup, sign-in, join, and device sign-in forms; same-origin WebSocket handshakes; sign-in limited to 10 attempts per IP over three minutes
- Room-scoped WebSocket fanout and message catch-up after reconnect, so sockets do not process events for unrelated rooms and missed messages can be recovered; timestamp-based room refresh also replaces edited messages missed while disconnected
- Room-scoped typing notifications, presence connection tracking, and live read/unread sidebar updates across sessions
- Web app manifest and service-worker endpoints, VAPID key management, browser push opt-in, subscription management, and delivery for disconnected users based on notification preference

## Remaining parity work

- Exact Campfire HTML, CSS, Turbo and ActionCable protocol behavior
- Remaining ActionText rich-text behavior, inline attachments, exact image/video variant behavior, and exact link preview rendering
- Exact ActionText mention rendering and webhook triggering, full presence integration and notification behavior; exact upstream typing and unread behavior
- Complete PWA behavior and cross-browser push delivery validation
- Full webhook response/MIME behavior and JSON API compatibility
- Remaining account/user administration and exact behavior for bans and session transfer
- Remaining small interaction features
- Content Security Policy and broader security review
- Full upstream route and test compatibility

See `bench/README.md` for the measurement procedure and `bench/RESULTS.md` for the preliminary Campfire comparison. The apps still produce different response content and side effects, so these measurements do not establish a speed or capacity advantage at feature parity.

## Checks

```sh
cargo check
cargo build
python tests/smoke.py
node tests/websocket.mjs
```

The smoke tests start isolated servers and databases, then check setup, chat, attachments, search, private-room isolation, registration, bot posting, and live WebSocket delivery.

## License

Upstream SVG icons, `campfire-icon.png`, and sound audio and images are copied from ONCE Campfire under its MIT license; see `LICENSE.upstream`. The Trix editor assets come from the upstream-locked MIT-licensed `action_text-trix` 2.1.19 gem; see `LICENSE.trix`. Rustfire's own code is distributed under the MIT license in `LICENSE`.
