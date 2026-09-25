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

The selected-room lookup is much faster in Rustfire at these fixture sizes. This is a **single-client ping-reuse comparison**, not proof of full-app speed or user capacity. These latest figures include a user lookup that matches Campfire's filtering of selected IDs. Both apps returned HTTP 302 with an empty body. Campfire renders and publishes Turbo sidebar broadcasts for each participant on reuse; Rustfire renders a per-user room link and sends a lighter room-list event, and no clients were subscribed during the trial. The apps therefore still perform different work. The load generator and servers shared the host, and no sustained concurrency sweep was run.

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
