# Benchmark protocol

Use **the same machine**, room fixture size, number of concurrent clients, request bodies, and duration for both servers. Run a warmup before recording results. Keep the server and load generator on separate machines when measuring high throughput. Record CPU, peak RSS, and response error rate alongside latency and throughput. Test at several concurrency levels and increase until the p95 latency or error rate crosses your service objective.

For the matched-fixture trial in `RESULTS.md`, build Rustfire with `cargo build --release`. Run the pinned upstream Campfire commit with Ruby 3.4.10, its locked gems, Redis, and `RAILS_ENV=production DISABLE_SSL=1` on localhost. The upstream Puma configuration automatically chooses its worker count; leave `WEB_CONCURRENCY` and `RAILS_MAX_THREADS` unset to reproduce the packaged-app baseline. Create the first administrator in each app, then stop both servers and seed disposable databases:

```sh
python bench/seed.py campfire /path/to/once-campfire/storage/db/production.sqlite3 --count 10000
python bench/seed.py rustfire /path/to/rustfire/data/compare.db --count 10000
```

The seed script uses the same text and client message IDs in both databases. It refuses a nonempty message table unless `--replace` is supplied. After a write warmup, restore the original fixture before the measured run. Confirm both servers use SQLite WAL and `PRAGMA synchronous=NORMAL`, and that `GET /rooms/1/messages` returns the latest 40 messages with status 200, before recording throughput.

The source Campfire repository includes `test/performance/chatter.js` for WebSocket fanout. For equivalent HTTP trials, start each app separately on the same hardware and run:

```sh
go run bench/http.go -base http://127.0.0.1:3000 -cookie 'session_token=...' -room 1 -mode read -clients 32 -seconds 30
go run bench/http.go -base http://127.0.0.1:3000 -cookie 'session_token=...' -room 1 -mode write -clients 32 -seconds 30 -csrf '...'
go run bench/http.go -base http://127.0.0.1:3000 -cookie 'session_token=...' -room 1 -mode write -rich -clients 32 -seconds 30
```

The probe loads `authenticity_token` from the room HTML before write trials unless `-csrf` supplies one; both servers enforce it for authenticated writes. The `-rich` flag sends Trix HTML through Rustfire's sanitizer; use it when comparing rich-text writes. The workload is bounded by the client and SQLite on one machine; it does not by itself establish maximum user scale or WebSocket fanout capacity.

The WebSocket fanout probe uses one authenticated session, subscribes a chosen number of sockets to one room, posts messages, and measures deliveries from POST start to each socket. It exits nonzero for missed deliveries or early disconnects:

```sh
node bench/fanout.mjs --app rustfire --base http://127.0.0.1:3000 --cookie 'session_token=...' --room 1 --sockets 100 --messages 20
node bench/fanout.mjs --app campfire --base http://127.0.0.1:3001 --cookie 'session_token=...; _campfire_session=...' --csrf '...' --room 1 --sockets 100 --messages 20
```

The Campfire mode fetches the room's signed `RoomMessagesChannel` stream name before opening sockets. Obtain the CSRF token from the room HTML. Repeat at increasing socket counts, record server CPU and RSS, and run the load generator on another machine for capacity claims. Campfire's Turbo event bodies are currently much larger than Rustfire's JSON event bodies, so these trials do not represent feature-equivalent fanout work.

For a Rustfire-only direct-room reuse regression check, run `cargo build --release` and `python bench/direct_lookup.py --rooms 10000 --iterations 100`. It creates a disposable 10,000-room fixture, checks startup migration of participant keys, compares the indexed SQL lookup with Rustfire's previous grouped query, then times repeated HTTP reuse requests. It does not compare Campfire or measure concurrent capacity.

For a paired ping-reuse trial, run `cargo build --release` and `python bench/paired_direct_lookup.py --rooms 1000 --iterations 10`, then repeat with `--rooms 10000`. The script requires the pinned Campfire checkout, its locally installed Ruby 3.4.10 and locked production bundle, a source production SQLite database containing one room and two users, and a running Redis server. Paths can be supplied with `--campfire-repo`, `--ruby`, and `--bundle-path`. It copies the Campfire database into a temporary directory, clears its messages, seeds both apps with the same 51 users and four-person direct-room sets, and runs them serially with one HTTP client. It does not modify the source database. Campfire runs with one Puma worker and five threads; Rustfire runs as one process. Both apps return HTTP 302, but they still differ in room-list broadcast work, so this is a core-path comparison rather than a full-parity scale claim.
