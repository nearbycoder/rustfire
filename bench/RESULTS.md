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
| 1,000 | 0.111 / 0.154 ms | 358.691 / 388.217 ms |
| 10,000 | 0.380 / 0.598 ms | 3,362.606 / 3,562.189 ms |

The selected-room lookup is much faster in Rustfire at these fixture sizes. This is a **single-client ping-reuse comparison**, not proof of full-app speed or user capacity. These figures include a user lookup that matches Campfire's filtering of selected IDs. Both apps returned HTTP 302 with an empty body. At the time of these runs, Campfire rendered and published Turbo sidebar broadcasts for each participant on reuse, while Rustfire sent its lighter room-list event. No clients were subscribed. The apps therefore performed different work. The load generator and servers shared the host, and no sustained concurrency sweep was run.

After Rustfire's direct-room link and signed Turbo prepend markup matched Campfire on the paired sidebar probe, the same ping-reuse trial was repeated. At **1,000** existing direct rooms, Rustfire measured **0.422 / 0.548 ms median / p95** versus Campfire **368.354 / 404.535 ms**. At **10,000** rooms, Rustfire measured **0.522 / 1.068 ms** versus Campfire **3,419.731 / 3,580.493 ms**. Both apps now render the source-shaped link and emit a per-user Turbo prepend event on reuse; Rustfire additionally keeps its custom room-list event. The trial still had no subscribed sockets, and the source publishes to Redis while Rustfire's in-process hub skips fanout when nobody is subscribed. These are single-client lookup and render comparisons, not sustained capacity results.

## Paired user autocomplete trial

`bench/paired_autocomplete.py` used pinned Campfire and the Rustfire release build with disposable fixtures containing the same active users. One authenticated keep-alive client queried names containing `User 3`; both apps returned the same 20 user names and IDs, with `name`, `value`, `avatar_url`, and `sgid` fields. The servers shared a signing secret, and the probe verified equal mention IDs and equal avatar URL paths and timestamp versions. URL origins differ because the servers use different ports. Each server ran separately on the same host. Two warmup requests preceded 30 measured requests. Campfire used one Puma worker with five threads and Rustfire one process.

| Active users | Rustfire median / p95 | Campfire median / p95 | Rustfire / Campfire JSON bytes |
|---:|---:|---:|---:|
| 1,000 | 0.518 / 1.198 ms | 6.053 / 16.962 ms | 7,526 / 7,526 |
| 10,000 | 0.982 / 1.348 ms | 10.252 / 19.013 ms | 7,642 / 7,642 |

The JSON response sizes are now equal, and the returned fields match after normalizing the different URL origins. A later run of the same fixture also fetched one linked initials SVG from each app and confirmed equal initials and background color. The images' exact bytes and uploaded-avatar behavior were not part of the timed probe. Short-run variation affects latency. This is a **single-client endpoint comparison**, not a feature-equivalent full-app speedup or a user-capacity result. The probe and servers shared the host; a separate short concurrent sweep follows.

### Paired concurrent autocomplete probe

The same 10,000-user fixture was probed with `--iterations 30 --clients 1 8 32 --seconds 3`. Each thread kept one HTTP connection, performed two warmup requests, then checked every measured response against its warmup body. Rustfire and one-worker, five-thread Campfire ran serially on the same host. Both returned the same verified fields, and all measured requests succeeded.

| Clients | Rustfire requests/s | Campfire requests/s | Rustfire p95 | Campfire p95 | Errors |
|---:|---:|---:|---:|---:|---:|
| 1 | 1,596.8 | 229.0 | 0.747 ms | 6.814 ms | 0 / 0 |
| 8 | 3,966.1 | 253.7 | 3.136 ms | 33.588 ms | 0 / 0 |
| 32 | 2,164.2 | 170.0 | 16.756 ms | 220.856 ms | 0 / 0 |

Both rates fell at 32 clients under this configuration. These are three-second endpoint trials with a Python client on the server host, not sustained capacity measurements. Campfire was limited to one Puma worker. The generated avatar images, related requests, and full-app work are outside this trial, so it does not establish a full-app speed or scale advantage.

### Sustained autocomplete sweep with packaged Campfire workers

The 10,000-user autocomplete fixture above was rerun with the checked Go keep-alive client, Campfire at 22 Puma workers, and each server running separately. Every measured JSON response had the same bytes as its server's warmed response; cross-app checks confirmed the same 20 IDs, names, signed mention IDs, avatar paths, avatar SVG initials and colors, and 7,642-byte response size. Each 15-second trial had zero response errors. Process-tree peak PSS was sampled once per second on the shared load-generator/server host.

The initial Rustfire release build used a 32-connection SQLite pool and computed 20 mention and avatar signatures for each request. A 15-second run plateaued near 2.3k requests/s from 32 to 128 clients, similar to Campfire's rate at high concurrency, though Rustfire used much less memory. Rustfire then cached deterministic autocomplete signatures and avatar paths by user ID and update timestamp. A four-connection global SQLite pool improved this endpoint but constrained another read path, so the final build retains the 32-connection pool and limits autocomplete to four simultaneous requests. With that setting, the follow-up run measured:

| Clients | Rustfire requests/s | Campfire requests/s | Rustfire p95 | Campfire p95 | Rustfire / Campfire peak PSS |
|---:|---:|---:|---:|---:|---:|
| 32 | 5,364.0 | 2,206.3 | 7.34 ms | 28.16 ms | 22.4 / 2,022.5 MiB |
| 128 | 5,144.0 | 2,322.3 | 28.07 ms | 82.74 ms | 28.3 / 2,041.2 MiB |
| 256 | 5,124.8 | 2,317.0 | 55.72 ms | 143.84 ms | 34.7 / 2,048.2 MiB |
| 512 | 5,114.1 | 1,940.8 | 106.91 ms | 349.14 ms | 45.5 / 2,109.9 MiB |

At a provisional 250 ms p95 target, the largest sampled passing client counts were 512 for Rustfire and 256 for Campfire. These are endpoint concurrency samples, not maximum capacity: the same host ran the client and server, the response uses cached identities, both apps were probed serially, PSS samples can miss peaks, and user updates, room-scoped autocomplete, other endpoints, long-lived sockets, and full-app side effects were outside the measured window. A separate check changed and restored one user's update timestamp while Rustfire was running and verified that the cached avatar version changed and the original response returned after restoration. Campfire's per-request work and framework memory differ. The result supports a faster, lower-memory autocomplete path on this fixture; it does not prove whole-app one-for-one parity or capacity.

## Paired bot message-list JSON trial

`bench/paired_bot_messages.py` seeded disposable, matched fixtures with one room, one bot, and messages alternating between plain text and a simple rich-text `<div>`. Both servers used the same signing secret. The probe fetched the latest 40 messages from the bot JSON API and confirmed that every parsed field matched after removing the different URL origins, including ActionText-wrapped HTML, creator details, signed avatar URL paths, and timestamps. Campfire used a running Redis server for production caching; the fixture now uses a unique update timestamp per run so fragments from older trials cannot be reused. Rustfire used SQLite WAL. Two warmup requests preceded each set of 30 sequential requests. The servers and Python load generator shared a host and ran serially.

| Stored messages | Campfire workers | Rustfire median / p95 | Campfire median / p95 | Rustfire / Campfire JSON bytes |
|---:|---:|---:|---:|---:|
| 1,000 | 1 | 0.476 / 0.666 ms | 9.754 / 22.518 ms | 19,025 / 20,225 |
| 10,000 | 1 | 0.448 / 0.496 ms | 9.816 / 24.739 ms | 19,185 / 20,385 |
| 10,000 | 22 | 0.509 / 1.217 ms | 10.053 / 22.256 ms | 19,185 / 20,385 |

The 22-worker, 10,000-message sequential run was repeated on two earlier builds: 0.463 / 0.705 ms and 0.645 / 1.238 ms for Rustfire; 12.091 / 26.891 ms and 10.914 / 22.518 ms for Campfire. All runs confirmed equal parsed content. The 1,200-byte response-size difference comes primarily from Campfire's JSON escaping of HTML characters. These measurements supersede earlier Campfire figures near 90–130 ms: the previous fixture reused fixed cache versions across trials, and the corrected trial verified a live Redis baseline. These trials show a faster **bot message-list read endpoint** on this fixture, not full-app parity or an overall speed ratio.

The final 22-worker run also used three-second concurrent sweeps. Each client kept one HTTP connection and checked every response against its warmup body. No measured request failed.

| Clients | Rustfire requests/s | Campfire requests/s | Rustfire p95 | Campfire p95 |
|---:|---:|---:|---:|---:|
| 1 | 2,096.1 | 157.2 | 0.634 ms | 8.841 ms |
| 8 | 10,473.7 | 927.7 | 1.109 ms | 13.481 ms |
| 32 | 9,482.0 | 1,273.5 | 6.196 ms | 66.171 ms |

The short 32-client Campfire trial completed 3,846 responses, so tail latency and throughput remain sensitive to scheduling and requests finishing after the three-second start window. Attachment rendering, writes, ActionCable broadcasts, push delivery, webhooks, and sustained operation remain outside this probe. Maximum user or socket scale has not been established for the feature-complete app.

An additional `--messages 1000 --iterations 5 --include-attachments` run replaced every tenth message with a file-only text attachment. Parsed JSON matched for the latest 40 messages, including attachment filenames as plain text and empty HTML bodies. It measured 0.647 / 0.791 ms median / p95 for Rustfire and 8.368 / 20.694 ms for Campfire. The fixture supplies attachment metadata but no file bytes, so this is only a message-list read parity check, not an attachment delivery or scale result.

With `--messages 41 --iterations 5 --scramble-timestamps`, message 1 was moved to the newest timestamp and message 41 to the oldest. The latest 40 and both `before` and `after` pages matched Campfire's parsed JSON. The final release build measured 0.486 / 0.884 ms for Rustfire and 10.934 / 16.690 ms for Campfire. Rustfire now indexes a nanosecond creation-time key and migrates older message rows once at startup; this case checks ordering independently of message IDs.

## Paired signed-stream delivery probe

`bench/paired_turbo_fanout.py --sockets 200 --messages 10 --campfire-workers 1` used disposable databases with one open room, one signed `RoomMessagesChannel` stream, and the same number of concurrent sockets and posts on each app. Both delivered all 2,000 expected events with no unexpected frames or early disconnects. The final release run measured **33.71 ms p95** for Rustfire and **171.72 ms p95** for Campfire. Three earlier runs measured 29.04 / 132.78 ms, 33.12 / 134.94 ms, and 31.05 / 157.65 ms respectively. The generator and both servers shared one host, with the apps run serially.

The average Turbo message payload was **2,318 bytes** in Rustfire and **9,647 bytes** in Campfire. Rustfire's message markup and some per-post work still differ, so this is evidence that signed subscriptions and Turbo delivery function at 200 sockets, not a feature-equivalent speed or maximum-scale result. A 50-socket, 5-message run also delivered all 250 events on both apps (42.88 ms and 128.39 ms p95 respectively). Sustained load, multiple rooms and users, and matching payloads remain to be tested.

After boost events were changed from full-message replacement to individual append/remove actions, the 200-socket, 10-message create probe still delivered all 2,000 events on both apps. It measured **29.30 ms p95** for Rustfire and **163.79 ms p95** for Campfire, with **2,340 / 9,647 bytes** average event bodies. This is a regression check under the current build; the same content and side-effect caveat applies.

## Paired boost append fanout

`bench/paired_turbo_fanout.py --operation boosts --sockets 200 --messages 10 --campfire-workers 1` seeded one existing message in each app and posted ten boosts while 200 signed room-stream sockets were subscribed. Both apps delivered all **2,000 / 2,000** expected boost append events with no misses or unexpected frames. Rustfire measured **30.66 ms p95** and Campfire **86.55 ms p95** on the local host. The average event bodies were **167 / 1,565 bytes** respectively. A 50-socket, 5-boost check delivered all 250 events on each app (40.31 / 115.34 ms p95). Both now use append for boost creation and remove for boost deletion, but the boost HTML and related work are not yet equivalent; these short runs do not prove a capacity advantage.

## Paired room refresh selection

`bench/paired_room_refresh.py --iterations 30` seeded three matched messages: one old and unchanged, one old and edited after the cutoff, and one created after the cutoff. Rustfire backfilled numeric creation/update times from its fixture's imported-style rows. Both apps returned a Turbo append for the new message and a replace for the edited message, excluding the unchanged one; every stream target existed on its app's room page. On the local host, the final run measured **0.179 / 0.415 ms** median / p95 for Rustfire and **6.582 / 16.879 ms** for Campfire over 30 sequential warm-cache requests. An earlier run measured 0.194 / 0.449 ms and 5.528 / 16.241 ms respectively. The response sizes were **4,553 / 20,506 bytes**. These different HTML bodies preclude a feature-equivalent latency claim; the probe establishes selection parity and a repeatable endpoint comparison. It does not measure concurrent refresh capacity or full reconnect behavior.

The reconnect probe now uses isolated Redis, aligned user and room fixture identities, and the same Campfire signing secret in Rustfire. Rustfire now includes the source's hidden CSRF fields in the refreshed messages. Parsed Turbo response tags, attributes, and non-whitespace text match for the selected new and edited plain messages after normalizing session tokens, signed avatar paths, and origins. A new 30-request sequential warm-cache run measured **0.128 / 0.277 ms** median / p95 for Rustfire and **6.708 / 20.774 ms** for Campfire, with **17,146 / 20,506** raw response bytes. This is a feature-matched fixture for one reconnect response; other message types, sustained concurrent refresh, full browser reconnect behavior, and CPU or memory capacity remain unverified.

## Campfire-style Turbo targets

Rustfire now names open, closed, and direct room message containers `messages_rooms_open_ID`, `messages_rooms_closed_ID`, and `messages_rooms_direct_ID`, matching the pinned Rails STI model names. It wraps each rendered message with `message_CLIENT_ID`, and exposes `presentation_message_CLIENT_ID`, `boosts_message_CLIENT_ID`, and `boost_ID` targets for the corresponding Turbo actions. The paired fanout probe now checks that each app's append target exists on its room page, so a mismatched target fails rather than counting a delivered but unapplied WebSocket frame. A browser check confirmed posting, a boost, opening inline edit, and canceling it retained one message wrapper and the boost.

On the final 200-socket, 10-post runs, both apps delivered all 2,000 events per workload with no missed or unexpected frames. Message append p95 was **32.88 ms** for Rustfire versus **156.46 ms** for Campfire; average payloads were **2,586 / 9,647 bytes**. Boost append p95 was **27.82 / 88.59 ms**, with **179 / 1,565 bytes** average payloads. A 30-request room refresh check still selected the same messages and existing targets, at **0.254 / 0.497 ms** median / p95 for Rustfire and **6.289 / 18.820 ms** for Campfire; response sizes were **4,910 / 20,506 bytes**. These are local short-run endpoint and delivery observations. The HTML and some side effects remain different, so they do not establish one-for-one speed or maximum capacity.

## Boost controls and signed-stream regression

Rustfire's boost markup now includes the booster's avatar and name and a delete control that the booster can reveal by mouse or keyboard. A browser check verified the streamed boost, reload, owner deletion, and that another user could neither reveal the control nor delete the boost through the endpoint (HTTP 404). The signed-stream WebSocket test also checks the streamed and persisted markup.

On the release build after this change, `python bench/paired_turbo_fanout.py --operation boosts --sockets 200 --messages 10 --campfire-workers 1` delivered all **2,000** expected events in each app, with no misses or unexpected frames. Rustfire's p95 was **32.31 ms** and Campfire's was **91.29 ms**; average event bodies were **497 / 1,565 bytes**. The apps still render different boost markup, and this short local burst does not prove full feature parity or maximum scale.

## Closer boost stream markup and matched identity

The boost stream now includes the linked profile avatar with a Rails-format signed URL, its accessible booster label, and a form-shaped delete control. The paired fixture now gives Rustfire's benchmark users the same names and update timestamps as Campfire's users. This exposed and fixed a native Rails timestamp parsing error that had appended fractional seconds to Rustfire's avatar version URL. The probe captures one event per app and fails if the booster name or signed avatar URL differs; both matched on the final run.

With 200 signed stream sockets and 10 boosts, both apps delivered all **2,000** events. The final release run measured **31.03 ms p95** for Rustfire versus **103.67 ms p95** for Campfire, with average event bodies of **1,469 / 1,565 bytes**. An earlier run before aligning more of Campfire's boost attributes measured 32.34 / 88.08 ms p95 and 1,267 / 1,565 bytes. The stream HTML, icon URLs, and browser framework behavior still differ. These are short local delivery measurements, so the result remains short of a one-for-one speed or capacity claim.

## Campfire-style message roots

Rustfire now uses `message_CLIENT_ID` as the `.message` element itself, with numeric message and creator IDs, creation/update timestamps, and a signed creator avatar. The browser's history, refresh, editing, and permalink lookup use the numeric data attribute rather than the removed `message-N` child ID. A live browser check covered posting, inline edit, boost append, permalink loading, and deletion. The WebSocket assertion checks the root and signed avatar markup. The paired fixture's creator name and signed avatar URL matched Campfire's sample event.

After this change, the 200-socket, 10-message signed-stream probe delivered all **2,000** events in each app, with no misses or unexpected frames. The final fixture reset Campfire's message sequence so both apps posted the same numeric IDs; the sample events also matched on creator name and signed avatar URL. Rustfire's p95 was **32.73 ms** and Campfire's **147.06 ms**; average event bodies were **3,341 / 9,589 bytes**. An earlier run before the sequence reset measured 34.45 / 148.70 ms p95 and 3,341 / 9,647 bytes. Campfire still renders much more message HTML and performs different work, so this is a delivery regression check rather than a feature-equivalent speed or scale result.

## Message action markup and forms

Rustfire's streamed message actions now render the eight quick boosts as forms with Campfire-style frame targets, plus icon controls for adding a custom boost, reply, copying a link, and editing. A browser check verified quick and custom boosts stayed on the room page and reached the message, reply filled the composer, and edit opened inline. The WebSocket test checks that the streamed form target and custom boost link are present.

With 200 signed room sockets and 10 messages, both apps delivered all **2,000** events without misses or unexpected frames. Rustfire measured **39.06 ms p95** and Campfire **141.81 ms p95**; average event bodies were **7,847 / 9,589 bytes**. The remaining HTML still differs in Turbo frames, message layout, and some controls, and per-post side effects are not yet equivalent. This is a local burst regression check, not proof of the final speed or scale goal.

## Message edit and boost frame targets

Rustfire's rendered messages now include Campfire's `edit_message_CLIENT`, `boosting_message_CLIENT`, and `new_boost_message_CLIENT` Turbo frames, alongside the existing presentation and boost list targets. A browser check on a room with existing messages and boosts confirmed those frames render, the custom boost form submits inline, and editing still opens inside the message. The signed-stream test checks the frame IDs, and the paired message probe rejects samples missing any of the six message or frame targets.

On the release build, the 200-socket, 10-message probe delivered all **2,000** expected events in each app, with no misses or unexpected frames. Rustfire measured **36.12 ms p95** and Campfire **134.70 ms p95**; average event bodies were **8,931 / 9,589 bytes**. A one-socket message probe verified matching creator identity and target IDs, and a one-socket boost probe verified booster identity. The frame contents, day separator, and some controls still differ. These short local bursts do not establish one-for-one feature parity or maximum scale.

## In-message day headings

Rustfire now renders Campfire's `message__day-separator` heading inside every message and marks the first message of each local day in the browser. This replaced separately inserted date nodes, so the heading stays with its message in Turbo appends and replacements. The browser check confirmed one visible heading for two same-day messages and two visible headings when the first message's disposable fixture timestamp moved to the prior day. The first-of-day avatar remained aligned with its bubble. The signed-stream and paired probes check the heading markup.

On the release build, the 200-socket, 10-message probe delivered all **2,000** events in each app, with no misses or unexpected frames. Rustfire measured **35.79 ms p95** and Campfire **163.85 ms p95**; average event bodies were **9,078 / 9,589 bytes**. The heading's fallback text and attributes still differ, as do other message HTML and side effects. This is a short local delivery comparison, not proof of full parity or maximum capacity.

## Message metadata and action placement

Rustfire now places a `message__heading` and the action menu together inside `message__meta`, matching Campfire's element order. The inline add-boost link appears only on messages with boosts, as in Campfire. In a browser room containing an owned message near the bottom of the viewport, the action menu flipped upward instead of being clipped by the composer; a message higher in the room opened downward. Quick boosting and inline editing still worked. The WebSocket test asserts the heading and action containers are adjacent in a signed stream.

On the release build, the 200-socket, 10-message probe delivered all **2,000** expected events in each app, with no misses or unexpected frames. Rustfire measured **28.01 ms p95** and Campfire **149.73 ms p95**; average event bodies were **9,112 / 9,589 bytes**. At this stage, the Campfire sample had a room link inside the heading that Rustfire lacked, and the rendered UI and side effects were not equivalent. These short local bursts do not establish a one-for-one speed or scale advantage.

## Message room links

Rustfire now includes Campfire's room permalink inside `message__heading`. For direct rooms, its label includes all participants with the same two-name and Oxford-comma sentence form used by Campfire. The HTML message batch loads that label once; JSON-only message reads skip the lookup. Smoke assertions cover an open-room message and a direct-room attachment message. The paired stream fixture now aligns the disposable room names and rejects a mismatch in the room link label.

On the release build, the 200-socket, 10-message probe delivered all **2,000** expected events in each app, with no misses or unexpected frames. Rustfire measured **37.42 ms p95** and Campfire **152.89 ms p95**; average event bodies were **9,221 / 9,589 bytes**. A one-socket probe after the JSON-path optimization passed the same room-label check. A separate 1,000-message, 10-iteration bot JSON read check returned the same normalized data, with Rustfire **0.701 / 1.261 ms** median / p95 and Campfire **9.250 / 17.043 ms**. These are short local endpoint observations; message HTML and side effects remain different, and sustained maximum scale is unproven.

## Plain message presentation and lazy boost forms

Rustfire now places plain text in a `trix-content` block directly inside the presentation target, matching Campfire's message structure. The initial message no longer carries hidden custom-boost forms. Selecting New boost from the action menu or the inline boost control loads a form into `new_boost_message_CLIENT` on demand. In the browser, opening, canceling, reopening, and submitting a custom boost kept the user in the room and appended the boost; reply and inline edit still worked with the direct presentation target. Smoke and signed-stream checks cover the lazy frame and absence of eager forms.

The paired plain-message samples now have identical counts and order for HTML tags after normalizing Rails' explicit closing tags for void elements. The paired probe checks this structure, but attributes, text, styling, and per-post work still differ. On the final release build, the 200-socket, 10-message probe delivered all **2,000** events in each app, with no misses or unexpected frames. Rustfire measured **34.85 ms p95** and Campfire **139.82 ms p95**; average event bodies were **8,609 / 9,589 bytes**. This short local comparison does not prove full feature parity or maximum capacity.

## Plain-message stream attributes and actions

Rustfire's plain-message stream now uses Campfire's message classes and action attributes without duplicate Rustfire-only markers. Its browser code reads Campfire's `data-user-id` and `data-local-time-target` fields, submits quick-boost forms, and handles the menu, reply, copy, and edit controls through their Campfire selectors. A browser check confirmed local date and time rendering, sender recognition, live posting, quick and custom boosts, reply, and inline edit. The paired probe now checks each element's attribute-name set as well as ordered tags. The sampled streams have 86 elements and matching attribute names at each position.

On a release build, both apps delivered all **2,000** expected events to 200 sockets during 10 posts, with no misses or unexpected frames. Rustfire averaged **8,470 bytes** per event and measured **40.17 ms p95**; Campfire averaged **9,589 bytes** and measured **151.21 ms p95**. These are short local bursts. Sample attribute values still differ for generated IDs and timestamps, asset URLs, and the clipboard origin; text, other message types, side effects, and sustained capacity need separate checks. This does not establish full feature parity or maximum scale.

## Matched boost append streams

Rustfire now renders Campfire's boost delete targets and accessible instruction, uses the same emoji-only text class, and adds `maintain_scroll="true"` to boost append events. Its browser code reveals the delete control for the booster using those targets. A browser check confirmed a quick emoji boost appeared, its delete button became visible with the accessible description, and deleting it removed the boost without leaving the room. A separate emoji-only message received Campfire's `message--emoji` class. The pinned SVG assets are served at Campfire's fingerprinted paths; the browser loaded one of the pinned message icons from that route.

The paired probe now sends the same boost text to both apps and rejects any difference in parsed boost-stream tags, attribute names or values, or non-whitespace text. The sampled boost event has **13 elements in each app** and passes that check. With 200 signed sockets and 10 boost posts, each app delivered all **2,000** events without misses or unexpected frames. Rustfire measured **30.92 ms p95** and averaged **1,495 bytes** per event; one-worker Campfire measured **93.44 ms p95** and **1,565 bytes**. A separate 200-socket, 10-message run also delivered all 2,000 events per app, at **33.67 / 152.08 ms p95** and **8,489 / 9,589 bytes** for Rustfire / Campfire. These short local bursts strengthen boost-stream parity evidence but do not establish equivalent full-app work, sustained capacity, or a maximum scale advantage.

## Absolute message copy links

Rustfire now emits an absolute copy-link URL in message HTML using the configured public origin or the request Host. The live stream, room page, message history, and refresh path use the same rendering rule; posts without a request use the configured public origin or bind-address fallback. The WebSocket and smoke tests assert the absolute URL for a randomly assigned local port.

The paired plain-message probe now supplies one canonical public origin to Rustfire and compares parsed static attribute values and non-whitespace text, as well as the existing element and attribute-name structure. One sampled stream had **86 elements** and equal static attributes and text. Only generated creation/update timestamps and the two time-element values differed; the apps posted serially. With 200 sockets and 10 message posts, both apps delivered all **2,000** expected events with no misses or unexpected frames. The first run measured **31.08 / 152.80 ms p95** for Rustfire / one-worker Campfire; a repeat after avoiding an unnecessary bind-address lookup measured **26.50 / 180.87 ms p95**. Average event sizes were **8,505 / 9,589 bytes** in both runs. The remaining byte difference includes serialization whitespace, and other message types and side effects are not proven equivalent. These short bursts do not establish full-app speed or maximum scale.

## Text attachment streams and signed blobs

Rustfire now renders non-preview attachments with Campfire's file row and Download/Share controls. Its signed blob URL uses Campfire's Active Storage verifier when the original secret key is configured; otherwise it uses a persistent local key. The signed route serves file bytes to anyone holding a valid token, and direct `/attachments/ID` downloads retain room access checks. Smoke tests verify attachment disposition for text and image files, anonymous access through a valid signed URL, and rejection of a tampered token. In a browser, the Share controls hid when the Share API was absent. With a stubbed Share API, the row action fetched the file and passed a `text/plain` File to the share handler.

The paired text-attachment probe posts the same small file to both apps. One sampled append had **96 elements** in each stream; ordered elements, attribute names, static values, and non-whitespace text matched after excluding generated timestamps. In two 200-socket, 10-attachment bursts, both apps delivered all **2,000** events per run with no misses or unexpected frames. Rustfire measured **40.68 and 37.77 ms p95**; one-worker Campfire measured **232.02 and 195.93 ms p95**. Average event sizes were **10,459 / 11,571 bytes** for Rustfire / Campfire. The apps still differ in file storage and processing, and Rustfire currently serves the signed blob path directly while Campfire redirects to its storage service. Image/video variants and larger files were outside this trial. This does not establish full-app speed or maximum scale.

## Image attachment streams and processed previews

Rustfire now reads uploaded image dimensions, creates and sharpens the thumbnail before broadcasting, and renders Campfire's measured preview container, image dimensions, and signed Active Storage representation URL. The paired image probe uploaded the same generated 2400×1600 PNG to each app. Both streams had the same ordered elements, attribute names, static values, and text after excluding generated timestamps. Both signed representation URLs returned a **byte-identical 331,484-byte 1200×800 PNG**. A separate 1×1 PNG sample produced identical 281-byte previews. The paired probe now fails if the PNG hashes differ.

In two `--operation images --image-size 2400x1600 --sockets 200 --messages 10 --campfire-workers 1` bursts after adding Campfire's sharpening step, both apps delivered **2,000 / 2,000** expected events without misses or unexpected frames. Rustfire measured **139.85 and 117.91 ms p95**; one-worker Campfire measured **311.64 and 269.05 ms p95**. The corresponding median latencies were **131.06 / 94.00 ms** and **111.87 / 90.19 ms** for Rustfire / Campfire: Rustfire had lower p95 but higher median latency. Average event bodies were **10,249 / 11,357 bytes**. This single-host, one-room, one-user burst does not establish sustained throughput, maximum capacity, or whole-app performance at full feature parity. Rustfire still differs in attachment storage and response behavior, and video processing remains outside this trial.

## Video attachment streams and posters

Rustfire now probes uploaded video dimensions and display aspect ratio, extracts the same JPEG preview frame as Campfire, and generates the resized, sharpened WebP poster before broadcasting. Its video stream includes Campfire's measured container and signed poster representation URL. The paired probe verified matching ordered elements, attribute names, static values, and text after excluding generated timestamps. Both the 16×16 default clip and a separate 2400×1600 clip produced byte-identical WebP posters from the two apps: **280 bytes** and **2,100 bytes**, respectively. Separate 16×16 clips with 2:1 and 3:2 sample aspect ratios also matched stream markup and poster hashes. The probe fails on a poster hash difference.

In two `--operation videos --sockets 200 --messages 10 --campfire-workers 1` bursts using the 16×16 clip, both apps delivered **2,000 / 2,000** expected events without misses or unexpected frames. Rustfire measured **175.04 and 173.16 ms p95**; one-worker Campfire measured **320.49 and 355.23 ms p95**. The corresponding medians were **162.63 / 131.66 ms** and **156.75 / 128.85 ms** for Rustfire / Campfire. Average event bodies were **9,962 / 11,068 bytes**. Rustfire had lower p95 and higher median latency in both runs. Larger video bursts, other codecs and aspect ratios, and sustained capacity remain unmeasured; storage and response behavior still differ.

## Message edit frames

Rustfire now returns Campfire's editable Turbo frame for plain messages and a view with Delete and Close controls for attachments. The room's editor link loads the frame in place; Save uses the nested message PATCH route, Close restores the message, and Delete removes it. The paired probe fetched edit responses for plain messages, text files, images, and videos. All four frame samples matched Campfire's ordered elements, attribute names, static values, and text after normalizing session CSRF tokens and the apps' different listen origins. A browser check opened the editor, saved text, closed a reopened editor without changes, and deleted the message. The smoke suite also exercises Rails-style `_method=patch` and `_method=delete` form posts. The surrounding document layout and other browser interactions still need parity work.

## Bot administration behavior and token storage

`bench/paired_bot_admin.py` now runs the same bot creation, editing, key rotation, and deletion workflow against the pinned Campfire and Rustfire builds on disposable databases. Both returned HTTP 302 to `/account/bots` for each mutation, accepted the same three new-form fields, saved a 12-character alphanumeric token, the byte-identical original PNG avatar, webhook, and open-room membership, and allowed the same bot message API path. After rotation, the old key redirected to sign-in and the new key worked. Deactivation retained the token on the inactive row and removed its open-room membership in both apps. Rustfire also migrated a legacy prefixed token in an older Rustfire database to Campfire's token-only storage without changing the public key. This checks the listed workflow and persisted fields; the surrounding HTML and all bot behavior are not yet equivalent.

The bot list now includes Campfire's room-specific text and file-upload `curl` commands. The paired workflow normalizes only the different listen origin and random bot key, then requires both displayed command values to match. In a browser, both copy buttons wrote exactly the displayed command text and the screen loaded without console errors. The surrounding page layout and all bot-list markup still need a broader visual and structural comparison.

## Bot JSON read concurrency sweep after token alignment

With 10,000 seeded messages and the latest 40 returned, `bench/paired_bot_messages.py` again confirmed equal parsed JSON after URL-origin normalization. The response sizes were 19,185 bytes for Rustfire and 20,385 for Campfire. Each app ran separately on the same host with Redis available to Campfire and SQLite WAL in Rustfire. One run used one Campfire worker; the other used 22. Two warmup reads preceded each measurement. The following results are separate short local trials, not a sustained capacity limit.

| Campfire workers | Clients | Trial length | Rustfire requests/s | Campfire requests/s | Rustfire p95 | Campfire p95 |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | 1 | 2 s | 1,926 | 155 | 0.8 ms | 10.7 ms |
| 1 | 8 | 2 s | 7,876 | 147 | 1.8 ms | 86.6 ms |
| 1 | 32 | 2 s | 3,764 | 92 | 10.3 ms | 420.3 ms |
| 22 | 1 | 2 s | 1,991 | 122 | 0.7 ms | 15.8 ms |
| 22 | 8 | 2 s | 8,772 | 876 | 1.5 ms | 16.2 ms |
| 22 | 32 | 2 s | 4,027 | 1,310 | 9.6 ms | 57.4 ms |
| 22 | 64 | 3 s | 4,105 | 1,283 | 21.9 ms | 108.3 ms |
| 22 | 128 | 3 s | 3,850 | 1,225 | 53.6 ms | 151.0 ms |

All listed runs had zero HTTP errors. Under a 100 ms p95 threshold for **this read endpoint**, Rustfire met the threshold at every tested concurrency through 128 clients; the 22-worker Campfire run met it through 32 and exceeded it at 64 and 128. A longer trial, independent load generator, more routes, and full feature parity are still required before claiming a general speed or scale advantage for the application.

## Account logo parity and cached PNG reads

`bench/paired_account_logo.py` used the pinned Campfire checkout and matching disposable accounts. A multipart PATCH of account name, room-creation setting, and `moon.jpg` produced the same stored values and HTTP 302 redirect to `/account/edit`. Both apps returned byte-identical stock PNGs at 512 and 192 pixels, byte-identical 512 and 192 pixel PNG variants for the JPEG, and the stock PNGs again after deletion. Both fell back to stock PNGs when the uploaded image was BMP. The probe asserts all of these values and the decoded pixel hashes; it checks this workflow and these fixtures, not every upload format or account screen interaction.

The timed run fetched the cached, **byte-identical 74,720-byte** 512-pixel JPEG variant. A release Rustfire process and packaged 22-worker Campfire each ran separately on the same 32-logical-CPU host; the Python load generator shared that host. Each client used a persistent HTTP connection and two warmup GETs. Three-second trials produced zero errors and verified each response body against the expected PNG:

| Clients | Rustfire requests/s | Campfire requests/s | Rustfire p95 | Campfire p95 |
| ---: | ---: | ---: | ---: | ---: |
| 1 | 10,462 | 433 | 0.136 ms | 6.558 ms |
| 8 | 12,427 | 4,208 | 1.622 ms | 3.156 ms |
| 32 | 12,411 | 5,174 | 6.373 ms | 9.822 ms |

This is an exact-response comparison for one warm logo-read endpoint. It does not include uploads or variant generation in the measured interval. The load generator was local, CPU and memory use were not recorded, and the three-second runs do not establish a sustained capacity limit. Full-app speed and scale remain unproven while other routes and behavior differ.

The logo endpoint now sends Campfire-style weak ETags. On both apps, the large and small variants share the same validator; a matching conditional GET returns 304 with an empty body and `Cache-Control: no-cache`. Uploading a JPEG, deleting it, and uploading a BMP each change the validator, so an older validator gets the current 200 response. The paired logo probe checked those transitions alongside the byte-identical PNGs.

With the packaged 22 Campfire workers, two-second local trials of the **same conditional 304 request** had zero errors:

| Clients | Rustfire requests/s | Campfire requests/s | Rustfire p95 | Campfire p95 |
| ---: | ---: | ---: | ---: | ---: |
| 1 | 26,659.7 | 1,243.5 | 0.050 ms | 1.893 ms |
| 8 | 19,663.4 | 8,287.4 | 1.301 ms | 1.696 ms |
| 32 | 15,475.1 | 7,945.2 | 6.328 ms | 8.395 ms |

The same run's full 74,720-byte PNG reads reached 10,749.4 / 506.9 requests/s at one client, 14,116.4 / 3,889.7 at eight, and 11,858.2 / 5,178.3 at 32 for Rustfire / Campfire. The 304 responses had the same status and empty body but server-specific ETag values. These short same-host reads do not establish sustained capacity or full application parity.

## Account administration route and roster behavior

Rustfire's visible account forms now submit Campfire's nested account fields to `/account.1` with Rails-style `_method` values; `/account` remains supported for direct requests. The visible user controls submit role changes and deletion to `/account/users/:id`. A browser check confirmed that changing the account name, toggling the room-creation restriction, selecting a JPEG logo, deleting it, and promoting a member all returned to `/account/edit` with updated controls and no detected browser errors.

`bench/paired_account_users.py` compared the same actions with the pinned Campfire checkout on disposable databases. Both apps placed the administrator before the member with a divider inside `account_users`; promoting user 2 returned **302 to `/account/edit`** and saved role 1; an invalid role fell back to member; deletion returned **302 to `/account/edit`**, set status 1, removed the open-room membership, retained the direct-room membership, and returned **404** on a repeated delete. The surrounding account HTML and presentation still differ, and this check does not measure throughput.

### Large account roster behavior

With `--users 1100`, the pinned Campfire `/account/edit` response rendered **all 1,100 users**, then included a lazy frame pointing to page 2. Its `/account/users.turbo_stream` endpoint returned 500 users on pages 1 and 2, 100 on page 3, and no users on page 4. Pages 1 and 2 replaced `next_page_container` and appended a new lazy frame; page 3 only replaced the frame. Rustfire now returns the same user IDs in the same order and the same stream action and next-page sequence. Browser checks on both apps showed 1,100 entries initially, 1,600 after scrolling to page 2, and 1,700 after page 3; the Rustfire forms delivered by the stream carried CSRF tokens.

The initial all-user rendering is a behavior of the pinned source, despite its separate 500-user page endpoint. Loading later pages duplicates those users in the page. Rustfire now matches the parsed initial roster and all four sampled Turbo Stream responses: ordered elements, attribute names and values, and non-whitespace text were equal after normalizing per-session CSRF token values. The surrounding account page, CSS, serialization whitespace, and other user workflows still differ.

### Matched account user-page read trial

With the same 1,100-user fixture and signed avatar URLs, `bench/paired_account_users.py --users 1100 --clients 1 8 32 --seconds 2 --campfire-workers 22` ran each release server separately on the same 32-logical-CPU host. Every client used one keep-alive connection and two fully parsed warmup responses. Measured responses had status 200, 500 rows, and the expected first and last users; no errors occurred. Rustfire's serialized page-2 response was **874,855 bytes** and Campfire's was **1,003,862 bytes**. The difference includes formatting and masked CSRF tokens; the parsed DOM matched.

| Clients | Rustfire requests/s | Campfire requests/s | Rustfire p95 | Campfire p95 |
| ---: | ---: | ---: | ---: | ---: |
| 1 | 278 | 7.8 | 4.19 ms | 193.70 ms |
| 8 | 1,484 | 55.1 | 7.42 ms | 210.43 ms |
| 32 | 1,589 | 88.9 | 29.94 ms | 650.26 ms |

This is one short local read sweep, with the load generator sharing CPU and network stack with both servers. It supports a speed advantage for this aligned page response under these conditions. CPU and memory were not recorded, and it does not establish sustained capacity or a whole-app speed or scale advantage at full feature parity.

### Settings controls and full-page read trial

The account settings panel now presents the source's two logo upload forms, name form, room-creation switch, and invite controls. `bench/paired_account_users.py --users 51` found the same four form method/field contracts on both apps. On each page, the QR path decoded to the displayed join URL, the copy button carried that URL, and the regenerate control was present. The browser rendered all panel icons and opened the QR lightbox. This is semantic control parity; the surrounding HTML and styling are still different.

On a 1,100-user fixture, the same paired script measured `GET /account/edit` with 22 Campfire workers and a release Rustfire build. Both pages rendered all 1,100 user rows and the matched roster markup. The Rustfire response was 1,931,512 bytes and Campfire's was 2,236,870 bytes. Each client used a persistent connection with two warmup reads; all measured responses returned 200 and included the expected roster and invite fields. The servers ran separately on the same host with the load generator sharing CPU and network resources. Each row is one short local trial.

| Clients | Trial | Rustfire requests/s | Campfire requests/s | Rustfire p95 | Campfire p95 |
| ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | 2 s | 163.8 | 4.3 | 8.2 ms | 293.0 ms |
| 8 | 2 s | 781.7 | 26.3 | 12.9 ms | 366.2 ms |
| 32 | 2 s | 887.0 | 42.2 | 53.3 ms | 1,374.5 ms |
| 64 | 3 s | 1,152.2 | 42.5 | 76.7 ms | 2,603.9 ms |
| 128 | 3 s | 1,150.4 | 46.7 | 173.6 ms | 3,797.8 ms |

All rows had zero HTTP errors. At a 100 ms p95 threshold for this full-page HTML read, Rustfire met it through 64 tested clients; Campfire exceeded it at the first tested client. This shows a larger short-run concurrency margin for this particular screen and fixture. The timed build preceded the account-specific stylesheet adjustment below, so the listed response sizes apply to that build. Static assets, image loads, browser rendering, and writes were outside the timed path. Neither application was tested to a failure limit; sustained capacity and whole-app performance remain unproven.

At a 1280 px wide dark-mode browser viewport, the pinned Campfire panel measured x=417.53 px, y=50 px, width=444.92 px. The adjusted Rustfire panel measured x=417.5 px, y=50 px, width=445 px. The invite text field started at x=500.47 px, y=435.88 px in Campfire and x=500.5 px, y=435.38 px in Rustfire. This is a geometry check of the visible top of the page; font, icon treatment, roster presentation, other themes and viewport sizes, and the outer document still need visual parity work. The account stylesheet loads before user custom CSS.

## Profile forms and device transfer

Rustfire's profile now displays the source's nested name, email, password, bio, and avatar forms; per-room notification buttons; PWA install notice; and QR, copy, and share controls for a four-hour device sign-in link. The transfer page submits automatically, as it does in Campfire. Profile GETs now generate a Rails-format signed link without adding a database row. With the source secret configured, a Campfire link signed in the disposable source fixture authenticated on Rustfire, and a Rustfire link authenticated on Campfire. Both apps returned HTTP 302 to the root and the receiving browser could then open room 1. A browser followed Rustfire's link through the automatic POST to the room without a page error. The paired probe also compared profile name, email, bio, and involvement writes and their database values. Its manifest check matched the source's key set, icon sizes and purposes, shortcut destinations and asset paths, and screenshot dimensions and asset paths after normalizing the apps' listen origins and app names. Rustfire serves the same screenshot image files; its branded descriptions differ.

For the same two-membership fixture at a 1280 px dark-mode viewport, Campfire's profile panel was 444.92 × 1061.25 px at x=417.53, y=50; Rustfire's was 445 × 1061.39 px at x=417.5, y=50. Campfire's first notification row started at y=730.66 and transfer input at y=963.47; Rustfire's started at y=731 and y=964. The fixture's user names and avatars differed, and the rest of the HTML and browser behavior have not been established as identical.

`bench/paired_profile.py --requests 320` then measured short warm `GET /users/me/profile` runs on one host with four Puma workers, one release Rustfire process, and persistent connections. The fixture had one open-room membership in each app. Both returned 200 throughout; the source HTML response was 35,622 bytes and Rustfire's was 13,807 bytes. The load generator shared the server host. This is one run, with two warmup requests per client and no CPU or memory measurement.

| Clients | Rustfire requests/s | Campfire requests/s | Rustfire p95 | Campfire p95 |
| ---: | ---: | ---: | ---: | ---: |
| 1 | 12,950 | 175 | 0.13 ms | 14.29 ms |
| 8 | 14,673 | 365 | 1.09 ms | 36.91 ms |
| 32 | 16,410 | 682 | 3.79 ms | 74.71 ms |

The screen's forms and sampled mutations match, but its full response bytes differ substantially, including source JavaScript imports and document markup. These numbers show a large speed margin for this narrower read workload under the tested conditions; they do not establish a whole-app advantage or a maximum sustainable client count.

## Original room invitation and mobile composer

Rustfire now renders the source's welcome invitation in the original room while it has at most 40 messages. The card contains the account logo, translated welcome text, current join URL, QR link, copy value, share control, and administrator regenerate form. `bench/paired_room_invitation.py` verified both apps displayed the card with 0, 1, and 40 messages, removed it at 41, and omitted it from a second room. It also decoded each QR path to its displayed join URL and matched the copy value. A browser opened Rustfire's QR dialog and posted a message through the mobile composer; the welcome card remained visible after that post.

At 1280 × 633 px in dark mode, Campfire's welcome body measured x=318.81, y=88.98, width=373.58, height=243.78 px; Rustfire's measured x=318.59, y=89, width=374, height=243 px. At 390 × 844 px, both bodies started at x≈12.8, y≈155 with width≈364.4 px. The mobile composer form measured x≈12.8, y=778, width≈364.4, height=42 px in both apps after the layout adjustment. Rustfire uses the upstream attachment and send SVG assets. The visible sidebar and notification bell still differ, as do broader room markup and interaction details; this geometry check does not establish full visual or behavioral parity.

## Current attachment fanout sweep

The room sidebar, composer, and notification bell were brought closer to Campfire, and the composer now sends every selected file as a separate message. A browser check selected two files, removed and re-added one, and sent them with text; Rustfire stored three separate messages. This was a behavior check, not a paired browser upload benchmark.

The WebSocket path now shares each broadcast payload, prepares its Turbo representation once, uses a membership-existence query for authorization, and caches access and the serialized frame only for that broadcast. The next event rechecks membership. Before the authorization change, two 10,000-socket attachment trials measured Rustfire p95 at 594.56 and 631.08 ms against 456.97 and 477.91 ms for Campfire. The final build was checked with the following paired, disposable runs using `bench/paired_turbo_fanout.py`, 22 Puma workers, one Rustfire process, one room and account, 20 small text-file posts, and the same number of subscribed sockets in each app. Both apps ran serially on the same host as the load generator. The probe compared the sampled attachment stream structure and identity whenever both runs completed.

| Sockets | Rustfire deliveries | Campfire deliveries | Rustfire p95 | Campfire p95 |
| ---: | ---: | ---: | ---: | ---: |
| 10,000 | 200,000 / 200,000 | 200,000 / 200,000 | 356.33 ms | 454.50 ms |
| 20,000 | 400,000 / 400,000 | 400,000 / 400,000 | 723.19 ms | 778.49 ms |
| 30,000, run 1 | 600,000 / 600,000 | 600,000 / 600,000 | 1,164.52 ms | 1,235.33 ms |
| 30,000, run 2 | 600,000 / 600,000 | 600,000 / 600,000 | 1,183.94 ms | 1,260.84 ms |
| 40,000, run 1 | 800,000 / 800,000 | 797,090 / 800,000 | 1,613.09 ms | 1,762.55 ms on received events |
| 40,000, run 2 | 800,000 / 800,000 | 798,041 / 800,000 | 1,595.78 ms | 1,573.76 ms on received events |

The 40,000-socket Rustfire bursts took 20.18 and 20.42 seconds after posting began. Campfire's probe hit its 30-second delivery deadline with 2,910 and 1,959 events still missing; its p95 figures exclude those missing events and cannot be treated as complete-run latency. Rustfire's sampled event body was about 10,477 bytes and Campfire's about 11,589 bytes. At 30,000 sockets the client used two loopback source IPs; at 40,000 it used four. Single-IP and two-IP attempts that failed during connection setup with local port errors were excluded. The four-IP distribution was identical for both apps. These results show a larger **short-burst attachment-fanout delivery margin** for Rustfire on this fixture, not a maximum sustainable socket count or a whole-app speed and scale advantage. Long-lived connections, CPU, peak memory, browser rendering, varied rooms and users, and full feature-equivalent side effects remain unmeasured.

## Full search-page read

The pinned Campfire and Rustfire builds each searched 10,000 messages in one accessible room with matching text and distinct creation times. `bench/paired_search.py --messages 10000 --requests 30` checked the same ordered IDs and body text for the latest 100 results in every response, after three warmups. One keep-alive client ran against each server serially; Campfire used one Puma worker and Rustfire one process. On the same host, Rustfire's median was **12.88 ms** and p95 **14.40 ms**; Campfire's median was **25.30 ms** and p95 **59.80 ms**. Responses were 860,568 and 1,050,197 bytes respectively.

The search results now use full message markup in Rustfire's chat layout, including attachment, boost, and message action elements. The two pages still differ in HTML serialization and surrounding layout, and the response sizes differ. This trial is evidence for this warm serial read path, with the client on the server host. It does not prove whole-app speed or sustained capacity at feature parity.

After the webhook and rich-text changes, the 2026-09-25 release build passed the same `--messages 10000 --requests 30` paired probe. Rustfire's median was **9.51 ms** and p95 **12.59 ms**; Campfire's median was **24.60 ms** and p95 **61.46 ms**. The response sizes remained 860,568 and 1,050,197 bytes. This is a single warm, serial regression run with the same semantic result checks and the same limitations described above.

After adding offline inline image, PDF, and video import, release build `7b60fa9` passed the same 10,000-message, 30-request paired search probe. Rustfire measured **9.81 ms median / 14.09 ms p95**; Campfire measured **27.07 ms median / 39.38 ms p95**. All responses passed the probe's ordered-ID and text checks; response sizes remained **860,568 / 1,050,197 bytes**. The different page markup and side effects still prevent a full-app speed or scale claim.

With source-style rich-text form defaults and imported inline media editing in revision `98f65b9`, the same paired search probe measured **9.46 ms median / 13.90 ms p95** for Rustfire and **23.78 ms median / 65.26 ms p95** for Campfire. All 30 measured responses passed ordered-ID and text checks. Response sizes were unchanged at **860,568 / 1,050,197 bytes**; this remains a warm serial read comparison with differing markup.

## Paired Active Storage direct upload

`bench/paired_direct_upload.py` ran against the pinned Campfire checkout and Rustfire release build with disposable databases. Both returned the same metadata field set for a small text file, with the declared byte count, MD5 checksum, MIME type, filename, service, Rails-style signed blob ID and attachable SGID purposes, and a signed disk-upload URL. Anonymous metadata requests returned 422 without CSRF context and 401 after a login-page visit with valid anonymous CSRF context. Both rejected an unauthenticated disk write with 401 and a write with incorrect length or checksum with 422. An authenticated write without a CSRF header returned 204; a public blob URL redirected to a disk URL that returned the original bytes. Sixteen concurrent metadata requests produced unique blob IDs in both apps. The key and blob ID formats remain server specific. This checks an Active Storage framework route, not the main message attachment workflow or its performance; the pinned Campfire app uses multipart message uploads for normal posts.

## Paired account custom CSS workflow

`bench/paired_custom_styles.py` ran the pinned Campfire and Rustfire release builds against disposable accounts. Both editor pages exposed the source form action path, POST method with PATCH override, `account[custom_styles]` textarea with 16 rows and the same placeholder and input attributes, seven translation entries, warning text, back link, and byte-identical warning SVG. PATCH and PUT each returned 302 to the editor and saved the submitted CSS on the account. The account update timestamp changed. Both apps embedded the saved CSS in a `data-turbo-track="reload"` style tag on authenticated and sign-in pages. After revoking the administrator role, GET and PATCH returned 403 and the saved CSS remained unchanged. The normalized probe result matched across both apps. Their full HTML and visual layout still differ, so this is a workflow parity check rather than a one-for-one page or performance result.

The rebuilt Rustfire now keeps the account CSS in `accounts.custom_styles`; existing Rustfire databases copy a nonempty saved value from the earlier separate table on startup. An import check confirmed that Campfire CSS appears in Rustfire's rendered room page after boot. A concurrent settings-page regression run exposed a pool deadlock at 32 clients: each request held a database connection while checking its back-room link through another. Releasing the first connection after the roster queries resolved it. The paired 1,100-user sweep then matched the source's parsed roster, four Turbo Stream pages, and account mutations, with zero measured HTTP errors on either server. Its full-page read results were:

| Clients | Rustfire requests/s | Campfire requests/s | Rustfire p95 | Campfire p95 |
| ---: | ---: | ---: | ---: | ---: |
| 1 | 160.7 | 4.3 | 8.18 ms | 308.22 ms |
| 8 | 737.2 | 23.1 | 13.62 ms | 594.11 ms |
| 32 | 991.8 | 40.3 | 48.51 ms | 1,202.08 ms |

The disposable fixture had no saved custom CSS, so this times the account-page rendering path with the new CSS lookup but not a large inline CSS payload. Rustfire's full response was 1,931,613 bytes and Campfire's was 2,236,870 bytes. Each 2-second trial used persistent clients and two warmups on the same host. These are short local read measurements with different full HTML, not sustained capacity or whole-app parity evidence.

A Chromium check of Rustfire's editor exposed a 403 on form submission: the shared CSRF injector had treated the `>` in `data-action="keydown.ctrl+enter->form#submit"` as the end of the opening form tag. The scanner now stops only at an unquoted `>`. After rebuilding, both Ctrl+Enter and the Save button submitted and reopened the editor with the persisted value. This is a Rustfire browser check; the paired HTTP probe covers Campfire's save behavior.

## Paired user ban workflow

`bench/paired_bans.py` compared disposable accounts on the pinned Campfire and Rustfire release builds. With two public-IP sessions for a member, both apps returned 302 to `/users/2`, marked the member banned, removed both sessions, and saved the two IP bans. A GET from one banned forwarded IP remained 200; a POST returned 429. Repeating ban preserved the same state and returned 302. Unban and repeat unban both returned 302, left the member active, and cleared the bans. Member attempts to ban or unban returned 403. When the member had a private-IP session, both apps returned 422 and rolled back the ban, preserving the active status and session. This covers these route, database, and request-filter outcomes. Campfire enqueues message removal through Resque; Rustfire now commits a SQLite job with the ban for a background worker. A separate restart check recovered a pending 55-message job, deleted all messages across two batches, and removed an attachment and inline blob from storage. These checks do not establish equal job scheduling latency, socket revocation, other IP classes, or all user roles.

The paired banned-content probe seeded the same 55 text messages, search entries, and boost on each app. Each received a ban request. Rustfire's SQLite worker and Campfire's live Resque worker processed the queued jobs; Campfire used a disposable source copy and isolated Redis server so it did not consume shared queue entries. Both removed all 55 messages, search entries, and boosts, and updated the room. Campfire also removed the 55 ActionText rich-body rows. With **200 subscribed room sockets**, each app delivered all **11,000 / 11,000** expected Turbo remove actions with zero misses or unexpected actions, and the sampled removal HTML was byte-identical. The worker polling interval was 250 ms on both apps. Three fresh same-host runs measured elapsed time from socket readiness through ban request, queue wait, job work, and final delivery:

| Run | Rustfire | Campfire |
| ---: | ---: | ---: |
| 1 | 126 ms | 946 ms |
| 2 | 144 ms | 707 ms |
| 3 | 102 ms | 804 ms |

Those runs used one Puma worker and one Resque worker for Campfire. At **5,000 subscribed sockets**, both apps then delivered all **275,000 / 275,000** expected removals in each trial. With one Puma worker, Rustfire took **564 and 562 ms** versus Campfire's **14,189 and 15,867 ms**. With the packaged **22 Puma workers** and one Resque worker, the three fresh runs were:

| Run | Rustfire | Campfire |
| ---: | ---: | ---: |
| 1 | 565 ms | 1,429 ms |
| 2 | 549 ms | 1,427 ms |
| 3 | 560 ms | 1,558 ms |

Reversing the serial order with Campfire first still delivered **275,000 / 275,000** removals on each app at 5,000 sockets: Rustfire took **568 ms** and 22-worker Campfire **1,441 ms**. The 200-socket reverse-order check likewise delivered all 11,000 removals (143 / 586 ms). Neither app missed or duplicated a removal in these runs.

Server, worker, and connection startup were outside the measured interval. The results show a faster **55-message banned-content cleanup and Turbo removal burst** for Rustfire under this fixture, with matched removal actions and zero delivery errors. They do not establish sustained capacity or full-app superiority; richer content, attachment cleanup, multiple rooms and users, CPU and memory use, and retry behavior need separate checks.

With the background worker idle, a 1,100-user paired account-page regression sweep still matched the source's parsed roster and Turbo Stream pages, with zero HTTP errors. At 32 clients for two seconds, Rustfire served the settings page at **1,030.7 requests/s, 43.03 ms p95** versus Campfire's **40.5 requests/s, 1,192.17 ms p95**. The page-2 Turbo Stream measured **1,565.0 versus 84.1 requests/s**, with **23.63 versus 702.89 ms p95**. This checks that the periodic worker did not visibly disrupt those short read trials; the full page HTML and workload side effects still differ.

## Paired browser compatibility and PWA formats

The paired browser-compatibility probe compared the pinned Campfire and Rustfire release builds on disposable accounts. Missing and bot user agents passed; Safari 17.1, Chrome 119, Firefox 114, Opera 103, and Internet Explorer 11 received the unsupported-browser page; their supported boundary versions passed. Chrome on iOS, Firefox on iOS, and Chromium Edge matched the pinned `useragent` parser's decisions in sampled sign-in and room reads. Health checks bypassed the browser gate. Both apps returned 406 for HTML-only manifest and service-worker requests and served the matching JSON or JavaScript format when explicitly requested or accepted. The four browser SVGs are byte-identical to the pinned source. The source's authenticated room page returned 500 for a legacy Edge agent because `install-edge.svg` is absent from its asset path; the probe excludes only that room read. A browser check at 1280 × 800 px and 390 × 844 px matched the source panel, heading, and browser-list bounding boxes exactly: the desktop panel was x=417.53, y=50, width=444.92, height=381.33 px, and the mobile panel was x=0, y=50, width=390, height=369.72 px. The dedicated stylesheet and copied icons brought this screen close visually in sampled light and dark themes; full HTML, interactions, other viewports, and performance remain unverified.

The release build with the browser gate also passed the 1,100-user paired account-user workflow and 2-second local read sweep with zero measured HTTP errors. At 32 clients, Rustfire served `/account/edit` at **1,028.7 requests/s, 40.23 ms p95** and Campfire at **42.5 requests/s, 1,144.98 ms p95**; the page-2 Turbo Stream measured **1,615.8 versus 97.9 requests/s**, with **29.02 versus 550.14 ms p95**. Parsed account-user rows and the sampled mutations matched. Full page HTML and response sizes still differ, and these short reads do not establish sustained capacity or whole-app superiority at feature parity.

## Attachment search-index regression check

After indexing attachment filenames for file-only messages, `bench/paired_turbo_fanout.py --operation attachments --sockets 200 --messages 10 --campfire-workers 1` delivered all **2,000/2,000** expected events for each app and found matching attachment-message identities. Rustfire p95 delivery was **29.01 ms**; Campfire p95 was **223.62 ms** on this one short localhost trial. Average received message sizes were 10,459 and 11,571 bytes. The probe does not compare the apps' full storage or notification side effects, and one 200-socket burst does not establish sustained capacity.

Release build `7b60fa9` repeated that paired attachment trial after the inline media importer changes. Each app delivered **2,000/2,000** events with matching attachment-message identities and no unexpected frames. Rustfire measured **36.68 ms p95** and Campfire **186.23 ms p95**. Average event sizes were unchanged at **10,459 / 11,571 bytes**. This is a short fanout regression check; storage and notification side effects still differ.

Revision `98f65b9` also passed paired 200-socket, 10-post bursts. For text messages, each app delivered **2,000/2,000** expected events with matching message identities; Rustfire measured **35.52 ms p95** and Campfire **163.63 ms p95**, averaging **8,505 / 9,589 bytes** per event. For text attachments, both delivered **2,000/2,000** events with matching attachment-message identities; Rustfire measured **23.73 ms p95** and Campfire **201.26 ms p95**, averaging **10,459 / 11,571 bytes**. These bursts do not establish sustained user capacity or full side-effect parity.

## Paired signed sidebar creation burst

`bench/paired_sidebar_fanout.py` created 10 open rooms on each disposable app database, each with the same 51 active-user memberships. Every subscribed socket received every `Turbo::StreamsChannel` prepend to `shared_rooms`; the sampled 443-byte Turbo payloads were byte-identical. The runs used a Rustfire release process and Campfire's packaged 22 Puma workers with isolated Redis. Room creation and socket delivery were measured after HTTP and subscribed-stream warmups, with a fresh HTTP connection per POST. Server startup, socket connection, warmup, and a 500 ms settling interval were excluded.

| Subscribed sockets | Expected deliveries per app | Order | Rustfire elapsed / p95 | Campfire elapsed / p95 |
| ---: | ---: | --- | ---: | ---: |
| 1,000 | 10,000 | Rustfire first | 124 / 18 ms | 663 / 96 ms |
| 1,000 | 10,000 | Campfire first | 132 / 20 ms | 648 / 128 ms |
| 5,000 | 50,000 | Rustfire first | 516 / 80 ms | 1,180 / 144 ms |
| 5,000 | 50,000 | Campfire first | 511 / 81 ms | 1,132 / 149 ms |

All four pairs had zero missed, duplicate, or unexpected deliveries and zero early socket closes. The elapsed interval includes sequential room POSTs and delivery to the last socket; p95 is per socket from its room's POST start. This establishes a faster matched room-creation/sidebar-broadcast burst on this host. It does not establish sustained user capacity, memory advantage, or whole-app parity. Other sidebar markup and app side effects still differ.

## Paired direct-room sidebar markup

The paired direct-sidebar probe used the pinned source and a Rustfire release build with matching user names, avatar timestamps, and signing secret. It created four direct rooms for the signed-in user with one, two, three, and five other users. Both apps returned those rooms in the sidebar and delivered four signed per-user Turbo prepend frames with the same room IDs and no unexpected events. Each rendered direct link and each received Turbo frame was byte-identical after replacing only its independently generated room `data-sorted-list-number` epoch-millisecond value; both values were validated as current times first. The check includes the four-avatar group limit, abbreviation text, and signed avatar paths. A later extension checked that both sidebars render 19 direct placeholders initially, then 17, 16, 15, and 13 after the four rooms. Campfire's `including` call appends the current user to its participant array, so its limit counts that user twice once a direct room exists. This source behavior is retained in Rustfire. The probe does not time either app or verify the full sidebar, unread states, or sustained socket load.

The direct shortcut forms now use Campfire's query-string participant ID, signed and versioned avatar path, and button markup. The paired probe aligns the first two fixture users' creation times, then compares every shortcut's ordered user ID and full form fragment across five sidebar states, normalizing only independently generated CSRF tokens. All 80 compared forms matched. A browser check clicked the first shortcut and reached its direct room at `/rooms/2`; the smoke suite also posts the query-string form path.

Rustfire's sidebar now includes the source's `user_sidebar` and `direct_rooms_control` Turbo frames, room-list containers, creation links, toggle control, and profile tools. With aligned fixture room names, the paired probe compared the entire sidebar frame's parsed tags, attributes, and non-whitespace text in five states and found them equal after normalizing only per-form CSRF tokens and direct-room epoch-millisecond sort values. A 1280×800 browser check showed the sidebar in the chat layout; at 390×844 the menu opened and closed, and the browser logged no page errors. The outer page, app JavaScript behavior, CSS details, and untested sidebar states may still differ.

The new-ping `direct_rooms_control` frame also matched the source's parsed tags, attributes, and non-whitespace text after normalizing only its CSRF token. A browser check opened it inline from the sidebar, selected and removed an autocomplete result, canceled back to the sidebar, and created a ping. A direct visit to `/rooms/directs/new` canceled back to the room. At 390×844 the inline form fit the sidebar without horizontal overflow; the browser logged no page errors. These checks do not establish full-page visual or interaction parity, and the form path was not timed.

A fresh 1,000-socket direct-room creation burst after this form change delivered **10,000/10,000** expected frames in each app, with identical sampled payloads and no missed, unexpected, or early-closed deliveries. Rustfire measured **133 ms elapsed / 21 ms p95** and Campfire **881 ms / 146 ms p95**. This is a short local regression trial, not a sustained capacity measurement.

The direct-room settings panel now matches Campfire's parsed tags, attributes, and non-whitespace text for pings with one, two, three, and five peers, after normalizing only the form's CSRF token and absolute origin. The source-shaped delete form returns the same HTTP 302 to `/` on both apps. A Rustfire browser check at 1280 px and 390 px showed linked member avatars, a working back link, the delete confirmation control, and no horizontal overflow or page errors. Three referenced source icons (`trash`, `remove`, and `remove-circle`) were added after the browser check exposed a missing trash icon. The generic outer page layout and CSS still differ from Campfire; these checks do not establish full-page parity or measure settings-page performance.

The paired direct-room deletion probe also received the same global `<turbo-stream action="remove" target="list_rooms_direct_2">` payload from both apps. The parsed `user_sidebar` frame matched after deletion, room 2 and its memberships were absent from both databases, and Rustfire's direct-room index entry was removed. This checks one deletion path and one subscribed global stream, without measuring deletion throughput or wider cleanup side effects.

The open and private room creation pages now have matching parsed panel tags, attributes, and non-whitespace text on a 51-user fixture, normalizing only the form CSRF token. Both rendered forms returned HTTP 302 and created the same room types and memberships: all 51 active users in the open room and users 1 and 42 in the private room. Rustfire's previous `/rooms/opens/new` and `/rooms/closeds/new` routes returned 400 because a numeric room-ID route captured `new`; explicit routes fixed that. A browser check covered filtering users, changing room type while retaining the typed name, submitting both forms, and a 390 px layout without horizontal overflow or page errors. Outer page HTML and styling still differ, and this workflow was not timed.

The paired probe now also compares four source-shaped edit pages: an open and a private edit route for each created room. Both the edit and delete panels matched in parsed tags, attributes, and non-whitespace text after normalizing only CSRF tokens and absolute form origins. Source-style POST forms with PATCH override converted the open room to private membership `[1, 42]` and the private room to open membership for all 51 active users, with the same 302 redirects and database state in both apps. The shared-room delete form returned 302 to `/`, and room 6 and its memberships were gone in both databases. In a Rustfire browser check, switching a private room to open preserved the edited name; saving landed on the room and gave it all 51 memberships. This checks selected workflows on one fixture, not all authorization and validation cases, full-page visual parity, or performance.

The paired edit probe also renamed the open and private rooms, then converted each to the other type, while subscribed to the signed global and per-user sidebar streams. Both apps emitted the same four `replace` events, byte for byte: open room 6 on the global stream, private room 7 on the user stream, converted private room 6 on the user stream, and converted open room 7 on the global stream. A separate `--operation update` fanout trial renamed one 51-member open room ten times after warmup, with 1,000 or 5,000 sockets subscribed to the global stream. It ran each app serially on one host with 22 Puma workers for Campfire and one release Rustfire process. The sampled `replace` event was **448 bytes** and byte-identical. Both trial orders at each socket count delivered every expected event, with zero missed, unexpected, or early-closed deliveries:

| Subscribed sockets | Expected deliveries per app | Trial order | Rustfire elapsed / p95 | Campfire elapsed / p95 |
| ---: | ---: | --- | ---: | ---: |
| 1,000 | 10,000 | Rustfire first | 137 / 21 ms | 757 / 109 ms |
| 1,000 | 10,000 | Campfire first | 132 / 20 ms | 694 / 100 ms |
| 5,000 | 50,000 | Rustfire first | 511 / 81 ms | 1,230 / 155 ms |
| 5,000 | 50,000 | Campfire first | 471 / 74 ms | 987 / 193 ms |

The interval covers ten sequential PATCH-override POSTs and delivery to the final socket, after two HTTP warmups, two subscribed-stream warmups, and a 500 ms settling interval. Socket setup is excluded. The final name and all 51 memberships matched in both databases. This establishes a faster matched update/sidebar-replacement burst on this fixture, not sustained capacity or whole-app superiority.

The room-route probe also found that the pinned Campfire build returns 302 to `/rooms/ID` for open and private room alias GETs; Rustfire now matches those statuses and locations. A direct-room alias GET returned Campfire's generic 500 page on two disposable runs, while Rustfire returned 303 to the usable room page. This is a known route divergence, not a successful parity check for that URL; the source error should be understood before any attempt to copy its behavior.

A fresh paired 1,000-socket, ten-room open-room creation burst after the form and redirect changes delivered **10,000/10,000** expected signed sidebar frames in each app with identical sampled payloads and no missed, unexpected, or early-closed deliveries. Rustfire measured **142 ms elapsed / 21 ms p95** and Campfire **579 ms / 100 ms p95**. This is a short localhost regression trial with one account, not a sustained capacity comparison.

With the source-shaped sidebar frame in the release build, a fresh reverse-order 1,000-socket direct-room creation burst delivered all **10,000/10,000** frames on each app. Rustfire measured **136 ms elapsed / 21 ms p95** and Campfire **733 ms / 115 ms p95**. Both received the same 815-byte average event payload and had zero missed, unexpected, or early-closed deliveries. The burst has the same short local scope described above.

## Paired direct-room creation and sidebar fanout

`bench/paired_direct_fanout.py` created ten direct rooms, each with the same two memberships, while 1,000 or 5,000 sockets subscribed as the creator to the signed per-user sidebar stream. It used the pinned Campfire checkout with 22 Puma workers and isolated Redis, and one Rustfire release process. The fixture aligned user names, avatar versions, and signing secret. Every subscribed socket received every `prepend` event, with no missed, duplicate, unexpected, or early-closed deliveries. The first sampled Turbo frame was byte-identical after normalizing only the independently generated epoch-millisecond room sort value; both values were checked against current time. Average delivered frame size was 815 bytes in each app.

| Subscribed sockets | Expected deliveries per app | Order | Rustfire elapsed / p95 | Campfire elapsed / p95 |
| ---: | ---: | --- | ---: | ---: |
| 1,000 | 10,000 | Rustfire first | 139 / 21 ms | 1,115 / 150 ms |
| 1,000 | 10,000 | Campfire first | 135 / 20 ms | 654 / 125 ms |
| 5,000 | 50,000 | Rustfire first | 474 / 73 ms | 1,421 / 178 ms |
| 5,000 | 50,000 | Campfire first | 517 / 84 ms | 1,095 / 171 ms |

Two HTTP creations before socket setup and two subscribed-stream creations after setup warmed each app. A 500 ms settling interval followed. The measured interval covers ten sequential POSTs, each on a fresh connection, and delivery to the final socket; per-delivery p95 starts when its room POST starts. Server startup, socket connection, warmup, and settling are excluded. These matched short bursts show faster direct-room creation and sidebar delivery in this fixture. They do not establish sustained throughput, a maximum connection count, memory use, or full application parity; all sockets represent the same account and receive the same per-user stream.

After changing the direct shortcut forms, the same ten-room probe delivered all expected events with identical sampled payloads and no delivery errors. With 1,000 sockets, Rustfire took **135 ms elapsed / 21 ms p95** versus Campfire's **1,058 ms / 146 ms**. With 5,000 sockets and Campfire run first, Rustfire took **539 ms / 84 ms p95** versus Campfire's **1,302 ms / 179 ms p95**. These fresh local runs confirm that the release build with the new forms still passes the short bursts, within the same limits above.

## Paired conditional message-list reads

`bench/paired_message_cache.py` seeded 40 matching messages with distinct subsecond creation times in disposable databases and ran the pinned Campfire build with isolated Redis. Rustfire now sends weak ETag, Last-Modified, and `Cache-Control: max-age=0, private, must-revalidate` headers on nonempty message pages. Both apps returned 304 with an empty body for matching ETag or Last-Modified, 200 for a stale date, and 204 for an empty page. The default and before/after pages contained the same ordered message IDs. After one message's update timestamp changed, both returned 200 with a new validator. The source and Rustfire ETag values and full HTML bodies differ: the 40-message 200 pages were **407,158** and **317,237 bytes**, respectively.

The first Rustfire implementation loaded the complete message rows before checking the validator. In one 15-second run with 22 Puma workers for Campfire, it served **4,015 / 3,984** conditional 304 requests/s at 32 / 128 clients versus Campfire's **4,233 / 4,327**. Rustfire then moved the matching-validator check to an indexed metadata query and skipped full message loading for 304. Fifteen-second serial same-host runs in opposite orders measured:

| Clients | Trial order | Rustfire requests/s / p95 | Campfire requests/s / p95 |
| ---: | --- | ---: | ---: |
| 32 | Campfire first | 28,496 / 1.57 ms | 4,319 / 15.04 ms |
| 128 | Campfire first | 28,588 / 7.11 ms | 4,597 / 39.96 ms |
| 32 | Rustfire first | 26,757 / 1.69 ms | 4,225 / 15.45 ms |
| 128 | Rustfire first | 27,757 / 7.31 ms | 4,567 / 40.95 ms |
| 256 | Campfire first | 28,071 / 15.41 ms | 4,430 / 124.70 ms |
| 512 | Campfire first | 27,518 / 36.45 ms | 4,650 / 152.61 ms |
| 256 | Rustfire first | 30,417 / 16.19 ms | 4,494 / 121.69 ms |
| 512 | Rustfire first | 29,994 / 34.51 ms | 4,539 / 157.06 ms |

Every measured response in those eight paired trials had the expected 304 status, empty body hash, ETag, and Last-Modified; there were zero errors. Two warmups per client and server startup were outside the timed intervals. At a provisional 20 ms p95 target, Rustfire passed through 256 tested clients in both orders and exceeded it at 512; Campfire passed 32 and exceeded it at 128, 256, and 512. Counts between 32 and 128 were not sampled. The load generator shared the server host, and these runs did not measure CPU, memory, maximum concurrency, long-lived sockets, writes, or mixed user activity. This establishes a faster feature-matched conditional read with a larger sampled concurrency margin on this fixture, not whole-app speed or sustained maximum capacity at complete parity.

A separate `bench/paired_bot_messages.py --messages 1000 --iterations 30 --clients 1 8 32 --seconds 3 --campfire-workers 22` regression check still matched the latest 40 parsed JSON messages after origin normalization, with zero measured response errors in both apps. At 32 clients Rustfire served **3,118** JSON reads/s (13.60 ms p95) versus Campfire's **1,550** (40.49 ms p95). The JSON bodies still differ in escaping and size, and this short trial is not a full-response parity or capacity claim.

The 200 response probe now compares parsed tag order, attributes, and non-whitespace text for all 40 messages after normalizing CSRF values, fixture-specific user and room names, signed avatar URLs, and server origins. Rustfire now includes the eight hidden CSRF fields per message that Campfire emits in the quick-boost forms. The 40-message probe passed with **407,158** source bytes and **339,637** Rustfire bytes. Raw HTML formatting and token values still differ; this check covers this text-message fixture, not every message presentation or the surrounding page. The earlier 304 throughput figures above were measured before this 200-rendering change; the 304 fast path does not render the HTML.

The same disposable 40-message fixture was then used for full HTTP 200 response load. Every measured response was checked for status, ETag, Last-Modified, 40 message roots, and 320 CSRF fields; both apps had zero errors. The Go client and server shared a host, 22 Puma workers served Campfire, each client warmed two requests outside the measured interval, and each run lasted five seconds. Both trial orders measured:

| Clients | Rustfire first | Rustfire pages/s / p95 | Campfire pages/s / p95 |
| ---: | :---: | ---: | ---: |
| 8 | No | 3,252 / 3.99 ms | 1,052 / 10.38 ms |
| 8 | Yes | 3,526 / 3.76 ms | 1,022 / 10.37 ms |
| 32 | No | 3,313 / 14.87 ms | 1,618 / 36.96 ms |
| 32 | Yes | 3,265 / 14.89 ms | 1,606 / 34.11 ms |
| 64 | No | 3,052 / 30.83 ms | 1,559 / 84.81 ms |
| 64 | Yes | 3,481 / 26.92 ms | 1,500 / 85.58 ms |
| 128 | No | 2,547 / 89.54 ms | 1,577 / 127.41 ms |
| 128 | Yes | 3,334 / 59.70 ms | 1,564 / 127.03 ms |

At a provisional 20 ms p95 target, Rustfire passed at 32 tested clients in both orders, and Campfire passed at 8; 16 clients were not tested. This adds feature-matched rendered-page evidence beyond the 304 path. It remains a short single-room, single-account read workload with different raw response sizes and no CPU, RSS, write, or socket measurement. It does not establish sustained multi-user capacity or whole-app superiority at complete parity.

The same paired full-200 fixture was run again for ten seconds per client count with `--resources` and both trial orders. Sampled peak PSS includes Puma's 22 workers and parent plus isolated Redis for Campfire; Rustfire had one server process. CPU seconds cover the Go client's two warmups per connection as well as the timed interval, while request rate and p95 cover only the timed interval. The client process is excluded from server resource totals.

| Clients | Rustfire first | Rustfire pages/s / p95 | Rustfire CPU s / peak PSS MiB | Campfire pages/s / p95 | Campfire CPU s / peak PSS MiB |
| ---: | :---: | ---: | ---: | ---: | ---: |
| 32 | No | 3,337 / 14.36 ms | 209.54 / 29.95 | 1,780 / 31.80 ms | 202.43 / 2,679.37 |
| 32 | Yes | 3,449 / 13.37 ms | 218.23 / 30.31 | 1,704 / 27.63 ms | 204.86 / 2,743.26 |
| 64 | No | 3,338 / 27.44 ms | 263.55 / 31.23 | 1,778 / 52.87 ms | 216.80 / 2,766.21 |
| 64 | Yes | 3,292 / 27.56 ms | 262.12 / 31.84 | 1,783 / 62.39 ms | 219.28 / 2,825.38 |

All measured responses passed status, validator, message-root, and CSRF-field checks with zero errors. This fixture shows more rendered-page throughput and a much smaller sampled server process footprint for Rustfire under the tested 22-worker Campfire configuration. It still does not measure a separate load-generator host, many simultaneous users and rooms, write or socket activity, or hours of steady load; the source and Rustfire also send different raw byte counts per page.

## Paired message creation and mixed reads/writes

`bench/paired_message_create.py` found that Rustfire's browser-style Turbo POST returned HTTP 201 with `text/html`, while Campfire returned HTTP 200 with `text/vnd.turbo-stream.html`. Rustfire now matches the source status and media type. On aligned disposable fixtures, both apps saved message ID 1 and the same client ID and text; their parsed Turbo response tags, attribute names and static values, and text matched after excluding generated timestamps. Raw response formatting still differs.

`bench/paired_message_mix.py` then seeded 40 matching messages and ran 32 concurrent full-HTML readers beside one writer scheduled for 100 messages over about 9.5 seconds. All POSTs returned HTTP 200 with Turbo Stream content, all measured GETs returned HTTP 200 with 40 message roots, all 100 rows and bodies were present afterward, and the final page contained the expected latest 40 IDs in each app. Ten-second serial same-host trials in both orders measured:

| Rustfire first | Rustfire reads/s / read p95 | Rustfire write p95 | Campfire reads/s / read p95 | Campfire write p95 |
| :---: | ---: | ---: | ---: | ---: |
| No | 3,249 / 14.43 ms | 7.46 ms | 1,359 / 49.20 ms | 122.56 ms |
| Yes | 3,414 / 13.68 ms | 7.60 ms | 1,531 / 41.83 ms | 84.62 ms |

Read and write error counts were zero in both trials. Campfire's fragment cache sometimes omitted the eight hidden CSRF fields for a newly created message, so the changing-page read check requires 40 message roots but does not require a fixed CSRF-field count; the initial pages passed the full parsed-markup comparison. This is one writer and one account in one room on a shared host, with no socket fanout or resource sample during the mix. It does not prove sustained multi-user capacity or complete feature parity.
