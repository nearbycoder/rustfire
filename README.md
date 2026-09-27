# Rustfire

Rustfire is an independent, open-source Rust implementation of [ONCE Campfire](https://github.com/basecamp/once-campfire). It targets Campfire commit [`91d294f`](https://github.com/basecamp/once-campfire/commit/91d294f4a09f9bbe37f9548959bfcb43645678fb).

**Project status:** Rustfire is in progress. Core chat, rooms, search, accounts, bots, attachments, browser updates, and an offline Campfire importer are implemented, but it is not yet a 1:1 replacement. The [compatibility record](docs/COMPATIBILITY.md) lists implemented behavior and remaining gaps. [Benchmark methods and results](bench/RESULTS.md) document specific paired workloads; they do not establish an overall speed or capacity advantage at full feature parity.

Rustfire is not affiliated with or endorsed by 37signals.

## Quick start

The Docker image builds Rustfire and includes its runtime media tools:

```sh
docker build -t rustfire .
docker run --rm -p 3000:3000 -v rustfire-data:/data rustfire
```

Open <http://127.0.0.1:3000> to create the first administrator. Keep the `rustfire-data` volume: it holds the SQLite database, uploads, and signing keys.

For a local build, use Rust 1.97 or newer, the libvips development library and headers, OpenSSL development headers, and `pkg-config`. Media processing also uses `ffmpeg` and Poppler tools:

```sh
cargo run --release
```

The local server listens on `127.0.0.1:3000` by default. It creates its SQLite schema on first start.

## Configuration

| Variable | Purpose | Default |
| --- | --- | --- |
| `RUSTFIRE_ADDR` | Bind address | `127.0.0.1:3000` locally; `0.0.0.0:3000` in Docker |
| `RUSTFIRE_DB` | SQLite database path | `data/rustfire.db` locally; `/data/rustfire.db` in Docker |
| `RUSTFIRE_UPLOAD_DIR` | Uploaded files | `data/uploads` locally; `/data/uploads` in Docker |
| `RUSTFIRE_PUBLIC_URL` | Public origin for invite and device links | Derived from the request |
| `RUSTFIRE_SECURE_COOKIES` | Mark session cookies secure when served over HTTPS | Unset |
| `RUSTFIRE_DISABLE_WEBHOOKS` | Disable outbound bot webhooks | Unset |
| `RUSTFIRE_DISABLE_PUSH` | Disable outbound Web Push | Unset |

For network access, serve Rustfire behind a TLS proxy, set `RUSTFIRE_PUBLIC_URL` to the public HTTPS origin, and set `RUSTFIRE_SECURE_COOKIES=true`. Back up the database and upload directory together, along with the VAPID key file if push subscriptions are in use. The [compatibility record](docs/COMPATIBILITY.md#run) documents all supported settings and the webhook retry commands.

## Importing from Campfire

Rustfire includes an offline importer for a stopped Campfire installation. Back up Campfire first, then follow the [import instructions and limitations](docs/COMPATIBILITY.md#import-an-existing-campfire-installation). Test the imported copy before replacing a running service. The importer needs the original Campfire secret key base to preserve signed identifiers and sessions.

## Development and verification

```sh
cargo test
cargo build
node --check static/app.js
```

Paired compatibility and performance probes live in [`bench/`](bench/README.md); many require a separate checkout of the pinned Campfire source, its Ruby dependencies, and Redis. The older scripts in `tests/smoke.py` and `tests/websocket.mjs` have assertions for routes that changed during parity work and are not current CI gates. See [contributing guidelines](CONTRIBUTING.md) for how to report a mismatch or submit a fix.

## License and attribution

Rustfire code is available under the [MIT license](LICENSE). This repository also includes material from ONCE Campfire, Trix, and Surfguard under their respective MIT notices: [Campfire](LICENSE.upstream), [Trix](LICENSE.trix), and [Surfguard](LICENSE.surfguard). Upstream CSS, icons, images, sounds, and browser assets are included to support compatibility. See the [detailed asset attribution](docs/COMPATIBILITY.md#license).
