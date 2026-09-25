# Preliminary Rustfire measurements

Machine: local 32-logical-CPU Linux host. Rustfire was built with `cargo build --release`, using one server process and SQLite WAL on local storage. The Go load generator ran on the same host. Each trial used 32 concurrent keep-alive HTTP clients for 10 seconds, against a room initially containing 323,639 messages. Reads returned the latest 40 messages. Writes posted small text messages and updated the SQLite FTS5 index.

| Workload | Throughput | p50 | p95 | p99 | Errors |
|---|---:|---:|---:|---:|---:|
| `GET /rooms/1/messages` | 10,738 successful requests/s | 2.91 ms | 4.14 ms | 4.88 ms | 0 |
| `POST /rooms/1/messages` | 9,759 successful requests/s | 0.18 ms | 18.34 ms | 53.58 ms | 0 |

The 10-second write trial inserted 98,244 messages. The database contained 421,883 messages afterward. This result predates later profile, avatar, account, and API additions and should be rerun after parity work. These are **Rustfire-only** measurements, not a Campfire comparison or a maximum scale claim. The load generator shared CPU and storage with the server, and Rustfire still lacks some work performed by Campfire when posting messages.

The older HTTP results above used a different fixture and should not be compared with Campfire. A later matched-fixture trial appears below.

## Rustfire WebSocket fanout trial

After the account CSS, bot administration, and room autocomplete additions, the release build was run on the same 32-logical-CPU host. `bench/fanout.mjs` connected one authenticated session to one room on localhost, then posted 20 small messages in sequence. It timed each delivery from the start of its POST. All messages reached every connected socket within the 30-second deadline.

| Sockets | Expected / received deliveries | p50 | p95 | p99 | Trial time |
|---:|---:|---:|---:|---:|---:|
| 100 | 2,000 / 2,000 | 5.20 ms | 11.40 ms | 39.16 ms | 0.13 s |
| 500 | 10,000 / 10,000 | 9.29 ms | 21.90 ms | 38.00 ms | 0.20 s |
| 2,000 | 40,000 / 40,000 | 30.92 ms | 53.13 ms | 60.77 ms | 0.54 s |
| 5,000 | 100,000 / 100,000 | 70.04 ms | 109.55 ms | 125.26 ms | 1.18 s |
| 10,000 | 200,000 / 200,000 | 137.59 ms | 182.91 ms | 198.77 ms | 2.25 s |

The server's resident set was about 1.36 GB after the 10,000-socket run. The trials used the same machine for the client and server, one account and room, and a short burst of 20 posts. They do **not** establish sustained capacity, memory per long-lived socket, or an advantage over Campfire. In particular, Rustfire does not yet perform Campfire's full ActionCable, Turbo, presence, notification, or webhook work for each message.

## Preliminary Campfire comparison

The upstream commit was run with locally built Ruby 3.4.10, Rails 8.2.0.alpha, Redis 7.2.11, and its production configuration with `DISABLE_SSL=1` for localhost. Both apps used SQLite WAL with `synchronous=NORMAL`, one authenticated administrator, one room, and 10,000 text messages at the start of each measured trial. Campfire used its default 22 Puma workers with five threads each. Rustfire used one release-build process and a pool of 32 SQLite connections. The Go client used 32 keep-alive workers on the same 32-logical-CPU host, a five-second read warmup or a two-second write warmup, then a ten-second measurement. Write fixtures were reset to 10,000 messages after warmup. No request errors occurred in the measured trials.

| Workload | Campfire default | Rustfire | Campfire p95 | Rustfire p95 |
|---|---:|---:|---:|---:|
| Read latest 40 messages as HTML | 1,974 successful requests/s | 10,429 successful requests/s | 26.39 ms | 3.96 ms |
| Post a small text message as Turbo Stream | 607 successful requests/s | 5,667 successful requests/s | 119.10 ms | 22.37 ms |

Campfire's 40-message HTML response was roughly 360–410 KB, while Rustfire's was about 21 KB. A single Campfire post returned about 9.3 KB and Rustfire about 0.6 KB. Campfire renders richer markup and performs additional message work that Rustfire has not implemented. These results show **current core-path throughput**, not a one-for-one speedup. Campfire's 23 Puma processes consumed about 3.8 GiB of summed proportional set size after the trials; Rustfire's one process consumed about 57 MiB. These are point-in-time memory readings, not peak or sustained-load measurements.

A one-worker Campfire run with 32 threads reached 133 reads/s and 80 writes/s. Its default multiworker setup above is the appropriate packaged-app baseline. More work is required before claiming that Rustfire is faster or supports more users at full feature parity: implement the missing per-message features, compare equivalent response content, run a concurrency and sustained-load sweep, and measure WebSocket fanout on both applications.

After adding account bans, session transfers, and an IP-ban check on writes, the Rustfire release server was rebuilt and rerun with the same 10,000-message fixture, 32 clients, warmups, and ten-second measurement. It served **8,969 reads/s (p95 4.49 ms)** and **5,901 writes/s (p95 19.47 ms)**, with zero errors. Those figures superseded Rustfire's two HTTP values in the table at that stage. The upstream baseline was not rebuilt or rerun in this follow-up, so normal run-to-run variation applies. The feature and response differences described above still apply.

After adding typing, presence, and live read/unread channels, the release build was measured again under that protocol. It served **9,378 reads/s (p95 4.55 ms)** and **5,745 writes/s (p95 19.57 ms)**, with zero errors. The Campfire baseline remains the earlier 1,974 reads/s and 607 writes/s; it was not rerun for this follow-up. Neither result is a full feature-parity comparison.

With Trix rich-text storage and server-side sanitization added, a later Rustfire build served **8,816 reads/s (p95 4.57 ms)** and **5,234 rich-text writes/s (p95 33.44 ms)**, again with zero errors and the same 10,000-message fixture, warmups, and 32 clients. The rich write body was `<div>benchmark N</div>` and used the new `-rich` option. The older Campfire write trial posted plain form text, which ActionText processed; the two apps still return different markup and perform different side effects, so these are indicative core-path measurements only.

After session-bound CSRF enforcement and same-origin WebSocket checks were added, the same Rustfire release protocol yielded **8,120 reads/s (p95 4.88 ms)** and **5,673 rich-text writes/s (p95 21.56 ms)**, with zero errors. The write probe fetched and submitted the CSRF token. These are the latest Rustfire HTTP numbers; the change from the prior run should be treated as run-to-run variation, not as an optimization result. The Campfire baseline remains older and the feature gaps remain.

### Paired live delivery bursts

The same raw WebSocket client subscribed to Rustfire's room channel or Campfire's signed `RoomMessagesChannel` stream. Each run used one authenticated account, one room, 20 sequential small posts, and a 30-second delivery deadline. Both servers and the load generator shared one host. Every expected event was received in all paired trials.

| Sockets | Deliveries per app | Campfire p95 | Rustfire p95 | Campfire trial time | Rustfire trial time |
|---:|---:|---:|---:|---:|---:|
| 100 | 2,000 | 23.53 ms | 18.85 ms | 0.36 s | 0.08 s |
| 500 | 10,000 | 32.06 ms | 19.19 ms | 0.60 s | 0.16 s |
| 2,000 | 40,000 | 97.20 ms | 41.71 ms | 1.57 s | 0.44 s |
| 5,000 | 100,000 | 169.15 ms | 109.67 ms | 3.24 s | 1.03 s |
| 10,000 | 200,000 | 395.05 ms | 172.42 ms | 6.68 s | 2.15 s |
| 15,000 | 300,000 | 537.41 ms | 249.45 ms | 9.86 s | 3.17 s |

Separate boundary probes also delivered every event: Campfire at 7,000 sockets had p95 231.83 ms, while Rustfire at 20,000 sockets had p95 329.67 ms. For a provisional 250 ms p95 target, the largest sampled passing counts were 7,000 and 15,000 sockets respectively. These are single short bursts, so they do not establish a reliable capacity ratio. The Turbo markup and JSON events differ substantially in size and work; Rustfire did not yet have typing, presence, or live unread broadcasts at the time of this sweep, and still lacks push delivery and webhooks. After the 20,000-socket Rustfire run and 7,000-socket Campfire run, the processes retained approximately 2.6 GiB and 6.7 GiB of summed PSS respectively; those are different run sizes and point-in-time readings.

The table predates Rustfire's typing, presence, live unread, push, and webhook work. A later 100-socket smoke probe against an intermediate release build delivered all 2,000 room-message events with no misses (p95 13.70 ms); it is a regression check, not a replacement for the full paired sweep. Exact ActionCable and Turbo behavior remains missing.

## Build with bot webhooks and QR links

The release build with public-form CSRF checks, QR links, message-page status parity, and outbound bot webhooks was measured with the same 10,000-message fixture and 32 local clients. A fresh disposable SQLite database was created for this run. After a five-second read warmup, the ten-second read trial served **8,315 requests/s (p95 4.77 ms)** with no errors. After a two-second rich-text write warmup and a fixture reset, the ten-second write trial served **4,553 requests/s (p95 33.63 ms)** with no errors. Repeated writes against the older disposable database were in the 4,346–4,457 requests/s range.

The write rate is lower than the previous 5,673 requests/s result. Ordinary messages without `@` skip webhook queries, and subsequent paired trials did not show a consistent webhook penalty. With the same later binary on fresh fixtures on the project disk, delivery disabled measured **4,672 writes/s (p95 33.56 ms)** and enabled measured **5,601 writes/s (p95 21.75 ms)**. On a temporary memory-backed filesystem, the same two modes measured **7,978** and **7,927 writes/s** respectively, both at about 18.5 ms p95. The different storage and run conditions materially affect throughput; none of these short trials isolates a code-level performance change. Campfire was not rerun. Rich-text response content, mention handling, and other side effects still differ, so this is not a feature-parity speed comparison.

## Paired run after Web Push and link previews

The release build with subscription management, VAPID encryption, link-preview fetching, local timestamps, and the updated message controls was measured against pinned Campfire on the same host. Both databases were seeded with the same 10,000 text messages; the servers were stopped for seeding. Campfire used its default 22 Puma workers and five threads per worker. Rustfire used one process with `RUSTFIRE_DISABLE_PUSH=1` and `RUSTFIRE_DISABLE_WEBHOOKS=1`; the fixture had no subscriptions or bots. The Go probe used 32 keep-alive clients and ten-second runs. Each write sent the same `<div>benchmark N</div>` body. There was no write warmup; both servers had served the read run first. All requests succeeded.

| Workload | Campfire | Rustfire | Campfire p95 | Rustfire p95 |
|---|---:|---:|---:|---:|
| Read latest 40 messages | 1,858.6 requests/s | 8,087.4 requests/s | 32.38 ms | 5.45 ms |
| Post rich-text message | 388.8 requests/s | 4,872.6 requests/s | 175.58 ms | 33.57 ms |

The read responses were 364,001 bytes for Campfire and 77,560 bytes for Rustfire. Campfire's 24 Puma processes (master plus workers) had about 3,947 MiB of summed PSS after the run; Rustfire used about 61 MiB. These are point-in-time readings, not peak memory. Campfire's authenticated benchmark user was a member while Rustfire's was an administrator. The applications still differ in markup, ActionText processing, Turbo broadcasts, notification work, and other side effects. This is a current core-path measurement, **not a 1:1 speedup or maximum-scale result**. Both databases were restored from pre-run backups afterward.

This paired run predates later attachment variants, inline message editing, browser history paging, session navigation, timestamp-based room refresh, room/message touch behavior, transactional room-membership revisions, and API response adjustments. Rerun it after the remaining parity work before drawing performance conclusions.

## Rustfire regression check after preview validation

The current release build was run on a disposable copy of the browser-test database, reseeded to 10,000 messages in room 1. Push and webhook delivery were disabled, and there were no bot subscriptions in the fixture. The Go probe and server shared one host, and an unrelated Puma process was also running on that host. With 32 keep-alive clients and ten-second runs, the latest-40 read reached **9,151.9 requests/s** (p95 **4.44 ms**), and rich-text posts reached **6,273.1 requests/s** (p95 **20.75 ms**). Each run had zero response errors. The write trial began after the read trial and inserted 62,923 messages; the database ended with 72,923. The server's post-run PSS was about 35 MiB, a point-in-time reading rather than peak memory. This is a Rustfire-only regression check, with no corresponding Campfire run under this exact binary and fixture, so it does not establish a speedup or scale ratio.

## Room-list update fanout regression check

After adding live room-list updates, the release build served a disposable two-user, 52-message fixture with push and webhooks disabled. The localhost probe opened 2,000 sockets subscribed to one room, then posted 20 messages in sequence. It received all 40,000 expected deliveries with no early disconnects or unexpected frames; p50 was **38.27 ms**, p95 **65.12 ms**, and p99 **72.15 ms**. The burst took **0.59 s**. This is a Rustfire-only check on a small fixture, not a feature-equivalent Campfire comparison or sustained capacity measurement.

## Direct-room reuse lookup regression check

After adding an indexed participant key and serializing direct-room creation, `bench/direct_lookup.py --rooms 10000 --iterations 100` seeded 10,000 four-person direct rooms in a disposable SQLite database. Restarting Rustfire populated all 10,000 keys. For a room near the end of the fixture, the indexed SQL lookup had **0.002 ms median and p95**; Rustfire's previous grouped-membership query had **4.258 ms median** and **4.676 ms p95** over 100 sequential lookups. Reusing that room through the current localhost HTTP endpoint had **0.064 ms median** and **0.270 ms p95**. This is a Rustfire-only single-client microbenchmark. It is not a matched Campfire comparison or a measure of concurrent capacity.

A rerun after the later deactivation changes measured **0.001 ms** indexed median (**0.002 ms p95**), **3.388 ms** previous-query median (**3.439 ms p95**), and **0.090 ms** current HTTP median (**0.172 ms p95**). The variation reflects short local runs; neither run isolates an end-to-end Campfire comparison.

## Paired direct-room reuse trial

`bench/paired_direct_lookup.py` ran pinned Campfire commit `91d294f4a09f9bbe37f9548959bfcb43645678fb` and the Rustfire release build serially on the same local host. Each disposable SQLite fixture had 51 active users, one open room, no messages, and the same four-person direct-room membership sets. The chosen ping was near the end of each fixture. One keep-alive client sent the same selected participant IDs, with two warmup requests followed by ten measured requests. Neither app created a new room; both returned an empty redirect to the existing ping. Campfire used one Puma worker with five threads; Rustfire used one process.

| Existing direct rooms | Rustfire median / p95 | Campfire median / p95 |
|---:|---:|---:|
| 1,000 | 0.297 / 0.549 ms | 345.953 / 375.839 ms |
| 10,000 | 0.162 / 0.386 ms | 3,466.264 / 3,633.413 ms |

The selected-room lookup is much faster in Rustfire at these fixture sizes. This is a **single-client ping-reuse comparison**, not proof of full-app speed or user capacity. Both apps returned HTTP 302 with an empty body. Campfire renders and publishes Turbo sidebar broadcasts for each participant on reuse; Rustfire renders a per-user room link and sends a lighter room-list event, and no clients were subscribed during the trial. The apps therefore still perform different work. The load generator and servers shared the host, and no sustained concurrency sweep was run.
