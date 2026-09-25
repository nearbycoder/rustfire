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
- `RUSTFIRE_PUBLIC_URL` — public origin for invite, device sign-in, and message copy links, for example `https://chat.example.com`; otherwise request Host and the secure-cookie setting determine the origin, with the bind address as fallback when no request is available
- `RUSTFIRE_DISABLE_WEBHOOKS` — set to `true` to suppress outbound bot webhook delivery
- `RUSTFIRE_DISABLE_PUSH` — set to `true` to suppress outbound Web Push delivery
- `RUSTFIRE_VAPID_KEY_FILE` — persistent VAPID private-key path; defaults beside the SQLite database as a `.vapid.der` file. Keep this file when moving or restoring an installation so existing browser subscriptions remain valid.
- `RUSTFIRE_CAMPFIRE_SECRET_KEY_BASE` — optional original Campfire secret key base, used to issue and verify Rails mention, avatar, blob, and device-transfer IDs with Campfire's verifier keys. Keep it configured while imported messages or attachments use those IDs. Rustfire also accepts IDs signed with its own persistent keys.
- `RUSTFIRE_TRUSTED_PROXY_IPS` — comma-separated IPs of reverse proxies that append `X-Forwarded-For`; empty by default. Session IPs and bans use the direct peer unless it is listed here.

The server creates its SQLite schema at startup and enables WAL mode. Run behind a TLS proxy for network use. Back up the database and upload directory together.

## Implemented

- Initial account setup, password sign in and sign out, invite link registration and rotation; requested room links survive sign-in, the last visited room is restored, and sign-out revokes the current session and its device push subscription
- QR codes for invite and device sign-in links
- Open and private rooms, room membership and settings, direct-message rooms; private-room membership revisions preserve unchanged members' notification and unread state, and creators may remove themselves. Ping creation searches users as you type instead of loading the full account roster. Direct-room reuse uses an indexed participant key and a write transaction so concurrent requests create one ping, returning the upstream 302 redirect and sending each participant a rendered sidebar link.
- Optional administrator-only room creation
- Paginated message history with upward scrolling, links that open a room around a message, local day separators, five-minute message grouping, a return-to-latest control, posting, inline editing, deleting, a multi-file composer queue that sends each file as its own message, authenticated direct byte-range downloads, Campfire-format signed blob and media representation links, image thumbnails and video posters processed during upload, lightbox viewing, and video playback; attachment files and cached variants are removed with their message or room
- Message pages and reconnect refresh follow Campfire's creation-time ordering and new/edited selection, backed by indexed nanosecond creation and update timestamps; existing Rustfire and imported Campfire-format messages are backfilled at startup
- Room unread markers, per-room notification preference storage, and live sidebar updates when rooms are created, renamed, deleted, or membership changes; visible pings are ordered by recent room activity, and the sidebar offers direct-ping shortcuts for active users without an existing ping
- The source's invitation card in the original room, including its join URL, QR, copy and share controls, and 40-message cutoff; the mobile room keeps the invitation and composer in the source's positions
- SQLite FTS5 search with Campfire's Porter tokenizer, limited to rooms the user can access, with a ten-item recent-search history and full message results in the chat layout
- Bot creation and editing with Campfire-style multipart forms that save name, webhook URL, and optional avatar together; the bot list shows Campfire's per-room text and file-upload commands with working copy controls. Campfire-format bot token storage automatically migrates older Rustfire keys without changing their public URLs. Bot key rotation, deletion, message read/post/update/delete API, boost create/delete API, and outbound webhooks with Campfire's registered attachment MIME types (including non-200 responses), rich HTML text replies, and blank text replies are supported.
- Bot message-list JSON now uses Campfire's field set, ActionText wrapper for plain and simple rich bodies, and signed creator avatar URLs; a paired probe rejects semantic response differences on matched 40-message fixtures
- Built-in `/play` sound messages with the upstream audio and image assets
- Trix rich-text composition and editing with server-side HTML sanitization; formatted messages retain searchable plain text with Campfire-style paragraphs, line breaks, lists, and blockquotes. Pasted links can request Open Graph previews through a public-IP-only fetcher. Saved preview attachments are rendered from validated attributes, dropping invalid, address-based, and same-host link or image URLs.
- Mention autocomplete with Campfire's response fields and Rails-format signed mention IDs, verified Trix mention attachments, and bot webhook dispatch for selected bot mentions in regular rooms (plain `@name` text does not trigger a bot); webhook payloads convert Trix mention and link-preview figures into ActionText attachment HTML and use null direct-room names, older Rustfire IDs remain readable, and original Campfire IDs can be verified when its secret key base is configured
- Rails-format signed avatar URLs in autocomplete, including Campfire's timestamp version; signed routes verify their purpose and signature. Missing human avatars render the upstream initials and color pattern, missing bot avatars use the upstream asset, and uploaded avatars serve cached 512-pixel WebP variants.
- Boosts with signed booster avatars and names, owner-only reveal/delete controls, individually addressable markup, and append/remove live events. Paired boost append samples match Campfire's parsed element, attribute, and text content when given the same boost.
- Profile name/email/password/bio and avatar editing with source-style nested forms, notification controls, and stateless four-hour Rails-format device-transfer links; account name, room-creation setting, and logo forms using the source account routes and panel controls, 512/192-pixel PNG logo variants, custom CSS, an account roster grouped by administrators and members with the source's Turbo Stream page sequence and parsed user-row markup on matched fixtures, administrator role changes, deactivation and bans, and live message delivery over `/cable`; deactivation preserves direct-room history while revoking sessions and sockets
- Session and room access control
- CSRF checks for authenticated writes and public setup, sign-in, join, and device sign-in forms; same-origin WebSocket handshakes; sign-in limited to 10 attempts per IP over three minutes
- Room-scoped WebSocket fanout and message catch-up after reconnect, so sockets do not process events for unrelated rooms and missed messages can be recovered; timestamp-based room refresh also replaces edited messages missed while disconnected
- Campfire-format signed room stream names for `RoomMessagesChannel`, verified against room type and current membership; the browser subscribes with the signed name and applies Turbo append, replace, and remove actions for messages and individual boosts. Room, message, presentation, and boost target IDs follow Campfire's naming. Message roots carry Campfire-style timestamps, signed creator avatars, and an in-message day heading; metadata contains the room permalink and action menu with quick boost forms. Plain message presentation uses an ActionText-style `trix-content` block, and custom boost forms load into the new-boost frame on demand. Message edit frames match the sampled Campfire frame structure for plain messages and file, image, and video attachments; the room can save, close, and delete through those frames. The existing Rustfire room-ID subscriptions remain available for API clients. ActionCable welcome and three-second ping frames and `HeartbeatChannel` subscriptions are supported. Paired plain-message streams now have the same ordered element and attribute-name structure; values, other message types, styling, and behavior still require parity work.
- Room-scoped typing notifications, presence connection tracking, and live read/unread sidebar updates across sessions
- Web app manifest with source-shaped icon sizes, shortcuts, and screenshots, the upstream service-worker behavior, VAPID key management, browser push opt-in, subscription management, and delivery for disconnected users based on notification preference. The room bell opens notification-permission help when push is unavailable and cycles room notification levels after subscribing.

## Remaining parity work

- Exact Campfire HTML, CSS, Turbo and ActionCable protocol behavior
- Remaining ActionText rich-text behavior, inline attachments, media format and metadata edge cases, and exact link preview rendering
- Exact ActionText mention HTML and webhook triggering, full migration of existing Campfire content, full presence integration and notification behavior; exact upstream typing and unread behavior
- Complete PWA behavior and cross-browser push delivery validation
- Remaining webhook response edge cases and JSON API compatibility
- Remaining account/user administration and exact behavior for bans and profile/session-transfer edge cases
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

Upstream SVG icons, `campfire-icon.png`, `app-icon.png`, `app-icon-192.png`, PWA screenshots, and sound audio and images are copied from ONCE Campfire under its MIT license; see `LICENSE.upstream`. The Trix editor assets come from the upstream-locked MIT-licensed `action_text-trix` 2.1.19 gem; see `LICENSE.trix`. Rustfire's own code is distributed under the MIT license in `LICENSE`.
