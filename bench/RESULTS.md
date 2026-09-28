# Preliminary Rustfire measurements

## Passing current-build 8,000-socket rich browser-channel load

At Rustfire commit `e8456f6`, `python bench/paired_message_multi.py --rooms 4 --users 4 --clients 64 --seconds 30 --write-rate 0.5 --sockets-per-room 2000 --socket-users-per-room 4 --browser-channels --rich-writes --campfire-workers 22 --resources` passed all strict gates against pinned Campfire `91d294f` in both server orders; the second command added `--rustfire-first`. The four rooms scheduled 15 rich posts each. Each app and order saved all 60 posts, delivered all **120,000 message appends**, **120,000 unread events**, and **2,004,000 presence read events** to 8,000 authenticated sockets, and had zero checked read errors, missed or unexpected socket events, or early closes. Each run compared 240 source and target append structures and validated the final pages and saved rows.

| Server order | Rustfire checked reads/s / p95 | Campfire checked reads/s / p95 | Rustfire final write / write p95 | Campfire final write / write p95 | Peak server PSS, Rustfire / Campfire |
| --- | ---: | ---: | ---: | ---: | ---: |
| Campfire first | 2,559 / 35.4 ms | 1,231 / 108.8 ms | 27.57 s / 39.1 ms | 29.14 s / 572.7 ms | 1,278 / 6,501 MiB |
| Rustfire first | 2,602 / 34.8 ms | 1,151 / 139.6 ms | 27.63 s / 44.0 ms | 28.33 s / 559.1 ms | 1,276 / 5,926 MiB |

Rustfire served **2.08–2.26×** as many checked reads per second and used **4.64–5.08×** less sampled peak server PSS in this passing, feature-matched workload. The [Campfire-first](results/rich-browser-8k-rate05-2026-09-27-camp-first.json) and [Rustfire-first](results/rich-browser-8k-rate05-2026-09-27-rust-first.json) reports contain the full counters and gates. In a separate otherwise identical pair at `--write-rate 1`, both apps saved and delivered all 120 posts, but Campfire's final write crossed the 30-second deadline by 31 ms when second, while both apps passed when Campfire ran first. Those [Campfire-first](results/rich-browser-8k-rate1-2026-09-27-camp-first.json) and [Rustfire-first](results/rich-browser-8k-rate1-2026-09-27-rust-first.json) reports are retained without treating the failed ordering as a passing throughput point. Both apps and load clients shared one 32-logical-CPU host, socket setup was outside the timed interval, and Campfire used 22 Puma workers plus isolated Redis while Rustfire used one release process. This measures neither connection setup rate nor a production maximum, hours-long stability, separate-host capacity, or whole-app performance at complete parity.

## Current 8,000-socket rich-text browser-channel comparison

At Rustfire commit `562ddcf`, the paired four-room, four-user workload ran for 30 measured seconds with 64 checked HTML readers, three rich posts per second per room, and 2,000 authenticated sockets per room subscribed to eight browser channels. Campfire used 22 Puma workers and isolated Redis; Rustfire used one release process. Both apps ran serially in each server order on the same 32-logical-CPU host as the load clients. Socket setup preceded the timed interval. The command was `python bench/paired_message_multi.py --rooms 4 --users 4 --clients 64 --seconds 30 --write-rate 3 --sockets-per-room 2000 --socket-users-per-room 4 --browser-channels --rich-writes --campfire-workers 22 --resources`, repeated with `--rustfire-first`.

| Server order | Rustfire reads/s / p95 | Campfire reads/s / p95 | Rustfire write p95 / last write | Campfire write p95 / last write | Peak PSS, Rustfire / Campfire |
| --- | ---: | ---: | ---: | ---: | ---: |
| Campfire first | 2,298 / 48.9 ms | 323 / 708.0 ms | 38.5 ms / 29.21 s | 642.8 ms / **36.49 s** | 1,276 / 5,786 MiB |
| Rustfire first | 2,098 / 57.2 ms | 203 / 914.9 ms | 64.7 ms / 29.23 s | 663.8 ms / **34.08 s** | 1,282 / 6,018 MiB |

Rustfire met the 30-second writer deadline in both orders; Campfire missed it by 4.08–6.49 seconds, although both saved all 360 posts. Each app delivered all **720,000 message appends**, **720,000 unread events**, and **2,004,000 presence read events** per run to the 8,000 sockets, with no missed or unexpected socket events or early socket closes. All 1,440 paired append structures matched. Rustfire had zero checked read errors; Campfire had zero when first and three when second. The strict paired command exited nonzero in both orders because of Campfire's missed deadline, and the second order also missed the read-error gate. The [Campfire-first report](results/rich-browser-8k-2026-09-27-camp-first.json) and [Rustfire-first report](results/rich-browser-8k-2026-09-27-rust-first.json) retain the counts, latencies, resources, and gate outcomes.

This is a current-build regression check at 8,000 sockets; earlier sections report separate 12,000- and 16,000-socket trials on older builds. It demonstrates Rustfire meeting the tested write deadline at this point while Campfire did not on this host and configuration. It does not locate either app's maximum connection count or establish hours-long stability, separate-generator capacity, or a whole-app performance advantage at complete feature parity. The load generators and unrelated host processes shared the server machine; the two Campfire read rates also varied substantially between orders.

## Current 16,000-socket rich-text browser-channel comparison

At Rustfire commit `82e73ae`, the same four-room, four-user workload ran with 4,000 sockets per room, 64 checked HTML readers, and three scheduled rich posts per second per room for 30 seconds. Sixteen 1,000-socket capture groups subscribed to the eight browser channels before timing began. Each app ran serially on the same 32-logical-CPU host with Campfire's 22 Puma workers and isolated Redis; the command above changed only `--sockets-per-room` to `4000`, and a second run added `--rustfire-first`.

| Server order | Rustfire reads/s / p95 / errors | Campfire reads/s / p95 / errors | Rustfire acknowledged posts / last write | Campfire acknowledged posts / last write | Delivered message appends, Rustfire / Campfire | Peak PSS, Rustfire / Campfire |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Campfire first | 1,998 / 65.9 ms / 0 | 259 / 926.9 ms / 6 | 360 / 29.21 s | 270 / 47.67 s | 1,440,000 / 1,084,000 | 2,493 / 7,694 MiB |
| Rustfire first | 2,060 / 63.9 ms / 0 | 226 / 832.4 ms / 15 | 360 / 29.21 s | 180 / 32.34 s | 1,440,000 / 728,000 | 2,501 / 7,316 MiB |

Rustfire passed its read, writer-deadline, saved-message, and socket-delivery checks in both orders. It delivered all 1.44 million expected message appends and 1.44 million unread events per run, plus all 8.008 million expected presence read events, with no early socket closes. Campfire's room writer timed out in one room when run first and two rooms when run second. Some timed-out requests persisted a message without an acknowledged response, so its acknowledged counts are not exact saved-row counts. The final Campfire database did not reach the expected 90 messages in every room; its socket captures therefore missed scheduled appends and unread events. All 8.008 million setup presence read events arrived in each app and order. Both paired commands exited nonzero on Campfire's strict read, write, delivery, and saved-state gates. No paired append-markup comparison was counted at this point because the source run was incomplete; lower-scale trials provide that check. The [Campfire-first report](results/rich-browser-16k-2026-09-27-camp-first.json) and [Rustfire-first report](results/rich-browser-16k-2026-09-27-rust-first.json) retain every per-room and per-socket result.

This demonstrates a tested 16,000-socket workload that Rustfire completed twice while Campfire did not complete it on this host. The Campfire read rates were observed alongside errors and incomplete writes, so they are not a clean feature-equivalent throughput ratio. The run does not establish either maximum connection count, hours-long stability, a separate-host result, or whole-app parity.

## Current 400-socket rich-text browser-channel comparison

On the release build at commit `6a3bbd3`, the four-room, four-user 30-second rich-text mix was rerun in both server orders with 64 checked readers, three posts per second per room, and 100 authenticated sockets per room on eight source-shaped browser channels. Both apps saved all 360 posts within the deadline, delivered all **36,000 message appends and 36,000 unread events**, and emitted all **5,200 expected presence read events** during setup. The harness compared 1,440 paired append structures and every timed read's status, media type, and message count. Both orders had zero checked read errors, missing events, or early socket closes.

| Server order | Rustfire reads/s / p95 | Campfire reads/s / p95 | Rustfire write p95 | Campfire write p95 | Rustfire / Campfire peak PSS |
| --- | ---: | ---: | ---: | ---: | ---: |
| Campfire first | 2,669 / 33.84 ms | 1,276 / 107.50 ms | 36.54 ms | 152.25 ms | 117 / 3,828 MiB |
| Rustfire first | 2,736 / 33.15 ms | 1,496 / 84.16 ms | 35.86 ms | 157.93 ms | 117 / 3,967 MiB |

Rustfire served **1.83–2.09×** as many checked reads per second in this sampled mix. Reproduce with `python bench/paired_message_multi.py --rooms 4 --users 4 --clients 64 --seconds 30 --write-rate 3 --sockets-per-room 100 --socket-users-per-room 4 --browser-channels --rich-writes --campfire-workers 22 --resources`, then add `--rustfire-first`. The [Campfire-first report](results/rich-browser-400-2026-09-27-camp-first.json) and [Rustfire-first report](results/rich-browser-400-2026-09-27-rust-first.json) contain the raw counts, latency distributions, deliveries, and resource samples. The apps and load clients shared one 32-logical-CPU host; Campfire used 22 Puma workers and isolated Redis, while Rustfire used one release process. Socket setup preceded the measured interval. This is a 30-second, 400-socket sample, not a sustained capacity limit or whole-app speed claim at complete parity.

## Current 4,000-socket rich-text browser-channel comparison

The same release build was tested with 1,000 sockets per room, 4,000 total, distributed across four authenticated identities per room. Both server orders saved all 360 rich posts, delivered all **360,000 message appends and 360,000 unread events**, and emitted all **502,000 expected presence read events** during socket setup. Neither app had a checked read error, missed or unexpected socket event, or early socket close. All 1,440 paired append structures matched. Campfire's final writer missed the strict 30-second deadline by 75 ms when Rustfire ran first; the command therefore exited nonzero for that order despite complete delivery and persistence.

| Server order | Rustfire reads/s / p95 | Campfire reads/s / p95 | Rustfire write p95 / final write | Campfire write p95 / final write | Rustfire / Campfire peak PSS |
| --- | ---: | ---: | ---: | ---: | ---: |
| Campfire first | 2,115 / 53.8 ms | 800 / 233.4 ms | 53.6 ms / 29.192 s | 408.3 ms / 29.416 s | 669 / 5,167 MiB |
| Rustfire first | 2,485 / 42.5 ms | 793 / 216.3 ms | 44.3 ms / 29.230 s | 479.3 ms / **30.075 s** | 668 / 5,119 MiB |

Rustfire served **2.64–3.13×** as many checked reads per second at this sampled connection count. Reproduce with the 400-socket command above, changing `--sockets-per-room 100` to `1000`, and repeat with `--rustfire-first`. The [Campfire-first report](results/rich-browser-4k-2026-09-27-camp-first.json) and [Rustfire-first report](results/rich-browser-4k-2026-09-27-rust-first.json) retain the strict gate outcomes. The 75 ms Campfire miss is too small to establish a reliable write-capacity boundary. Each app ran serially on the same host as the load clients; connection setup was outside the timed interval. This does not establish maximum sockets, hours-long stability, or whole-app parity.

## Five-minute 4,000-socket rich-text browser-channel comparison

At Rustfire commit `c642681`, `python bench/paired_message_multi.py --rooms 4 --users 4 --clients 64 --seconds 300 --write-rate 3 --sockets-per-room 1000 --socket-users-per-room 4 --browser-channels --rich-writes --campfire-workers 22 --resources` ran against pinned Campfire `91d294f`; a second run added `--rustfire-first`. Each app ran serially on the same 32-logical-CPU host as the load clients. Socket setup and warmup preceded the measured five-minute interval. Every app and order saved all 3,600 rich posts, delivered all **3.6 million message appends and 3.6 million unread events** to 4,000 sockets, and had zero checked read errors or early socket closes. Each ordering passed 14,400 paired sampled append-structure comparisons and all final-page and saved-message checks.

| Server order | Rustfire reads/s / p95 | Campfire reads/s / p95 | Rustfire write p95 / last write | Campfire write p95 / last write | Rustfire / Campfire peak PSS | Strict result |
| :--- | ---: | ---: | ---: | ---: | ---: | :--- |
| Campfire first | 2,386 / 44.70 ms | 778 / 237.99 ms | 48.36 ms / 299.23 s | 310.83 ms / **300.40 s** | 672 / 5,680 MiB | Failed: Campfire write deadline |
| Rustfire first | 2,410 / 43.20 ms | 778 / 242.82 ms | 44.27 ms / 299.20 s | 330.54 ms / 299.35 s | 674 / 5,794 MiB | Passed |

Rustfire served **3.07–3.10×** as many checked reads per second and met the 300-second write deadline in both orders. Campfire met it when second and missed by 0.40 seconds when first, while still saving and delivering every write. The [Campfire-first raw report](results/rich-browser-4k-300s-2026-09-27-camp-first.json) and [Rustfire-first raw report](results/rich-browser-4k-300s-2026-09-27-rust-first.json) retain counts, latency distributions, sampled server CPU/PSS, delivery checks, and strict gate outcomes. Campfire used 22 Puma workers and isolated Redis; Rustfire used one release process. This is a five-minute same-host workload with four rooms and four users, not an hours-long stability test, maximum socket limit, separate-generator benchmark, or full-app performance claim at complete parity.

## Paired page snapshots

`python bench/paired_visual_pages.py --output-dir /tmp/rustfire-visual-boost` compared fifteen matched Chromium pages at 1280×800 against the pinned source. The normalized pixel differences ranged from 0% to 0.124%; both boost pages, the custom CSS editor, bot list, open-room edit, both new-room forms, new private-chat form, notification-setting page, and push-subscription page matched pixel for pixel. Before the shell changes, the two boost pages differed by 12.705% and 12.842%, the notification-setting page by 12.737%, and the custom CSS editor by 1.039% of pixels. At 390×844, the fifteen pages differed by 0% to 0.280%, with the room page having the largest difference. The fixture aligns timestamps, account join code, and the sampled search result; generated product names and local URL values account for some remaining differences. `python bench/paired_boost_controls.py`, `python bench/paired_direct_sidebar.py`, `python bench/paired_involvement.py`, and `python bench/paired_custom_styles.py` passed their relevant form, transition, and persistence checks. These screenshots cover two viewports and one fixture, not every application state or device behavior.

The `--no-rooms` fixture compared the signed-in welcome screen separately. Rustfire's old page differed by 13.420% of pixels at 1280×800. After matching Campfire's empty message area, sidebar frame, and packaged empty-message SVG, both apps' screenshots matched pixel for pixel at 1280×800 and 390×844. The SVG is byte-identical to the pinned source asset. This fixture removes the administrator's room membership while retaining other account records; it does not cover every way an account can reach a no-room state.

## Room-page read check after query-format parity

After the 3,348-case GET format sweep passed across three roles, `python bench/paired_room_shell.py --read-clients 32 --seconds 5 --campfire-workers 22` ran in both server orders on the release build. The probe checked every successful room-page read and matched the sampled parsed page sections before timing. Each app had zero read errors.

| Server order | Rustfire reads/s / p95 | Campfire reads/s / p95 |
| :--- | ---: | ---: |
| Campfire first | 3,482.6 / 11.22 ms | 1,302.6 / 50.50 ms |
| Rustfire first | 3,606.5 / 10.82 ms | 1,203.8 / 57.93 ms |

Rustfire served 2.67–3.00× as many checked reads in these short one-message-room trials. This verifies the routing change did not erase the advantage at this sampled load; it does not establish full HTML equality, sustained capacity, or whole-app superiority.

## Room creation after preserving imported ID high-water marks

After Rustfire began retaining Campfire's deleted-user and deleted-room ID high-water marks, `python tests/import_campfire.py` imported a fixture whose highest live user and room IDs were 3 and 1 but whose saved sequences were 40 and 50. Creating a bot and open room through Rustfire's HTTP routes then assigned IDs 41 and 51. The 46 Rust unit tests and paired first-run and bot-administration probes passed with the changed allocator.

The same importer check now sets Campfire's push-subscription sequence to 60 with only IDs 1 and 2 live. A new registration through the imported Rustfire account receives ID 61, preserving the identity used by subscription delete and test-notification controls. `python bench/paired_push_registration.py` also passes after a release rebuild, including upgrade of Rustfire's older unique-endpoint table and Campfire's exact-match touch behavior.

The importer fixture now includes two bot users whose avatars reference one source Active Storage blob. `python tests/import_campfire.py` confirmed both imported edit pages use the same source-key-signed original-file URL and that following it serves the original PNG bytes. Deleting one avatar left the other preview, its blob row, and its rendered WebP avatar usable; deleting the last removed the blob row and stored file. The fixture also checks that an ordinary imported user avatar is stored in Rustfire's avatar directory. These are local import and HTTP checks on sampled records; a full imported account and broader media formats still need validation. The importer test's message write requests now use the source-compatible Turbo Stream and HTML formats.

`python bench/paired_import_roundtrip.py --sample-dir /tmp/rustfire-import-roundtrip` now creates a bot with a PNG avatar, a rich-text message, and a text-file message through pinned Campfire's HTTP routes, then stops it and imports its SQLite database and Active Storage files. The importer reported 52 users, two messages, one file attachment, one avatar, one push subscription, and three sessions. The imported Rustfire server accepted the copied signed session. Both apps served the same original PNG and text-file bytes from identical source-signed blob paths. Complete parsed head and body comparisons passed for the bot list (146/180 tokens), bot editor (146/213), and room (148/769). The comparator normalizes listen origins, generated times, signed avatar and blob values, product names, and the source's intermittent boost-form CSRF inputs; both servers used the same VAPID key and browser user agent. This is a live-to-import sample with two message types, not comprehensive migration parity or a performance comparison.

The same parity gate preceded `python bench/paired_import_roundtrip.py --read-clients 32 128 256 --seconds 10 --report bench/results/imported-room-reads.json`. One Rustfire release process and Campfire with 22 Puma workers ran serially on the same 32-logical-CPU host as the Go load generator. Each keep-alive client made two checked warmup reads. Every measured response had HTTP 200, HTML media type, and both imported message IDs; all 12 trials had zero errors. Both server orders used the same browser user agent and ten-second interval at each concurrency point.

| Clients | Rustfire reads/s | Campfire reads/s | Rustfire / Campfire | Rustfire p95 | Campfire p95 |
| :--- | ---: | ---: | ---: | ---: | ---: |
| 32 | 3,273–3,285 | 980–1,178 | 2.78–3.35× | 12.1–12.3 ms | 56.3–64.5 ms |
| 128 | 3,102–3,267 | 1,160–1,177 | 2.64–2.82× | 61.7–66.0 ms | 213.1–312.6 ms |
| 256 | 3,212–3,232 | 1,109–1,236 | 2.62–2.90× | 136.0–138.4 ms | 328.3–379.5 ms |

The [raw report](results/imported-room-reads.json) contains per-order counts and latencies. This measures one imported room page with two messages; it does not establish maximum scale, long-duration stability, equal raw HTML bytes, or a whole-app performance advantage at full parity.

## Imported 40-message room reads

`python bench/paired_import_roundtrip.py --extra-messages 38 --read-clients 32 128 256 --seconds 10 --report bench/results/imported-room-40-reads.json` extends the live Campfire account above to 39 rich messages and one real text-file attachment. After the offline import, both apps served identical source-signed avatar and attachment paths with byte-identical originals. Their complete parsed room heads and bodies matched at **148 and 7,609 tokens** after the documented generated-value normalizations. The captured raw room documents were 422,032 bytes from Campfire and 375,598 bytes from Rustfire.

The parity check ran before the timed reads. One Rustfire release process and Campfire with 22 Puma workers ran serially on the same 32-logical-CPU host as the Go keep-alive client, in both server orders. Each client made two checked warmup requests. Every measured response had HTTP 200, HTML media type, and all 40 rendered message IDs; the 12 ten-second trials had zero errors.

| Clients | Rustfire reads/s | Campfire reads/s | Rustfire / Campfire | Rustfire p95 | Campfire p95 |
| :--- | ---: | ---: | ---: | ---: | ---: |
| 32 | 2,227–2,277 | 489–744 | 3.06–4.55× | 20.6–21.4 ms | 93.7–168.5 ms |
| 128 | 2,606–2,636 | 701–720 | 3.62–3.76× | 74.8–76.5 ms | 349.6–389.0 ms |
| 256 | 2,638–2,639 | 660–666 | 3.97–3.99× | 161.6–161.8 ms | 574.3–594.0 ms |

The [raw report](results/imported-room-40-reads.json) preserves per-order counts and latencies. The 32-client Campfire rate varied substantially by server order; the ranges retain that variation. These trials show a speed advantage for this imported, fully populated room-page workload. They do not establish a maximum user count, hours-long stability, equal raw HTML bytes, or a whole-app advantage at full feature parity.

## Imported 41-message paging boundary

`python bench/paired_import_roundtrip.py --extra-messages 39 --sample-dir /tmp/rustfire-import-41` imported 41 real Campfire messages to cross the 40-message room-page boundary. Both apps rendered the latest 40 messages in matching parsed room heads and bodies (148 and 7,499 tokens). The `before=` request returned the same single older message (179 parsed body tokens), and the `after=` request returned matching content (7,022 parsed body tokens). A request before the oldest message returned HTTP 204 in both apps. The source-signed bot avatar and message attachment paths and original bytes also matched after import.

Both apps supplied weak ETags and the same Last-Modified value on the imported `before=` and `after=` responses. Each returned an empty HTTP 304 for its own `If-None-Match` and `If-Modified-Since` requests. After Rustfire adopted Campfire's versioned-message-key and template-digest validator, a fresh run matched the **exact ETag strings** on both pages. The source ETag formula is also checked against the imported Campfire database before comparing the apps. The raw paged HTML still differs: 9,499 versus 9,124 bytes for the older page and 371,359 versus 356,772 bytes for the `after=` page (Campfire versus Rustfire, from the preceding sample). This is a functional paging check, not a throughput or capacity measurement.

## Large imported message history

`python bench/paired_large_import.py` copies the pinned Campfire database schema to a disposable fixture and seeds **16,230** messages with distinct creation times and rich text every tenth message. Campfire served 406 consecutive 40-message pages, including the final 30-message page, before returning HTTP 204. The stopped fixture imported into Rustfire in **0.75–1.17 seconds** across two local runs; Rustfire then served the same 406 pages and 204 terminator. Across all 16,230 messages, every paired page matched its ordered IDs, exact ETag, Last-Modified value, and complete parsed body after the documented generated-value normalization. Campfire transmitted 168,159,226 raw HTML bytes across the pages versus Rustfire's 143,294,460; the templates serialize whitespace differently.

This is a deterministic, seeded history rather than a production account. It exercises full pagination and offline rich-text migration at this data size; it does not validate attachments, boosts, multiple rooms, search results, concurrent users, or sustained capacity for a large imported account. The smaller live Campfire roundtrip above covers one attachment and avatar.

`python bench/paired_large_import.py --read-clients 32 128 --read-seconds 5 --campfire-workers 22 --report bench/results/large-import-message-reads.json` then ran checked latest-page GETs against the same 16,230-message fixture, after the 406-page parity sweep. One Rustfire release process and Campfire's 22 Puma workers ran serially in both orders on the same 32-logical-CPU host as the Go client. Each connection warmed twice outside the five-second trial. Full 200 responses had the expected ETag, Last-Modified, 40 message roots, and 320 CSRF fields; conditional 304 responses had the same validator and an empty body. All 16 trials had zero checked errors.

| Response | Clients | Rustfire requests/s | Campfire requests/s | Rustfire / Campfire | Rustfire p95 | Campfire p95 |
| :--- | ---: | ---: | ---: | ---: | ---: | ---: |
| Full 200 | 32 | 2,794–2,809 | 1,012–1,109 | 2.53–2.76× | 17.6–17.7 ms | 53.7–58.7 ms |
| Full 200 | 128 | 2,830–2,832 | 939–946 | 2.99–3.01× | 71.0–71.1 ms | 255.6–311.6 ms |
| Conditional 304 | 32 | 17,210–17,516 | 1,276–1,636 | 10.71–13.49× | 2.41–2.49 ms | 46.5–52.8 ms |
| Conditional 304 | 128 | 17,186–17,221 | 1,663–1,704 | 10.11–10.33× | 11.69–11.70 ms | 147.7–182.1 ms |

The [raw paired report](results/large-import-message-reads.json) retains both orders and exact counts. This is a short, single-room, same-host read workload. It shows faster checked message-page reads at this database size, not maximum sustained capacity, mixed traffic, or a whole-app advantage at full feature parity. The full 200 HTML bodies differ in serialization and raw size despite matching parsed content.

## Large imported multiroom history

`python bench/paired_large_graph_import.py --messages-per-room 4000 --read-clients 32 128 --read-seconds 5 --campfire-workers 22 --report bench/results/large-graph-import-reads.json` seeded 16,000 distinct-time messages across an open room, two private rooms, and a direct room. Two users authored the messages; the administrator could reach three rooms and the member all four. Every one of the **700** reachable message pages matched its ordered IDs, exact ETag and Last-Modified values, and complete parsed body after generated-value normalization. The stopped database imported in **0.78 seconds** on this local run.

One Rustfire release process and Campfire's 22 Puma workers then served seven user-room latest-page targets in both server orders, with a local Go client. Each connection warmed twice outside the five-second trial. Every measured 200 response matched its target's ETag, Last-Modified, HTML media type, 40 message roots, and 320 hidden CSRF fields; all eight trials had zero checked errors.

| Clients | Rustfire reads/s | Campfire reads/s | Rustfire / Campfire | Rustfire p95 | Campfire p95 |
| ---: | ---: | ---: | ---: | ---: | ---: |
| 32 | 2,755–2,769 | 1,248–1,315 | 2.10–2.22× | 17.94–18.00 ms | 45.12–47.87 ms |
| 128 | 2,861–2,871 | 1,146–1,175 | 2.44–2.50× | 69.74–69.92 ms | 187.01–217.19 ms |

The [raw report](results/large-graph-import-reads.json) retains each order and exact counts. This adds a multiroom, two-user imported read workload at roughly the size of the single-room history above. It does not measure concurrent writes, attachments, socket fanout, CPU or memory, hours of steady load, or a maximum connection count. The apps still send different raw HTML byte counts per page, and this result does not establish whole-app speed at full feature parity.

## Large imported multiroom search

`python bench/paired_large_graph_import.py --messages-per-room 4000 --search-clients 32 128 --read-seconds 5 --campfire-workers 22 --search-report bench/results/large-graph-import-search-reads.json` repeated the 16,000-message, four-room import with a matching source and imported VAPID key. All 700 message pages still matched. Four search pages then matched in complete parsed head and body: broad searches returned the newest 100 visible messages for each user; the administrator's search for room 4 returned none, while the member's returned that private room's newest 100. The parsed body token counts were **17,815**, **88**, **17,812**, and **17,818** respectively.

The three nonempty search targets were then read in both server orders, with 22 Campfire Puma workers, one Rustfire release process, and the Go client on the same 32-logical-CPU host. Each connection warmed twice outside its five-second trial. Every measured 200 response matched its target's HTML media type, 100 message roots, CSRF-field count, and newest-message text marker. All eight trials had zero checked errors.

| Clients | Rustfire reads/s | Campfire reads/s | Rustfire / Campfire | Rustfire p95 | Campfire p95 |
| ---: | ---: | ---: | ---: | ---: | ---: |
| 32 | 563–568 | 313–326 | 1.75–1.80× | 154.68–154.82 ms | 216.99–227.10 ms |
| 128 | 408–415 | 279–293 | 1.41–1.46× | 499.73–511.18 ms | 768.15–801.33 ms |

The [raw search report](results/large-graph-import-search-reads.json) retains each order and exact counts. This is a short, read-only search workload with three result-heavy queries; it does not establish sustained search capacity, mixed write or socket behavior, CPU or memory efficiency, or whole-app superiority at complete feature parity.

The multiroom import probe also passed with 61 messages per room in three VAPID states: no configured source key and no subscriptions, a configured key with no subscriptions, and a configured key with one subscription. Each state matched 14 paginated message views and four complete parsed search pages. The no-key import persisted a `preserve_missing_vapid` flag, created no key file, and rendered the source's `<meta name="vapid-public-key">` without a `content` attribute. The configured states retained the supplied key even when no subscription existed. A separate fresh Rustfire database still generated and exposed its own key. This closes the sampled keyless-import metadata difference; live browser push delivery remains unverified.

## Imported multi-room account graph

`python bench/paired_import_graph.py` seeded a disposable pinned Campfire account with four rooms: open, private, direct, and a private room inaccessible to the signed-in administrator but visible to a second member. It added five rich-text messages, a boost, two equally timestamped recent searches, visible and unread memberships, and one push subscription so the imported account retains Campfire's VAPID key. After offline import, the source and Rustfire had identical saved room, membership, message, boost, and search rows. The administrator's search results included the three reachable messages and excluded the hidden room's message; the member's results included all four matching messages.

The parsed heads and bodies matched for the open room (**148 / 463 tokens**), private room (**148 / 781**), direct room (**148 / 466**), private room centered on its first message (**148 / 781**), and search page (**146 / 682**). The `user_sidebar` Turbo frame matched at **334 tokens**. An inaccessible room redirected to each app's absolute root URL. This probe exposed three Rustfire differences, now fixed: relative instead of absolute room-show redirects, reversed order for recent searches with equal timestamps, and a relative clear-history form action when recent searches exist. The existing search-history and room-redirect probes still pass.

The second member's parsed room heads and bodies matched for open (**148 / 463**), private (**148 / 773**), direct (**148 / 466**), centered private (**148 / 773**), and the fourth private room (**148 / 463**). The member's search page matched at **146 / 821** tokens, and the sidebar frame at **339** tokens. This checks two signed-in perspectives of one imported graph. It does not establish every membership transition, full migration of arbitrary production data, or performance for this account shape.

The same fixture then restarted Campfire after import and posted a new private-room message as the member to each app, using each app's rendered CSRF token. The Turbo create responses matched at **181 parsed tokens**. Both saved the same next message ID beyond the fixture's first five, indexed the same plain text, and marked the administrator's previously read room unread while leaving the author's room read. Afterward, the administrator's and member's private room bodies matched at **956** and **948** parsed tokens, their sidebar frames at **334** and **339**, and their search bodies at **857** and **996**. The search results included the new message for both users.

The imported administrator then changed the private room's involvement from `everything` to `nothing`, `invisible`, and `mentions`. Each 11-token notification frame, redirect path, and saved value matched Campfire. At `invisible`, both apps removed the room link from that administrator's sidebar (**329 parsed tokens**, down from **334**) while keeping the room page (**956 body tokens**) and searchable messages (**857 body tokens**) accessible. Returning to `mentions` restored the sidebar link and 334-token frame. The other member's sidebar remained at **339 matching tokens** throughout. This covers one post-import message write and three involvement transitions, not full mutation or live notification parity.

`python bench/paired_sidebar_fanout.py --sockets 100 --rooms 10 --campfire-workers 22` passed in both server orders using the release build. Each app created ten open rooms with 51 memberships each and delivered all 1,000 expected, source-shaped sidebar events with no missed or unexpected events. The sampled payloads matched byte for byte. The measured ten-request interval was 50 ms for Rustfire versus 640 ms for Campfire with Rustfire first, and 56 ms versus 664 ms with Campfire first. This short same-host burst checks room creation and delivery at 100 sockets; it does not establish sustained or maximum capacity.

## Paired QR code response parity

`python bench/paired_qr.py` compared ten invitation-like URLs, alphanumeric strings, and numeric strings against pinned Campfire's `rqrcode` 3.2.0 renderer and running HTTP endpoint. The SVG bodies matched byte for byte, including source-selected masks, high error correction, and a numeric input that exactly fills a smaller QR version but causes Campfire to choose the next version. The actual routes returned matching content type, one-year public cache control, weak ETag, and Vary header; all ten conditional requests returned matching empty 304 responses. The parity probe does not check malformed IDs or every QR capacity transition.

`python bench/paired_qr.py --benchmark --clients 32 --seconds 3 --campfire-workers 22 --report bench/results/qr-reads.json` then measured a warm **31,362-byte** invitation-shaped SVG in both server orders after Rustfire added source-style gzip responses. One Rustfire release process and 22 Campfire Puma workers shared a 32-logical-CPU host with the load generator. Each of 32 keep-alive clients warmed its connection twice. Every measured 200 response was gzip decoded by the Go transport and matched the same SVG SHA-256 and ETag; every conditional 304 had an empty body and the same ETag. All trials had zero errors.

| Rustfire first | Response | Rustfire reads/s / p95 | Campfire reads/s / p95 |
| :---: | :---: | ---: | ---: |
| Yes | 200 | 13,890 / 4.38 ms | 2,277 / 28.17 ms |
| Yes | 304 | 32,336 / 2.16 ms | 2,818 / 18.75 ms |
| No | 200 | 12,076 / 4.96 ms | 2,358 / 31.32 ms |
| No | 304 | 31,776 / 2.19 ms | 2,905 / 18.29 ms |

Rustfire served **5.12–6.10×** as many full QR reads and **10.94–11.48×** as many conditional reads in these three-second trials. The [raw report](results/qr-reads.json) contains counts and latencies. This is a single QR value, same-host, short read test; it does not measure multi-value QR traffic, page rendering, connection setup, resource use, maximum scale, or a whole-app advantage at full parity.

## Paired response version, Vary, and gzip behavior

`python bench/paired_security_headers.py` compared 13 response-header fields on 15 sampled routes against pinned Campfire, covering successful HTML and JSON, redirects, 204, 403, 404, 406, PWA resources, health, and a static asset. The values matched, including Campfire's controller-only `X-Version` and `X-Rev`, `Vary: Accept-Encoding` on entity responses, `Vary: Accept,Accept-Encoding` on the sampled negotiated formats, and no Vary on the sampled empty responses. Explicit `APP_VERSION` and `GIT_REVISION` settings and the revision fallback also matched. Seven additional gzip requests made the same compression decisions; their response bodies decompressed successfully, and the static asset bytes matched. The probe does not compare every route, compressed byte streams, or performance of general gzip traffic.

## Paired message attachment MIME detection

`python bench/paired_attachment_mime.py` passed against pinned Campfire `91d294f`. The browser message POST path stored JPEG and PNG bytes labeled `text/plain` as `image/jpeg` and `image/png`, BMP bytes labeled `image/jpeg` as `image/bmp`, and QuickTime bytes labeled `text/plain` as `video/quicktime` in both apps. Each original saved file had the same SHA-256 as the submitted bytes, and each normalized Turbo message response had the same parsed presentation. A text file labeled JPEG caused HTTP 500 in both apps after the message and original attachment were persisted; the saved metadata and bytes matched. This probes four valid signatures and one malformed image. It does not compare error-page bodies, all media formats, or upload performance.

The `--extended-images` option passed with generated GIF, WebP, TIFF, animated GIF, and EXIF-rotated JPEG uploads labeled `text/plain`. Both apps detected the same MIME types, saved byte-identical originals, emitted matching parsed Turbo presentations, and served inline previews with matching MIME types and SHA-256 hashes. Rustfire now treats TIFF as an inline previewable image, matching Campfire. The EXIF-rotated 80×40 JPEG initially exposed a presentation mismatch: Campfire displayed it as 40×80, while Rustfire used the unrotated frame dimensions. Rustfire's JPEG metadata reader now applies orientation values 5–8 to the display dimensions, and the paired probe passes. These generated fixtures do not cover all image formats or metadata variants.

The same 80×40 EXIF-oriented image, converted to WebP and PNG while retaining orientation metadata, exposed the equivalent mismatch in Rustfire's libvips metadata path. Campfire displayed each as 40×80, while Rustfire initially used 80×40. Rustfire now applies the orientation reported by `vipsheader` when deriving display dimensions. The extended paired probe passes for both formats, including parsed message presentation and served preview hashes. This does not cover every orientation value or format that can carry orientation metadata.

`python bench/paired_inline_upload.py` now passes against pinned Campfire for newly direct-uploaded text and JPEG files embedded in rich messages. Both apps rendered the same parsed presentation after normalizing independently signed JPEG preview URLs; the served JPEG previews had the same SHA-256. Original file bytes, blob-to-rich-text associations, and searchable text matched. One JPEG was referenced by two messages; both apps retained the surviving reference after the first removal, and Rustfire deleted the original only after the final removal. A two-image gallery matched the source's container, 800×600 signed variation dimensions, and preview hashes; edits to one and then zero images matched presentation, search text, and saved associations. A separate edit added and removed a new inline text file with matching redirects and presentation. This covers selected inline-file lifecycles, not other inline media, larger galleries, browser editor interactions, upload performance, or all ActionText output.

The same paired probe passed with `--large-mib 129` after Rustfire moved composer multipart attachments to staged files and disabled the 128 MiB extractor limit on the message collection route. Before the change, this **129 MiB plus one byte** binary file returned 400 in Rustfire and 200 in pinned Campfire. Afterward, both returned 200, saved the complete file with the same SHA-256 and `application/octet-stream` type, and produced matching parsed Turbo message presentations. Rustfire left no staged composer file after the valid and malformed-image posts. This verifies one upload above the former limit; it does not measure concurrent-upload capacity, memory, throughput, or larger files.

## Paired avatar upload and rendering parity

`python bench/paired_avatar.py` passed against pinned Campfire `91d294f`. The probe uploaded the source's `moon.jpg` and `pixel.bmp` through the profile form, then compared the served avatar type and parsed initials SVG or exact WebP bytes. Campfire and Rustfire both accepted the BMP but rendered initials; after reproducing the source image-processing sharpening convolution, their JPEG WebPs were byte-identical. JPEG and PNG bot avatars uploaded with a misleading `text/plain` multipart type were both detected from file bytes, saved with matching content types, and served as byte-identical WebPs. All sampled avatar reads had weak ETags and matching cache-control values; conditional reads returned 304 with empty bodies, and each new upload invalidated the prior validator. This checks selected formats, upload paths, and cache transitions. It does not establish all MIME-detection behavior, other image formats, or raw ETag equality.

The same fixture then measured a warm **3,364-byte** JPEG bot avatar using the checked Go keep-alive client. The two apps served identical WebP bytes. A full 200 read was checked against that body hash, content type, and each app's ETag; a conditional read was checked for a 304, empty body, and unchanged ETag. Each client completed two warmups before the five-second interval. The apps ran serially on one 32-logical-CPU host in both server orders, with 22 Campfire Puma workers and isolated Redis versus one Rustfire release process. All measured reads passed with zero errors.

| Clients | Response | Rustfire first | Rustfire reads/s / p95 | Campfire reads/s / p95 |
| ---: | :---: | :---: | ---: | ---: |
| 32 | 200 | Yes | 25,990 / 2.59 ms | 3,228 / 27.43 ms |
| 32 | 200 | No | 22,433 / 2.95 ms | 2,874 / 30.65 ms |
| 32 | 304 | Yes | 54,308 / 1.44 ms | 5,035 / 15.04 ms |
| 32 | 304 | No | 50,671 / 1.53 ms | 7,783 / 7.67 ms |
| 128 | 200 | Yes | 42,223 / 6.82 ms | 3,268 / 73.41 ms |
| 128 | 200 | No | 39,659 / 7.14 ms | 4,398 / 57.90 ms |
| 128 | 304 | Yes | 77,583 / 4.17 ms | 7,292 / 31.44 ms |
| 128 | 304 | No | 95,571 / 3.50 ms | 7,162 / 29.93 ms |

Rustfire served **7.81–12.92×** as many full avatar reads per second and **6.51–13.34×** as many conditional reads, with lower p95 in every sampled trial. The [raw report](results/avatar-reads.json) preserves counts and latencies. These are short, single-avatar, same-host, warm-cache measurements; they do not measure variant generation, uploads, CPU or memory use, maximum scale, or a whole-app advantage at full parity.

## Rotated-key User mention parity

`python bench/paired_mention.py` passed eight matched cases against pinned Campfire `91d294f`. Current and Rails 7 User SGIDs with invalid signatures still rendered the same parsed mention markup and searchable `@name` text in both apps. Campfire's `Message#mentionees` and Rustfire's saved recipient rows excluded those two invalid-signature mentions, while valid, duplicate, banned-member, and nonmember cases retained their existing behavior. `python bench/paired_mention_webhook.py` also passed after adding an invalid-signature bot mention: both apps showed it in the complete normalized bot message-list JSON, minted a fresh valid SGID inside its rendered mention, and sent no webhook for it. The source's broader ActionText handling and other notification paths remain unverified.

Machine: local 32-logical-CPU Linux host. Rustfire was built with `cargo build --release`, using one server process and SQLite WAL on local storage. The Go load generator ran on the same host. Each trial used 32 concurrent keep-alive HTTP clients for 10 seconds, against a room initially containing 323,639 messages. Reads returned the latest 40 messages. Writes posted small text messages and updated the SQLite FTS5 index.

| Workload | Throughput | p50 | p95 | p99 | Errors |
|---|---:|---:|---:|---:|---:|
| `GET /rooms/1/messages` | 10,738 successful requests/s | 2.91 ms | 4.14 ms | 4.88 ms | 0 |
| `POST /rooms/1/messages` | 9,759 successful requests/s | 0.18 ms | 18.34 ms | 53.58 ms | 0 |

The 10-second write trial inserted 98,244 messages. The database contained 421,883 messages afterward. This result predates later profile, avatar, account, and API additions and should be rerun after parity work. These are **Rustfire-only** measurements, not a Campfire comparison or a maximum scale claim. The load generator shared CPU and storage with the server, and Rustfire still lacks some work performed by Campfire when posting messages.

The older HTTP results above used a different fixture and should not be compared with Campfire. A later matched-fixture trial appears below.

## Paired rich-mention bot API reads

The pinned Campfire build and Rustfire each received 40 signed-mention messages through the same browser POST route. Their bot message-list JSON matched after normalizing generated timestamps and URL origins, including ActionText mention HTML. Both responses were **53,003 bytes**. A Go client then checked every timed GET against that app's warmed response SHA-256. Campfire used its packaged 22 Puma workers; Rustfire used one release process. Both apps and the client shared the 32-logical-CPU host. Each trial lasted 10 seconds, with no response errors.

| Clients | Order | Campfire reads/s | Rustfire reads/s | Campfire p95 | Rustfire p95 |
|---:|---|---:|---:|---:|---:|
| 32 | Rustfire first | 1,267 | 2,788 | 55.56 ms | 16.34 ms |
| 32 | Campfire first | 1,209 | 2,836 | 51.40 ms | 16.23 ms |
| 128 | Rustfire first | 1,226 | 2,767 | 259.43 ms | 73.64 ms |
| 128 | Campfire first | 997 | 2,804 | 265.00 ms | 72.19 ms |
| 256 | Rustfire first | 1,060 | 2,855 | 363.12 ms | 152.69 ms |
| 256 | Campfire first | 921 | 2,792 | 419.02 ms | 154.76 ms |

Rustfire delivered about **2.2–3.0×** as many checked reads per second in these trials. With an illustrative 250 ms p95 objective, Rustfire met it at all three tested client counts; Campfire met it at 32 clients. This establishes an advantage for this matched read path at the sampled concurrency levels. It does not establish a maximum user count, long-running capacity, or a speed advantage at complete application parity. The client, Redis, SQLite, and servers shared one host; repeat on separate hosts for deployment-scale claims. Reproduce with `python bench/paired_mention_reads.py --clients 32 128 256 --seconds 10 --campfire-workers 22 --slo-ms 250` and `--campfire-first`.

The bot JSON serializer now preserves field order. In a fresh 10-second sweep, the complete 40-message rich-mention response matched Campfire **byte for byte** after replacing only the local server origin and each independently generated `created_at` timestamp. Both raw bodies were 53,003 bytes. Every timed read matched its own warmed SHA-256, with zero errors:

| Clients | Order | Campfire reads/s | Rustfire reads/s | Campfire p95 | Rustfire p95 |
|---:|---|---:|---:|---:|---:|
| 32 | Rustfire first | 1,525 | 3,331 | 50.4 ms | 13.8 ms |
| 32 | Campfire first | 1,110 | 3,145 | 64.5 ms | 14.6 ms |
| 128 | Rustfire first | 1,405 | 3,109 | 149.9 ms | 65.9 ms |
| 128 | Campfire first | 1,374 | 3,065 | 138.9 ms | 66.7 ms |
| 256 | Rustfire first | 1,152 | 3,132 | 336.3 ms | 137.6 ms |
| 256 | Campfire first | 1,153 | 3,138 | 357.7 ms | 138.8 ms |

Rustfire served **2.18–2.83×** as many checked reads/s in these two orders. With the same illustrative 250 ms p95 objective, Rustfire met it through the largest sampled count of 256 clients; Campfire met it through 128 in these runs. The difference from the earlier Campfire 128-client result shows run variation. These 10-second, same-host trials do not establish maximum supported clients, deployment capacity, or whole-app parity. The [Rustfire-first report](results/rich-mention-reads-wire-rust-first.json) and [Campfire-first report](results/rich-mention-reads-wire-camp-first.json) retain the counts and latencies.

After Rustfire added the five Rails default security headers, the same paired probe ran for five seconds per client count in both trial orders. Every timed response matched its own warmed SHA-256, both response bodies were 53,003 bytes, all 40 normalized messages matched, and neither app returned an error.

| Clients | Order | Campfire reads/s | Rustfire reads/s | Campfire p95 | Rustfire p95 |
|---:|---|---:|---:|---:|---:|
| 32 | Rustfire first | 1,445 | 3,315 | 44.4 ms | 13.0 ms |
| 32 | Campfire first | 1,424 | 2,802 | 45.4 ms | 16.6 ms |
| 128 | Rustfire first | 1,336 | 3,025 | 221.5 ms | 66.7 ms |
| 128 | Campfire first | 1,358 | 2,692 | 143.5 ms | 74.8 ms |
| 256 | Rustfire first | 1,079 | 3,069 | 345.4 ms | 140.9 ms |
| 256 | Campfire first | 1,018 | 2,664 | 375.0 ms | 166.1 ms |

Rustfire served **1.97–2.84×** as many checked reads per second in these shorter trials. The five-second duration and same-host setup limit comparison with the earlier ten-second rows; they verify that this added response behavior did not erase the sampled read advantage. Reproduce with `python bench/paired_mention_reads.py --clients 32 128 256 --seconds 5 --campfire-workers 22 --slo-ms 250` and `--campfire-first`.

## Paired bot webhook bursts

`bench/paired_webhook_burst.py` posted one rich-text message in a direct room with 80 or 320 webhook bots in each app. A local receiver held each request for 100 ms before responding 204. The bot IDs, tokens, memberships, URLs, message, and every outgoing JSON payload matched. Each elapsed time starts before the browser POST and ends when the receiver has responded to every bot; all deliveries completed. Campfire used one Puma process, eight registered Resque workers, isolated Redis with a 250 ms polling interval, and SQLite. Rustfire used one release process, SQLite, and 64 concurrent webhook delivery slots. The apps and receiver ran serially on the same 32-logical-CPU host.

| Bots | Trial order | Campfire | Rustfire | Rustfire elapsed speedup | Deliveries |
|---:|---|---:|---:|---:|---:|
| 80 | Rustfire first | 4.005 s | 1.238 s | 3.24× | 80 / 80 |
| 80 | Campfire first | 4.173 s | 1.997 s | 2.09× | 80 / 80 |
| 80 | Campfire first, repeat | 3.906 s | 1.244 s | 3.14× | 80 / 80 |
| 320 | Rustfire first | 15.731 s | 2.027 s | 7.76× | 320 / 320 |
| 320 | Campfire first | 15.673 s | 2.002 s | 7.83× | 320 / 320 |

Rustfire's completion time grew less from 80 to 320 bots under these worker settings. The first 80-bot reverse-order sample was slower for Rustfire than its repeat, so retain the individual trial times when comparing results. This does not identify either app's maximum supported bot count or establish sustained delivery or whole-app scale. Reproduce with `python bench/paired_webhook_burst.py --bots 80 --campfire-workers 8` and `--bots 320 --campfire-workers 8`, repeating each with `--campfire-first`.

`bench/paired_webhook_failures.py` separately checked two failure paths. A receiver that waited beyond seven seconds produced the same timeout bot reply and normalized message list in both apps. A refused local connection produced no bot reply in either app; Campfire retained one failed Resque job and Rustfire retained one failed SQLite job. Manually requeuing those failed jobs delivered the same outgoing JSON in both apps, preserved each historical failure, and set `retried_at`. This behavioral check was not timed and does not cover other network errors or repeated retries under load.

## Thirty-second paired webhook load

`bench/paired_webhook_sustained.py` offered 360 sequential message posts over 30 seconds at 12 posts/s, each in the same direct room with eight bots. Every post produced eight outgoing webhooks, and a local receiver held each request for 100 ms before returning 204. Both apps used the same bot IDs, tokens, membership, and message bodies on fresh disposable SQLite databases. Campfire used one Puma process, 22 registered Resque workers, and isolated Redis with 250 ms polling; Rustfire used one release process and 64 webhook slots. The runs were serial on the same 32-logical-CPU host, with one warmup post before timing. Both app orders completed **2,880/2,880** matching JSON payloads, with no duplicates, queued jobs, or failed jobs. The p95 offered-post scheduling lag was under 0.5 ms in both apps.

| Trial order | Campfire deliveries during posting | Rustfire deliveries during posting | Campfire delivery p95 | Rustfire delivery p95 | Campfire total | Rustfire total |
|---|---:|---:|---:|---:|---:|---:|
| Campfire first | 1,100 | 2,860 | 41,398.89 ms | 205.01 ms | 73.065 s | 30.381 s |
| Rustfire first | 1,184 | 2,863 | 37,930.02 ms | 206.62 ms | 69.689 s | 30.915 s |

The offered load required about 96 webhook deliveries/s. During posting, Campfire completed 36.73–39.55/s and drained its remaining 1,696–1,780 deliveries in another 39.75–43.12 seconds; Rustfire completed 95.59–95.70/s and drained its remaining 17–20 in under one second. End-to-end completion was 2.25–2.40× faster for Rustfire on this specific workload. Reproduce with the two commands in `bench/README.md`. This is one 30-second rate point on a shared client/server host, with 204 responses and no browser sockets, push subscriptions, or bot replies. It does not establish maximum throughput, hours-long stability, or whole-app scale at full parity.

## Turbo route format parity

`python bench/paired_turbo_formats.py` passed on disposable instances of the pinned source and Rustfire. The room refresh and account user pagination routes now agree on HTTP status and media type for absent, HTML, JSON, wildcard, and Turbo Stream Accept headers, and for explicit `.turbo_stream` paths. Both return the same JSON 406 body when the refresh route is requested as JSON, and treat a nonnumeric `since` value as zero. Empty room refresh responses contain a newline as in the source. Rustfire's browser applies Turbo refresh responses for edits and uses a separate JSON `refresh_state` route for backlog pagination. This is a route behavior check, not a timing result.

In a local Chromium browser with one existing message, 105 messages and an edit were written directly to the disposable SQLite database without broadcasting WebSocket events. Dispatching an `online` event caused two checked `refresh_state` pages and one Turbo refresh. The browser then showed all 106 messages exactly once, including the first and last backlog entries, and displayed the edited original. This verifies one reconnect recovery path; it does not measure latency or replace cross-browser testing.

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

The paired probe now sends the same boost text to both apps and rejects any difference in parsed boost-stream tags, attribute names or values, or non-whitespace text. The sampled boost event has **13 elements in each app** and passes that check. With 200 signed sockets and 10 boost posts, each app delivered all **2,000** events without misses or unexpected frames. Rustfire measured **30.92 ms p95** and averaged **1,495 bytes** per event; one-worker Campfire measured **93.44 ms p95** and **1,565 bytes**. A separate 200-socket, 10-message run also delivered all 2,000 events per app, at **33.67 / 152.08 ms p95** and **8,489 / 9,589 bytes** for Rustfire / Campfire. After the boost ID sequence and form changes, the 200-socket, 10-boost check still delivered **2,000/2,000** events in each app with matching sampled boost identity and zero misses or unexpected frames. It measured **30.54 / 89.20 ms p95** and **1,495 / 1,565 bytes** for Rustfire / one-worker Campfire. These short local bursts strengthen boost-stream parity evidence but do not establish equivalent full-app work, sustained capacity, or a maximum scale advantage.

## Absolute message copy links

Rustfire now emits an absolute copy-link URL in message HTML using the configured public origin or the request Host. The live stream, room page, message history, and refresh path use the same rendering rule; posts without a request use the configured public origin or bind-address fallback. The WebSocket and smoke tests assert the absolute URL for a randomly assigned local port.

The paired plain-message probe now supplies one canonical public origin to Rustfire and compares parsed static attribute values and non-whitespace text, as well as the existing element and attribute-name structure. One sampled stream had **86 elements** and equal static attributes and text. Only generated creation/update timestamps and the two time-element values differed; the apps posted serially. With 200 sockets and 10 message posts, both apps delivered all **2,000** expected events with no misses or unexpected frames. The first run measured **31.08 / 152.80 ms p95** for Rustfire / one-worker Campfire; a repeat after avoiding an unnecessary bind-address lookup measured **26.50 / 180.87 ms p95**. Average event sizes were **8,505 / 9,589 bytes** in both runs. The remaining byte difference includes serialization whitespace, and other message types and side effects are not proven equivalent. These short bursts do not establish full-app speed or maximum scale.

## Text attachment streams and signed blobs

Rustfire now renders non-preview attachments with Campfire's file row and Download/Share controls. Its signed blob URL uses Campfire's Active Storage verifier when the original secret key is configured; otherwise it uses a persistent local key. The signed route serves file bytes to anyone holding a valid token, and direct `/attachments/ID` downloads retain room access checks. Smoke tests verify attachment disposition for text and image files, anonymous access through a valid signed URL, and rejection of a tampered token. In a browser, the Share controls hid when the Share API was absent. With a stubbed Share API, the row action fetched the file and passed a `text/plain` File to the share handler.

The paired text-attachment probe posts the same small file to both apps. One sampled append had **96 elements** in each stream; ordered elements, attribute names, static values, and non-whitespace text matched after excluding generated timestamps. In two 200-socket, 10-attachment bursts, both apps delivered all **2,000** events per run with no misses or unexpected frames. Rustfire measured **40.68 and 37.77 ms p95**; one-worker Campfire measured **232.02 and 195.93 ms p95**. Average event sizes were **10,459 / 11,571 bytes** for Rustfire / Campfire. The apps still differ in file storage and processing. Later paired route checks added Campfire-style signed blob redirects and disk downloads to Rustfire; those changes were not part of this earlier burst. Image/video variants and larger files were outside this trial. This does not establish full-app speed or maximum scale.

## Image attachment streams and processed previews

Rustfire now reads uploaded image dimensions, creates and sharpens the thumbnail before broadcasting, and renders Campfire's measured preview container, image dimensions, and signed Active Storage representation URL. The paired image probe uploaded the same generated 2400×1600 PNG to each app. Both streams had the same ordered elements, attribute names, static values, and text after excluding generated timestamps. Both signed representation URLs returned a **byte-identical 331,484-byte 1200×800 PNG**. A separate 1×1 PNG sample produced identical 281-byte previews. The paired probe now fails if the PNG hashes differ.

In two `--operation images --image-size 2400x1600 --sockets 200 --messages 10 --campfire-workers 1` bursts after adding Campfire's sharpening step, both apps delivered **2,000 / 2,000** expected events without misses or unexpected frames. Rustfire measured **139.85 and 117.91 ms p95**; one-worker Campfire measured **311.64 and 269.05 ms p95**. The corresponding median latencies were **131.06 / 94.00 ms** and **111.87 / 90.19 ms** for Rustfire / Campfire: Rustfire had lower p95 but higher median latency. Average event bodies were **10,249 / 11,357 bytes**. This single-host, one-room, one-user burst does not establish sustained throughput, maximum capacity, or whole-app performance at full feature parity. Rustfire still differs in attachment storage and response behavior, and video processing remains outside this trial.

## Video attachment streams and posters

Rustfire now probes uploaded video dimensions and display aspect ratio, extracts the same JPEG preview frame as Campfire, and generates the resized, sharpened WebP poster before broadcasting. Its video stream includes Campfire's measured container and signed poster representation URL. The paired probe verified matching ordered elements, attribute names, static values, and text after excluding generated timestamps. Both the 16×16 default clip and a separate 2400×1600 clip produced byte-identical WebP posters from the two apps: **280 bytes** and **2,100 bytes**, respectively. Separate 16×16 clips with 2:1 and 3:2 sample aspect ratios also matched stream markup and poster hashes. The probe fails on a poster hash difference.

In two `--operation videos --sockets 200 --messages 10 --campfire-workers 1` bursts using the 16×16 clip, both apps delivered **2,000 / 2,000** expected events without misses or unexpected frames. Rustfire measured **175.04 and 173.16 ms p95**; one-worker Campfire measured **320.49 and 355.23 ms p95**. The corresponding medians were **162.63 / 131.66 ms** and **156.75 / 128.85 ms** for Rustfire / Campfire. Average event bodies were **9,962 / 11,068 bytes**. Rustfire had lower p95 and higher median latency in both runs. Larger video bursts, other codecs and aspect ratios, and sustained capacity remain unmeasured; storage and response behavior still differ.

## Message edit frames

Rustfire now returns Campfire's editable Turbo frame for plain messages and a view with Delete and Close controls for attachments. The room's editor link loads the frame in place; Save uses the nested message PATCH route, Close restores the message, and Delete removes it. The paired probe fetched edit responses for plain messages, text files, images, and videos. All four frame samples matched Campfire's ordered elements, attribute names, static values, and text after normalizing session CSRF tokens and the apps' different listen origins. A browser check opened the editor, saved text, closed a reopened editor without changes, and deleted the message. The smoke suite also exercises Rails-style `_method=patch` and `_method=delete` form posts. The surrounding document layout and other browser interactions still need parity work.

## Bot administration behavior and token storage

The 2026-09-27 rerun extended `bench/paired_bot_admin.py` with four webhook URL update states. Campfire and Rustfire both returned 302 to `/account/bots` and saved the same webhook row for an explicit blank URL (removed), a restored URL, a misspelled `user[webook_url]` field (removed), and an entirely omitted URL field (removed after another restore). This caught a Rustfire gap where an omitted field retained the old webhook. The existing bot creation, edit-page markup, avatar bytes, key rotations, API access, deletion, and legacy token migration checks also passed in that rerun. This is one bot's administration path, not a full bot or webhook compatibility claim.

`bench/paired_bot_admin.py` now runs the same bot creation, editing, key rotation, and deletion workflow against the pinned Campfire and Rustfire builds on disposable databases. Both returned HTTP 302 to `/account/bots` for each mutation, accepted the same three new-form fields, saved a 12-character alphanumeric token, the byte-identical original PNG avatar, webhook, and open-room membership, and allowed the same bot message API path. After rotation, the old key redirected to sign-in and the new key worked. Deactivation retained the token on the inactive row and removed its open-room membership in both apps. Rustfire also migrated a legacy prefixed token in an older Rustfire database to Campfire's token-only storage without changing the public key. This checks the listed workflow and persisted fields; other bot states and behavior remain to be compared.

The bot list now includes Campfire's room-specific text and file-upload `curl` commands. The paired workflow normalizes the different listen origin and random bot key, then requires both displayed command values to match. In a browser, both copy buttons wrote exactly the displayed command text and the screen loaded without console errors. A later run compared the complete parsed head and body of new, list, and edit pages, including edit after replacing an avatar and edit with no avatar. All five sampled documents matched after normalizing generated signed URLs, bot keys, CSRF tokens, origins, and product names. The matched token counts were 146 head tokens on each page and 193, 180, or 213 body tokens by page. The three newly included SVG assets matched Campfire by SHA-256. Both edit previews redirected once and returned the same 68-byte original PNG; replacing the avatar issued a distinct signed blob URL and preserved the same preview bytes. A no-avatar edit used Campfire's default bot SVG. `cargo test --quiet` passed 47 tests, and `python bench/paired_direct_upload.py --large-mib 1` passed after preserving monotonic signed blob IDs across avatar replacement. Raw HTML bytes, imported-bot avatar previews, additional account states, and wider browser behavior remain to be compared.

The bot mutation workflow now checks both PATCH and PUT on the same bot-owned message. PUT returned the same normalized JSON as pinned Campfire after updating the body; both apps rejected PUT against another user's message and redirected an invalid bot key the same way. The existing check of 24 writes from eight concurrent clients still passed. This establishes PUT behavior on the sampled bot route, not all bot API payloads or sustained write throughput.

`python bench/paired_bot_pagination_edges.py --formats` passed 16 bot cursor queries and 11 format/Accept cases against the pinned Campfire checkout with 90 messages. Both apps returned the same normalized JSON and XML bodies, media types, total-count values, and next links. Explicit `.html` returned a 40-message fragment with matching parsed tags, attributes, and text after normalizing generated CSRF tokens. Rustfire now ignores blank cursors, returns Campfire's JSON 404 for invalid or absent cursor IDs, and gives `before` priority for page contents when both cursors appear. In that combined case, Campfire still uses the present `after` parameter to choose the next-link direction; Rustfire now does the same. This is sampled route and response parity, not a bot API throughput, raw HTML byte, or browser form-workflow claim.

The expanded probe passes **49/49 cursor queries and 11/11 format cases**. It exposed six nested-parameter mismatches and later numeric cursor and array-record mismatches. Campfire accepts the numeric prefix of scalar IDs such as `60x` and `20.0`; a nested array of valid IDs reaches a packaged 500, while one containing an absent ID returns 404. A scalar followed by a nested value returns 400, and a nested `after` still selects the next-link direction when a scalar `before` selects page contents. Rustfire now matches the observed status, full JSON body, content type, count, and link behavior. The 52 Rust unit tests pass. Other parameter shapes and bot API operations remain outside this query probe.

`python bench/paired_bot_write_formats.py` passed **72 paired bot write-format cases** against the pinned source. It checks message create, PATCH and PUT edits, deletion, boost creation, and boost deletion across six URL suffix states, four query-format states, and three unsuffixed Accept headers. Both apps now agree on status, media type, redirect path, error-body SHA-256 or successful JSON field names, and the saved effect of every request. In particular, Campfire saves an edit before returning 406 for an unsupported representation, and saves a boost before returning 500 for HTML, Turbo Stream, or XML; Rustfire does the same in these cases. Path suffixes determine bot write representations, while `?format=` is ignored on these routes. The earlier Rustfire build had 29 differences in the initial suffix/Accept probe, including unhandled suffixes and missing JSON charsets. This is response and persistence parity for sampled bodies, not proof of every JSON value, attachment, authorization, webhook, or throughput behavior.

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

A later serializer change aligned JSON field order. With 10,000 seeded messages, the latest 40-message responses were both 20,385 bytes and matched byte for byte after replacing only their local origins; the before/after page responses matched under the same normalization. A separate 1,000-message fixture with a text-file attachment and scrambled timestamps also passed the raw-byte check, with both latest-page bodies at 19,879 bytes. The earlier unequal response sizes in the table above describe that historical build. Three serial iterations of the 10,000-message case measured Rustfire median/p95 at 0.577/0.636 ms and Campfire at 22.911/24.300 ms; that small sample is a parity check, not a throughput or capacity estimate.

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

The account settings route was then moved to the source-style document shell. On a matched **1,100-user** fixture, `bench/paired_account_users.py` found identical parsed administrator navigation (24 tokens) and settings panel (40,862 tokens) after normalizing session CSRF values, signed avatars, logo versions, and invite URLs. The member navigation (10 tokens) and panel (16,557 tokens) also matched. The administrator panel stayed matched after a browser-style room-creation restriction update, after uploading a PNG logo (40,872 tokens), and after submitting the versioned logo-delete form. The same probe still passed account-user paging, role changes, and deactivation.

With 22 Campfire Puma workers, one release Rustfire process, two warmup requests per connection, and separate two-second local trials, the updated `/account/edit` read measured:

| Clients | Rustfire requests/s / p95 | Campfire requests/s / p95 |
| ---: | ---: | ---: |
| 1 | 172.7 / 7.76 ms | 4.3 / 305.94 ms |
| 8 | 814.9 / 12.28 ms | 23.9 / 639.49 ms |
| 32 | 1,079.2 / 42.13 ms | 39.8 / 1,293.28 ms |

All measured responses passed the harness's status and page-control checks with zero HTTP errors. Rustfire sent 1,935,926 bytes per response and Campfire 2,236,856 bytes; the parsed panel matches under the stated normalizations, but the surrounding document and raw byte counts differ. The page-2 Turbo Stream at 32 clients measured 1,405.7 versus 103.6 requests/s and 28.08 versus 543.02 ms p95. These are short reads on the server host, not a sustained capacity limit or a whole-app speed claim.

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

The profile page was then moved to Rustfire's source-style document shell. On a matched fixture with one shared and one direct room, `bench/paired_profile.py --requests 320` compared the parsed navigation and complete profile panel: **23 nav tokens and 380 panel tokens matched** after normalizing signed avatar, transfer, QR, and CSRF values and the product name. Posting the same PNG avatar to both apps produced the source-style delete control and **390 matching panel tokens**. Both source-shaped avatar-delete forms removed the saved file and returned a panel with **381 matching tokens** after the profile fields changed. The probe also passed two-way device transfer, profile writes, and notification changes. A new warm read run with the avatar attached measured:

| Clients | Rustfire requests/s / p95 | Campfire requests/s / p95 |
| ---: | ---: | ---: |
| 1 | 9,099 / 0.17 ms | 174 / 11.92 ms |
| 8 | 13,365 / 1.27 ms | 373 / 38.05 ms |
| 32 | 14,749 / 4.40 ms | 616 / 129.67 ms |

The source sent 37,211 bytes per page and Rustfire 19,725 bytes. The nav and panel matched under the stated normalization, while the surrounding document, JavaScript imports, and response size still differed. Both servers and the client ran on one host; four Puma workers served Campfire and one release process served Rustfire. These short serial trials do not establish sustained capacity or whole-app superiority.

## Original room invitation and mobile composer

Rustfire now renders the source's welcome invitation in the original room while it has at most 40 messages. The card contains the account logo, translated welcome text, current join URL, QR link, copy value, share control, and administrator regenerate form. `bench/paired_room_invitation.py` verified both apps displayed the card with 0, 1, and 40 messages, removed it at 41, and omitted it from a second room. It decoded each QR path to its displayed join URL, matched the copy value, and checked that the SVG response has Campfire's one-year public cache header. A browser opened Rustfire's QR dialog and posted a message through the mobile composer; the welcome card remained visible after that post.

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

A proxy-route repeat also passed on the uploaded text blob. Both apps served the complete bytes with HTTP 200, the first six bytes with HTTP 206 and the same Content-Range, and identical content type, Content-Disposition, and cache-control values. Each app returned empty 304 responses for its own ETag and Last-Modified validators, served the blob when the URL's filename differed, and returned 404 for an invalid signed ID. The weak ETag in each app matched Rails' first 32 SHA-256 hex characters of that app's URL path; values differ because the signed URLs are installation-specific. Both apps also redirected the legacy blob URL and accepted noncanonical redirect and disk URL filenames, delivering the original bytes.

The probe now also uploads HTML, PNG, SVG, accented Latin, Cyrillic, punctuation, reserved-character, separator, and bidirectional-override filenames directly. Both apps serve HTML and SVG as `application/octet-stream` attachments even when inline is requested, while PNG defaults to inline and switches to attachment when requested. Complete bytes, content type, Content-Disposition, cache headers, Last-Modified, and sampled security headers matched. Rails' ASCII fallback transliterates accented Latin characters and percent-escapes non-Latin question-mark replacements; Rustfire now matches those headers while preserving the UTF-8 `filename*` parameter. Both apps retain the submitted unsafe filename in storage but return its Rails-sanitized form in metadata and download responses. Disk downloads also matched the serving choices: `X-Content-Type-Options: nosniff` is present there and absent from proxy responses, with no Content-Security-Policy header on either. The main message-attachment fixture separately matched text and JPEG original blob redirect, disk, and proxy behavior. These are sampled public download paths; other MIME types, filename cases, and malformed signatures remain open.

The same paired probe now verifies an `application/octet-stream` upload of **25 MiB plus one byte** through metadata creation, authenticated disk write, full proxy reads, redirect and disk download, and stored bytes. Rustfire's former 25 MiB metadata cap rejected this file; pinned Campfire accepted it. Both apps also returned the same metadata, signed upload token fields, and direct-upload headers for a 264-character filename, a long MIME type, a negative declared byte count, an empty filename, an empty MIME type, and an omitted MIME type. Rustfire now preserves `null` for the omitted MIME type and migrates older direct-upload tables with a marker to distinguish it from an explicitly empty string. The omitted-MIME file uploads and proxies with `application/octet-stream` in both apps. On the final redirected disk request, both now return Campfire's HTTP 500 response with matching media type and complete error-page bytes. The tightened probe passed with a 1 MiB plus one byte upload; the earlier 25 MiB and 129 MiB runs did not compare this error page. These boundary checks are compatibility evidence, not a throughput or maximum-upload-size measurement.

After Rustfire changed direct-upload PUT to hash and write each body chunk to a temporary file, `python bench/paired_direct_upload.py --large-mib 129` passed the same paired route checks with a **129 MiB plus one byte** file. The former Rustfire `Bytes` extractor rejected requests above 128 MiB. The new handler does not use that extractor and removes temporary files when validation fails or a request is cancelled. This verifies one larger file on both apps; it does not measure throughput, peak process memory, concurrent uploads, or a maximum accepted size.

## Paired account custom CSS workflow

`bench/paired_custom_styles.py` ran the pinned Campfire and Rustfire release builds against disposable accounts. Both editor pages exposed the source form action path, POST method with PATCH override, `account[custom_styles]` textarea with 16 rows and the same placeholder and input attributes, seven translation entries, warning text, back link, and byte-identical warning SVG. PATCH and PUT each returned 302 to the editor and saved the submitted CSS on the account. The account update timestamp changed. Both apps embedded the saved CSS in a `data-turbo-track="reload"` style tag on authenticated and sign-in pages. After revoking the administrator role, GET and PATCH returned 403 and the saved CSS remained unchanged. The normalized probe result matched across both apps. Their full HTML and visual layout still differ, so this is a workflow parity check rather than a one-for-one page or performance result.

A route-method repeat also matched plain POST and DELETE-override POST: both returned 404 and left the account row unchanged. Browser-form POST with PATCH and PUT overrides returned 302 and saved the CSS in both apps, in addition to direct PATCH and PUT. Rustfire previously saved CSS on plain POST. This expands method and state parity for the tested administrator, without measuring throughput.

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

A paired Chromium check of the installed-app badge API used disposable signed-in room pages and instrumented `navigator.setAppBadge` and `navigator.clearAppBadge`. Adding and clearing unread markers in the sidebar, then replacing the sidebar badge container, produced the same **1, 2, 1, 0, 0** badge state sequence in both apps. Rustfire observes unread-marker changes and sidebar replacements; Campfire's Stimulus badge controller received its room-list events. This checks the badge count behavior with controlled DOM changes, not real push notifications, browser installation, or all sidebar event timing.

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

`python bench/paired_room_filter_browser.py` later compared the filter directly with pinned Campfire in Chromium on both new-room types. Before the fix, Rustfire set `hidden` on nonmatching rows immediately and left the source's `filter--active` and `selected` classes unused. After matching Campfire's 300 ms class update and correcting Rustfire's more specific CSS display rule, both apps showed the same 51 initial rows, two `alp` matches with lowercase and uppercase input, zero results for `nomatch`, and all rows after clearing. The list and row class states also matched. This verifies these four inputs on new-room forms; edit forms, other input strings, and every locale remain open.

The paired probe now also compares four source-shaped edit pages: an open and a private edit route for each created room. Both the edit and delete panels matched in parsed tags, attributes, and non-whitespace text after normalizing only CSRF tokens and absolute form origins. Source-style POST forms with PATCH override converted the open room to private membership `[1, 42]` and the private room to open membership for all 51 active users, with the same 302 redirects and database state in both apps. The shared-room delete form returned 302 to `/`, and room 6 and its memberships were gone in both databases. In a Rustfire browser check, switching a private room to open preserved the edited name; saving landed on the room and gave it all 51 memberships. This checks selected workflows on one fixture, not all authorization and validation cases, full-page visual parity, or performance.

The paired edit probe also renamed the open and private rooms, then converted each to the other type, while subscribed to the signed global and per-user sidebar streams. Both apps emitted the same four `replace` events, byte for byte: open room 6 on the global stream, private room 7 on the user stream, converted private room 6 on the user stream, and converted open room 7 on the global stream. A separate `--operation update` fanout trial renamed one 51-member open room ten times after warmup, with 1,000 or 5,000 sockets subscribed to the global stream. It ran each app serially on one host with 22 Puma workers for Campfire and one release Rustfire process. The sampled `replace` event was **448 bytes** and byte-identical. Both trial orders at each socket count delivered every expected event, with zero missed, unexpected, or early-closed deliveries:

| Subscribed sockets | Expected deliveries per app | Trial order | Rustfire elapsed / p95 | Campfire elapsed / p95 |
| ---: | ---: | --- | ---: | ---: |
| 1,000 | 10,000 | Rustfire first | 137 / 21 ms | 757 / 109 ms |
| 1,000 | 10,000 | Campfire first | 132 / 20 ms | 694 / 100 ms |
| 5,000 | 50,000 | Rustfire first | 511 / 81 ms | 1,230 / 155 ms |
| 5,000 | 50,000 | Campfire first | 471 / 74 ms | 987 / 193 ms |

The interval covers ten sequential PATCH-override POSTs and delivery to the final socket, after two HTTP warmups, two subscribed-stream warmups, and a 500 ms settling interval. Socket setup is excluded. The final name and all 51 memberships matched in both databases. This establishes a faster matched update/sidebar-replacement burst on this fixture, not sustained capacity or whole-app superiority.

The room-route probe also found that the pinned Campfire build returns 302 to `/rooms/ID` for open and private room alias GETs; Rustfire now matches those statuses and locations. A direct-room alias GET returned Campfire's generic 500 page on two disposable runs while the earlier Rustfire build returned 303 to the usable room page. A later route fix matched the source's 500 status, media type, and full error-page hash, as checked by the expanded room-route probe below.

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

`bench/paired_message_cache.py` seeded 40 matching messages with distinct subsecond creation times in disposable databases and ran the pinned Campfire build with isolated Redis. Rustfire sends weak ETag, Last-Modified, and `Cache-Control: max-age=0, private, must-revalidate` headers on nonempty message pages. Both apps returned 304 with an empty body for matching ETag or Last-Modified, 200 for a stale date, and 204 for an empty page. The default and before/after pages contained the same ordered message IDs. After one message's update timestamp changed, both returned 200 with a new validator. The source and Rustfire ETag values originally differed; the probe now asserts that the **exact initial, before, and after ETags match**. Full HTML bodies still differ: the latest sampled 40-message 200 pages were **407,158** and **339,637 bytes**, respectively.

The current release build also passes 16 HTML message-history cursor cases and four JSON-Accept cases on that fixture. New cases cover duplicate scalar cursors, nested arrays and hashes, scalar-then-nested parsing errors, numeric-prefix IDs, and `before` precedence over `after`. Rustfire now matches Campfire's complete 400/404/500 error body hashes and content types, successful ordered IDs and parsed message markup, and 304 validator behavior on successful cursors. These are sampled 40-message page states; they do not prove every query shape or change the historical throughput results below.

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

After switching to **byte-identical source ETags**, `python bench/paired_message_cache.py --clients 32 128 --seconds 5 --campfire-workers 22` and the same command with `--rustfire-first` rechecked conditional 304 reads in both orders. Every measured response had the expected empty body and its app's exact, now matching, ETag and Last-Modified; all four paired trials had zero errors.

| Clients | Order | Rustfire requests/s / p95 | Campfire requests/s / p95 |
| ---: | --- | ---: | ---: |
| 32 | Campfire first | 27,148 / 1.89 ms | 3,834 / 25.07 ms |
| 128 | Campfire first | 27,625 / 7.41 ms | 4,617 / 46.26 ms |
| 32 | Rustfire first | 25,803 / 2.01 ms | 3,798 / 22.57 ms |
| 128 | Rustfire first | 28,421 / 7.12 ms | 4,806 / 38.33 ms |

These five-second same-host trials confirm the sampled conditional-read advantage survived exact validator parity. They do not establish sustained capacity or a whole-app speed ratio.

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

The same 32-reader, 100-write, ten-second workload was repeated with 100, 500, and 1,000 signed `RoomMessagesChannel` subscribers. Socket setup and subscription confirmation were outside the measured HTTP interval. Every socket received each expected message append exactly once, with the expected client and numeric message IDs. All writes persisted, all measured reads passed the 40-root check, and no socket missed an event, received an unexpected event, or closed early in either trial order.

| Sockets | Rustfire first | Expected deliveries per app | Rustfire reads/s / read p95 | Rustfire write p95 | Campfire reads/s / read p95 | Campfire write p95 |
| ---: | :---: | ---: | ---: | ---: | ---: | ---: |
| 100 | No | 10,000 | 3,250 / 14.61 ms | 8.62 ms | 1,530 / 39.04 ms | 95.71 ms |
| 100 | Yes | 10,000 | 2,875 / 18.28 ms | 9.47 ms | 1,238 / 61.09 ms | 123.08 ms |
| 500 | No | 50,000 | 3,136 / 14.81 ms | 7.66 ms | 1,288 / 47.14 ms | 85.68 ms |
| 500 | Yes | 50,000 | 2,946 / 16.08 ms | 9.28 ms | 948 / 85.96 ms | 130.46 ms |
| 1,000 | No | 100,000 | 3,029 / 15.81 ms | 7.51 ms | 1,243 / 56.14 ms | 84.67 ms |
| 1,000 | Yes | 100,000 | 3,132 / 15.20 ms | 7.82 ms | 1,158 / 56.37 ms | 96.63 ms |

These are short serial runs on one host with one account, one room, and one writer. The socket checker verifies event identity and delivery count, not complete event bytes or per-event latency. The runs do not establish maximum connections, sustained multi-user capacity, or full application parity.

The mixed probe now captures every append on one subscriber and compares each paired event's parsed tag order, attribute names, stable values, and text. The comparison excludes generated timestamps and the server origin in the copy link. It also excludes nonempty hidden CSRF inputs: Campfire's fragment cache emitted eight such inputs in two of 100 new-message events in one run and none in the other, while Rustfire's broadcasts omitted them. The initial and final HTTP pages retain their separate markup and persistence checks. Two fresh ten-second, 100-socket runs in opposite trial orders each matched all 100 paired append structures and delivered **10,000/10,000** events per app, without read, write, or socket errors:

| Rustfire first | Rustfire reads/s / read p95 | Rustfire write p95 | Campfire reads/s / read p95 | Campfire write p95 |
| :---: | ---: | ---: | ---: | ---: |
| No | 3,223 / 14.62 ms | 7.69 ms | 1,483 / 44.35 ms | 82.12 ms |
| Yes | 3,272 / 14.20 ms | 7.82 ms | 1,490 / 41.21 ms | 66.08 ms |

This strengthens message-stream content evidence for the tested plain-text workload. It does not establish byte-identical serialization, the behavior of other message types, per-event delivery latency, or sustained capacity.

The same all-event comparison also passed at **1,000 subscribers** in both trial orders. Each app delivered **100,000/100,000** expected events, with no read, write, or socket errors. Rustfire served **3,108 / 3,094 reads/s** at **14.89 / 15.34 ms p95**, versus Campfire's **1,262 / 1,186 reads/s** at **54.36 / 60.12 ms p95**. Rustfire's write p95 was **8.63 / 7.97 ms**, versus Campfire's **144.10 / 115.72 ms**. Campfire emitted CSRF inputs in none of the first run's 100 appends and seven of the reverse run's 100; these were normalized as described above. This remains a short same-host, one-account fixture rather than a sustained or maximum-capacity result.

## Paired multi-room, multi-user reads and writes

`bench/paired_message_multi.py` spreads the full-HTML readers evenly across every room/user pair and schedules one authenticated writer per room. Each room starts with 40 matched messages. The paired probe checks initial parsed message pages, every measured GET's HTTP 200 status, HTML media type and 40 message roots, every POST's Turbo response, the saved room, creator, client ID and body, and each final page's latest 40 IDs. All checks passed with zero measured read errors. Each writer completed inside the ten-second read interval, so the full write set overlapped the measured reads.

| Rooms / users | Read clients | Writes per app | Rustfire first | Rustfire reads/s / read p95 | Rustfire write p95 | Campfire reads/s / read p95 | Campfire write p95 |
| ---: | ---: | ---: | :---: | ---: | ---: | ---: | ---: |
| 4 / 4 | 32 | 200 | No | 3,228 / 14.31 ms | 24.51 ms | 1,620 / 34.36 ms | 97.59 ms |
| 4 / 4 | 32 | 200 | Yes | 3,315 / 14.02 ms | 24.39 ms | 1,553 / 38.96 ms | 111.95 ms |
| 8 / 8 | 64 | 240 | No | 3,010 / 30.13 ms | 67.87 ms | 1,477 / 92.51 ms | 157.38 ms |
| 8 / 8 | 64 | 240 | Yes | 2,962 / 32.08 ms | 68.17 ms | 1,029 / 157.74 ms | 263.08 ms |

These are serial same-host runs with one release Rustfire process, 22 Campfire Puma workers, isolated Redis, and a local Go load generator. The four-room runs scheduled five writes per second per room; the eight-room runs scheduled three per second per room. They demonstrate better throughput and p95 latency for Rustfire on these sampled multi-user HTTP mixes. They do not include socket fanout, CPU or memory accounting, a separate load-generator host, hours of steady traffic, or complete application parity. Campfire's results varied between trial orders, so a sustained capacity limit is not established.

The same multi-user probe was repeated with **100 signed subscribers per room**, under the corresponding writer's account. Every socket received all of its room's appends exactly once. The captured numeric message IDs matched the saved client IDs and rooms; there were no unexpected events or early closes. The four-room runs each delivered **20,000/20,000** events per app, and the eight-room runs each delivered **24,000/24,000**. Both trial orders passed all HTTP and database checks:

| Rooms / users | Read clients | Sockets | Writes per app | Rustfire first | Rustfire reads/s / read p95 | Rustfire write p95 | Campfire reads/s / read p95 | Campfire write p95 |
| ---: | ---: | ---: | ---: | :---: | ---: | ---: | ---: | ---: |
| 4 / 4 | 32 | 400 | 200 | No | 3,261 / 14.26 ms | 25.09 ms | 1,379 / 47.59 ms | 129.82 ms |
| 4 / 4 | 32 | 400 | 200 | Yes | 3,255 / 14.24 ms | 26.25 ms | 1,419 / 52.02 ms | 104.09 ms |
| 8 / 8 | 64 | 800 | 240 | No | 3,204 / 28.87 ms | 60.91 ms | 1,370 / 112.66 ms | 206.58 ms |
| 8 / 8 | 64 | 800 | 240 | Yes | 3,186 / 28.96 ms | 68.29 ms | 1,360 / 104.51 ms | 211.62 ms |

Socket subscription and reader warmup were outside the measured read interval. Each room's writer finished within ten seconds. This checks delivery counts and message identity under mixed activity across several rooms and accounts; it does not compare full event bytes, measure delivery latency, or establish a sustained limit. All clients and servers shared one host.

The eight-room, eight-user, 64-reader, 800-socket workload was then extended to **60 seconds** at three scheduled writes per second per room. Every writer finished within the read interval, and both apps saved **1,440** writes and delivered **144,000/144,000** expected events in each trial order. Measured read errors, missing or unexpected socket events, and early socket closes were all zero. CPU seconds and peak proportional set size (PSS) were sampled after socket setup and reader warmup through the read/write interval. Rustfire's resource total covers one server process; Campfire's includes the Puma parent, 22 workers, and isolated Redis. The Go reader and eight Node socket clients are excluded.

| Rustfire first | Rustfire reads/s / read p95 | Rustfire write p95 | Rustfire CPU s / peak PSS MiB | Campfire reads/s / read p95 | Campfire write p95 | Campfire CPU s / peak PSS MiB |
| :---: | ---: | ---: | ---: | ---: | ---: | ---: |
| No | 3,079 / 29.89 ms | 58.45 ms | 1,550.58 / 166.88 | 1,500 / 98.43 ms | 138.95 ms | 1,284.55 / 4,148.51 |
| Yes | 3,024 / 31.07 ms | 69.41 ms | 1,487.61 / 166.93 | 1,501 / 82.86 ms | 150.11 ms | 1,317.07 / 4,092.73 |

The release Rustfire process sustained roughly twice the checked read rate with lower read and write p95 latency on this one-minute fixture, while using much less sampled server process memory under the tested 22-worker Campfire configuration. Rustfire consumed more aggregate server CPU seconds because it completed roughly twice as many reads. This is still a serial local comparison of plain-text messages in eight rooms, not a maximum connection count, an hours-long stability test, or proof of full-app superiority at complete parity.

The multi-room socket probe now also compares every captured append's parsed tags, attribute names, stable values, and text. It normalizes only generated timestamps, independently allocated message IDs in paths and attributes, server origins in copy links, and Campfire's intermittent nonempty hidden CSRF inputs. Both ten-second trial orders with eight rooms, eight authors, and 800 sockets passed all **240** paired append comparisons while delivering **24,000/24,000** events per app. A fresh 60-second Campfire-first run passed all **1,440** paired comparisons and delivered **144,000/144,000** events per app, with 1,440 persisted writes and zero measured read or socket errors. In that minute run Rustfire served **3,112 reads/s at 29.55 ms p95** versus Campfire's **1,582 at 111.93 ms**; write p95 was **60.09 / 158.90 ms**. The full parsed comparison does not establish byte-identical HTML, attachment or rich-text parity, per-event latency, or hours-long stability.

### Higher-subscriber mixed-workload sweep

The same eight-room, eight-user fixture was tested with 64 concurrent checked page readers, three scheduled plain-message writes per second per room, and more signed room-stream subscribers. Both apps ran serially on the same 32-logical-CPU host with disposable SQLite databases; Campfire used 22 Puma workers and isolated Redis, and Rustfire used one release server process. Socket setup and reader warmup preceded each measured interval. Both trial orders passed all read, write, saved-row, latest-page, socket-identity, and parsed Turbo append checks at 2,000, 4,000, and 8,000 subscribers for 60 seconds. Each app saved 1,440 writes in the measured interval, delivered every expected socket event without an unexpected event or early close, and matched all 1,440 paired append structures. Measured read errors were zero.

| Subscribers | Rustfire first | Deliveries per app | Rustfire reads/s / read p95 | Rustfire write p95 | Rustfire peak PSS MiB | Campfire reads/s / read p95 | Campfire write p95 | Campfire peak PSS MiB |
| ---: | :---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 2,000 | Yes | 360,000 | 2,984 / 31.96 ms | 68.48 ms | 358.88 | 1,418 / 102.37 ms | 173.01 ms | 4,652.40 |
| 2,000 | No | 360,000 | 3,125 / 30.79 ms | 71.31 ms | 354.62 | 1,423 / 124.01 ms | 179.80 ms | 4,709.10 |
| 4,000 | Yes | 720,000 | 3,051 / 32.22 ms | 61.08 ms | 646.01 | 1,223 / 151.20 ms | 230.24 ms | 5,132.18 |
| 4,000 | No | 720,000 | 2,940 / 33.78 ms | 63.38 ms | 645.85 | 1,202 / 143.16 ms | 247.09 ms | 5,092.48 |
| 8,000 | Yes | 1,440,000 | 2,754 / 38.72 ms | 68.85 ms | 1,235.70 | 498 / 515.08 ms | 533.49 ms | 4,799.68 |
| 8,000 | No | 1,440,000 | 2,701 / 39.76 ms | 67.55 ms | 1,233.79 | 661 / 330.24 ms | 478.76 ms | 5,220.91 |

During the minute-long measured intervals, Rustfire used 1,480–1,554 aggregate server CPU seconds and Campfire used 1,286–1,308. The CPU figures reflect different numbers of completed page reads, so they are not a per-request efficiency comparison. Sampled peak PSS covers Rustfire's server process or Campfire's Puma parent, workers, and Redis; it excludes the local Go HTTP reader and Node socket clients. Campfire's read rate varied between trial orders, especially at 8,000 subscribers. Rustfire had higher checked read throughput and lower read and write p95 in both orders at every sampled point.

At **16,000 subscribers** (2,000 per room), the ten-second scheduled workload separated the apps on the writer deadline in both orders. Rustfire completed its 240 scheduled writes within the measured interval; Campfire's slowest room writer took 12.54–12.93 seconds, so the paired harness correctly exited nonzero. Both apps eventually persisted all 240 writes and delivered all **480,000/480,000** expected socket events per run. All 240 paired append structures matched after the same documented normalization, with zero measured read errors, missing or unexpected events, and early closes.

| Rustfire first | Rustfire reads/s / read p95 | Rustfire write p95 / slowest writer | Rustfire peak PSS MiB | Campfire reads/s / read p95 | Campfire write p95 / slowest writer | Campfire peak PSS MiB |
| :---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Yes | 2,502 / 48.55 ms | 88.63 ms / 9.22 s | 2,395.86 | 198 / 1,234.44 ms | 824.74 ms / 12.54 s | 5,331.09 |
| No | 2,427 / 51.85 ms | 88.15 ms / 9.25 s | 2,392.00 | 140 / 1,684.36 ms | 889.17 ms / 12.93 s | 5,219.28 |

These results establish an advantage for Rustfire on this specific plain-message, eight-room mixed workload and a tested ten-second scheduled-write threshold that Rustfire met at 16,000 subscribers while Campfire missed. They do not establish an absolute maximum connection count, dropped Campfire messages, hours-long stability, separate-host capacity, or one-for-one behavior for the rest of the application. Read rates cover the fixed ten-second interval; the Campfire write p95 includes writes that finished afterward. Rich text, attachments, notifications, other app activity, raw response bytes, and browser rendering still need equivalent comparisons.

## Room notification involvement

`python bench/paired_involvement.py` passed three initial bell-frame comparisons and eight paired setting changes across open, direct, and private rooms. After normalizing only each session's CSRF input value, each frame had the same parsed element order, attributes, and text as pinned Campfire. The probe also checked the next-setting form action, 302 redirect path, and saved membership preference after every submission. The same probe compared the complete parsed notification-help dialog for ten Chrome, Firefox, Safari, and Chromium Edge user-agent profiles across desktop, Android, and iPhone, normalizing the product name and local root URL. The source treats Firefox on iPhone as Safari and Chromium Edge as Chrome in this view, and Rustfire now follows those observed branches. In a local Chromium browser, the bell opened the help dialog with push unavailable; with a simulated existing subscription, it loaded the form and changed mentions to everything, which persisted in SQLite. A visual check of expanded desktop instructions found and fixed a clipped Close button by making the instruction section scroll. Actual cross-browser push subscriptions and the rest of the room page remain outside this check. This run did not measure speed or scale.

A route-method repeat matched plain POST and DELETE-override POST on the involvement update URL: both returned 404 and preserved the membership preference. Rustfire previously applied the query-string preference on plain POST. The existing PUT-override form still updated the setting on both apps. This is a sampled route and state check, not a delivery or performance measurement.

`python bench/paired_push_subscriptions_page.py` compared the management page on empty and seven-subscription fixtures and again after deleting one subscription. The parsed navigation matched in 10 tokens; the populated subscription area matched in 242 tokens, and the post-delete area in 208 tokens after masking only CSRF values. Browser labels for Chrome, Firefox, Safari on macOS and iPhone, Chromium Edge, Android Chrome, and a missing user agent matched the pinned source. Both apps accepted the same method-override delete form, returned HTTP 302 to the index, and removed the same row. A local Chromium check showed the source-style page with one subscription and no page errors; clicking Delete returned to an empty list without page errors. This checks management behavior, not delivery of real push notifications or all possible user-agent strings.

A route-compatibility extension compared the same empty and seven-subscription page sections through `/users/2/push_subscriptions` and the empty page through `/users/999/push_subscriptions`; they matched the pinned source in 10 navigation tokens and four or 242 subscription-area tokens. `/users/2/sidebar` and `/users/999/sidebar` rendered the same parsed frame as `/users/me/sidebar` within each app. Both apps rejected an invalid subscription endpoint through the explicit-ID alias with HTTP 422, returned 404 for an absent test-notification target, and accepted method-override and direct DELETE through explicit-ID paths. Both deletes redirected to `/users/me/push_subscriptions`, removed the same two rows, and left a 174-token parsed subscription area. A paired profile probe also matched the source's 23-token navigation and 380-token panel through `/users/2/profile` and `/users/999/profile`; a PATCH-style form submitted at `/users/999/profile` updated the signed-in user's name and left user 2 unchanged in both apps. This verifies these sampled nested routes; it does not prove all Rails route variants or real push delivery.

## Search normalization and recent-history parity

`python bench/paired_search_query_edges.py` matched 24 GET queries against the pinned Campfire source on 100-message fixtures. It compares status, media type, and ordered result IDs on successful pages, and complete body length and SHA-256 on error pages. Three operator-only FTS queries (`OR`, `AND`, `NOT`) exposed an empty Rustfire 500 response; Rustfire now returns Campfire's 4,887-byte packaged 500 page for those SQLite evaluation errors. The existing search-history probe still passes. This covers the sampled query strings and results, not all full-text syntax or complete page markup.

The expanded probe passes 30 GET, four GET format, eight POST, and four POST format cases against the same pinned source. The new cases cover duplicate scalar `q`, nested `q[]` and `q[value]`, scalar/nested ordering, URL query precedence over POST body, missing `q`, and JSON Accept or suffix negotiation. It compares complete Content-Type and body hashes on errors, POST redirect targets, and persisted search rows. Campfire returns a packaged 500 for nested-only `q`, an empty 400 when a scalar is followed by a nested `q`, and handles those malformed values before JSON format negotiation; Rustfire now matches those observed responses. The search-history and 100-result page probes also pass. These 46 cases do not establish every parser input, all formats, or search speed.

The pinned source replaces each non-word character in a search query with a space before searching or recording it. It orders recent searches by `updated_at`, touches that timestamp when a query is repeated, and retains the original `created_at`. Rustfire now follows those behaviors and returns the source's HTTP 302 after search submission or history clearing. Its schema migration gives existing Rustfire searches an `updated_at` value from their formerly recency-bearing `created_at`; the Campfire importer now preserves both source timestamps.

`python bench/paired_search_history.py` passed on disposable instances of both apps. A punctuated query returned the same 100 message IDs; three submitted queries produced the same normalized redirect values, HTTP 302 statuses, saved queries, and recent-search order. Repeating a seeded older query changed its last-used time while preserving its original creation time. The full importer probe also passed with a search whose source creation and update times differ, and an old-schema initialization probe preserved an existing search while adding `updated_at`. This checks search semantics and migration, not the full search page's HTML or search throughput.

## Inline boost controls and response parity

`python bench/paired_boost_controls.py` now passes on a matched one-message fixture. The source and Rustfire returned the same parsed boost-list and editor frame tags, attribute names and values, and text for both Turbo-frame and direct GET requests. After a boost was created, the populated list frame also matched. The POST created the same saved boost and returned HTTP 302 to the boost list; the browser-style method-override delete removed it and returned HTTP 204 on both apps. A later run matched five raw form cases: missing and top-level content returned 400; empty, whitespace, and 20-character nested values returned 302 and saved the same content. The final three-boost frame also matched parsed tags, attributes, and text after fixing the ID sequence that had previously reused a deleted boost's ID. Session-specific CSRF values were normalized in the editor; nonempty CSRF inputs were excluded from the cached populated list and direct-page comparison because their presence varied in the source. The existing live append/remove event checks still cover delivery. A paired Chromium check also confirmed that a member's typed boost draft survived another user's live message edit and boost in both apps. These are sampled interactions and do not establish every account/permission state or a new speed measurement.

The Chromium draft probe now submits that retained boost in both apps. Before the fix, Rustfire's JavaScript still looked for the former `.custom-boost-form` class even though its rendered frame uses Campfire's `.boost__form`; submission navigated away from the room. Rustfire now handles the current form and replaces the boost frame from the post-redirect response. Two consecutive paired runs saved both boosts, displayed the submitted one, stayed at `/rooms/1`, and reported no browser console errors. The probe invokes the submit button's DOM click; it does not cover touch keyboard behavior or every boost form failure path.

`python bench/paired_scroll_maintenance_browser.py` passed twice against pinned Campfire after Rustfire's live message replacement stream gained the source's `maintain_scroll` attribute and its stream renderer applied Campfire's scroll-height adjustment. In a 40-message room, a long edit to a message above the viewport increased the scrollable height by 768 px and the scroll position by 768 px in both apps, leaving the visible anchor at the same screen position. A new boost on that message added 32 px of height and 32 px of scroll in both apps; anchor movement was about 0.03 px. A later edit below the viewport added 600 px of scrollable height without changing the anchor or scroll position in either app. The probe checks saved changes, received live updates, and browser console errors. Chromium's native scroll anchoring already preserved the anchor in the sampled first edit before the explicit Rustfire scroll handling; this result also closes the stream-attribute mismatch for live edits. Other viewports, browsers, and concurrent stream sequences remain unverified.

A route-method repeat also checked two rejected attempts before deletion. Plain POST and PATCH-override POST to the boost member URL returned HTTP 404 in both apps, and the boost remained saved. Rustfire previously treated plain POST as a delete; it now dispatches only a DELETE override or direct DELETE to that handler. The valid browser form still returned 204 and removed the boost. This is a route and state check, not a throughput result.

## Browser pasted Open Graph preview parity

`python bench/paired_unfurl_browser.py` passed against the pinned source in local Chromium. A mocked Open Graph response inserted one Trix attachment in each app with matching parsed tags, attributes, and text. Its quoted image URL remained a single `src` attribute, with no injected style attribute. Both browsers requested the preview but inserted no card when the mocked image was missing. In a two-paste sequence, each browser aborted the held first request and rendered only the second preview. The probe isolates browser behavior by mocking the route; it does not prove live fetch, all editor states, or other browsers.

The current release build also passed `python bench/paired_link_preview.py` for five posted preview presentations. Focused Rust checks now match the pinned source's literal-markup title and description cases: `<script>` markup is removed while its inner text remains, and a tag-only value becomes blank. The fetch path now accepts HTML documents only with HTTP 200, matching `Opengraph::Fetch`. These source-derived checks do not exercise a live remote website or all URL and HTML parser cases.

`python bench/paired_unfurl_parameters.py` now passes **31/31** authenticated POST shapes against the pinned source. Before the fix, 17 of the first 20 cases differed: Rustfire ignored `url` in the query string, used the first duplicate form value, rejected nested and non-string JSON values that Campfire treated as no preview, and returned 415 instead of 400 for a plain-text body without a URL. Rustfire now matches query precedence, body and query parser collisions, blank values, and complete HTML/JSON error content types and body hashes. The disposable cases use non-fetchable URLs, so this verifies parameter handling and 204 no-preview behavior rather than remote HTTP fetching or successful metadata extraction.

## Current-build mixed message rerun after link previews

With Rustfire commit `7bae063` and pinned Campfire `91d294f`, the paired mixed probes were rerun in both server orders on disposable fixtures. The one-room run used 32 readers, 100 scheduled posts in ten seconds, and 100 subscribers. Every measured request passed, both apps saved all 100 posts and delivered 10,000/10,000 expected socket events in each order. Rustfire served 3,218–3,299 reads/s at 14.33–14.75 ms p95; Campfire served 1,448–1,475 reads/s at 40.47–46.18 ms p95. Write p95 was 7.20–8.88 ms for Rustfire and 60.70–92.39 ms for Campfire.

The four-room, four-user rerun used 32 readers, 200 scheduled posts at five per second per room, and 100 subscribers per room. Initial pages, saved rows, final pages, and all 200 paired append structures passed the harness comparisons. Both trial orders completed all writes inside the ten-second read interval and delivered 20,000/20,000 events per app without missing events or early closes. Sampled peak PSS includes the Rustfire server process or Campfire's Puma process tree and isolated Redis; it excludes the local load generator and socket clients.

| Rustfire first | Rustfire reads/s / p95 | Rustfire write p95 | Rustfire peak PSS | Campfire reads/s / p95 | Campfire write p95 | Campfire peak PSS |
| :---: | ---: | ---: | ---: | ---: | ---: | ---: |
| No | 3,199 / 14.89 ms | 25.96 ms | 105.44 MiB | 1,434 / 45.75 ms | 106.67 ms | 3,166.68 MiB |
| Yes | 3,210 / 14.54 ms | 25.42 ms | 106.73 MiB | 1,345 / 47.57 ms | 113.92 ms | 3,154.76 MiB |

These are same-host, ten-second trials with one Rustfire process, 22 Campfire Puma workers, and one local load generator. They support a speed and sampled-memory advantage for this checked plain-message workload. The parsed structures match after the benchmark's documented normalization, but raw bytes, other message types, notification side effects, and the rest of the application are not yet equivalent. They do not establish whole-app speed or maximum sustainable scale.

## Link preview presentation filters

`python bench/paired_link_preview.py` posts five identical rich messages to disposable Campfire and Rustfire instances, then compares the complete parsed presentation subtree. The cases cover ordinary text beside a preview, a preview-only attachment, a URL followed only by its matching preview, an X URL with a query string whose preview points to Twitter, and a Twitter profile-image preview. All five now match the pinned source's tag order, attributes, and visible text. Rustfire removes a duplicate solo URL only in the presented message, retaining the original body for search and webhooks. The source's `cf-twitter-avatar` layout marker is applied to the displayed message when the preview image uses Twitter's profile-image path. A fresh debug build passed the full HTTP smoke test, including creation and editing of a preview-only message. This is a rendering parity check, not a load test or proof for other ActionText attachments.

`python bench/paired_reply_browser.py` also passed against the pinned source in local Chromium. In each app, it clicked Reply on preview-only and text-plus-preview messages and compared the editor HTML after normalizing only message links. The preview-only reply quoted the original URL; the text-plus-preview reply quoted its accompanying text. Both used the same Trix `cite` block, kept the source permalink, focused the editor, and assigned `_blank` to the external preview link. The probe then submitted the preview-only reply and observed a saved message and live room presentation containing the expected quote and citation in both apps. This covers two browser reply cases, not all rich text or browsers, and is not a performance measurement.

The optional read mode added 40 distinct solo URL previews to each disposable app, then compared the entire parsed latest-40 HTML page before timing 32 keep-alive readers for ten seconds. Both pages had the same 7,640 parsed element and text events after normalizing generated timestamps, session-specific values and URL origins, and Campfire's optional cached CSRF inputs. Campfire's cached page omitted all 320 CSRF inputs in these runs; Rustfire included them with nonempty values. The Go reader required HTTP 200, HTML content type, and 40 message roots on every response. Rustfire ran as one process and Campfire used 22 Puma workers; servers and load generator shared one host. Both trial orders had zero checked response errors.

| Campfire first | Rustfire reads/s / p95 | Campfire reads/s / p95 | Rustfire / Campfire response bytes |
| :---: | ---: | ---: | ---: |
| No | 3,108 / 15.57 ms | 1,820 / 39.73 ms | 364,410 / 389,051 |
| Yes | 3,068 / 15.95 ms | 1,852 / 32.00 ms | 364,410 / 389,051 |

Rustfire was faster on this checked preview-rich read route in both short runs. The complete page matches as parsed HTML under the stated normalizations, but the raw bytes differ and writes were outside the timed interval. This does not establish parity or a speed advantage for other rich-text constructs or the whole application.

## Rich-text sanitization parity

`python bench/paired_rich_filters.py` compared the complete parsed message presentation after posting seventeen identical rich bodies to disposable Campfire and Rustfire instances. Both apps removed standalone remote images, tables, and sections; stripped event-handler attributes and unsafe links; and retained permitted address/big tags, safe attributes, ordinary links, bold/code text, and a list. Rustfire filters submitted tags before rendering verified mentions and imported inline attachments, so their generated avatars and previews remain visible. The repeatable `--sweep` passed all **123** paired presentations, including 23 permitted-tag, 25 attribute, 43 URL-scheme, seven data-URL, and eight obfuscated-URL cases. These are sampled presentation checks, not every ActionText or attachment path; the sanitizer probe itself does not measure throughput.

## Mixed message rerun after rich-text sanitizer alignment

After the sanitizer changes, `python bench/paired_message_mix.py --clients 32 --seconds 10 --write-rate 10 --campfire-workers 22` passed in both trial orders, adding `--rustfire-first` for the reverse order. Each disposable app started with 40 matched messages and completed 100 browser-style message posts during ten seconds of 32 checked page readers. The harness compared the initial parsed message markup, final message IDs and saved bodies, and required HTTP 200, HTML responses with 40 message roots on every measured read. Write responses had the expected Turbo type and target. There were zero checked read errors; socket capture was disabled.

| Rustfire first | Rustfire reads/s / read p95 | Rustfire write p95 | Campfire reads/s / read p95 | Campfire write p95 |
| :---: | ---: | ---: | ---: | ---: |
| Yes | 3,308 / 14.20 ms | 8.17 ms | 1,540 / 40.28 ms | 60.06 ms |
| No | 3,242 / 14.57 ms | 7.29 ms | 1,616 / 35.89 ms | 91.60 ms |

Rustfire completed roughly twice as many checked reads and had lower sampled read and write latency under this local plain-message workload. The ten-second trials shared one host with the load generator and ran the apps serially; Campfire used 22 Puma workers and Rustfire one process. These checks do not compare each write's full Turbo event, rich-text sanitizer cases under load, long-lived sockets, or maximum sustainable capacity, and they do not establish a whole-app advantage at full parity.

## Main room page shell

After replacing Rustfire's custom room shell with Campfire's page structure, `python bench/paired_room_shell.py` passed against pinned Campfire `91d294f` on disposable fixtures. It compared original, direct, and private rooms; the original room after posting the same message; and that room after uploading the same account logo. The largest message area had **304 matching parsed tokens**. The original room's parsed navigation (180 tokens without a logo, 183 with one), composer footer (55), initial sidebar Turbo frame (4), and optimistic-message template (39) also matched. The comparison normalizes independent CSRF tokens, server origins including the encoded QR URL, generated post times, logo version, and product name. A browser check showed the desktop and mobile layouts, a working mobile sidebar, visible messages, and successful text and file sends without page errors.

A later pass added a seventh message with the uploaded filename `report:Q?.txt`. Both databases retained the raw filename. Campfire and Rustfire rendered `report-Q-.txt` with **1,410 matching parsed message-area tokens** in the seven-message room; the detail and edit pages also matched. Both signed blob redirect, disk download, and proxy routes returned the same file bytes and sampled headers. The two FTS indexes now store the same sanitized filename, and a search for that name returns the message in both apps. Editing the message to a caption and back to an empty body yielded the same caption and sanitized-filename index values in both apps. A focused Rust test checks that database initialization repairs a preexisting raw-name index entry. This checks one sanitized multipart filename and query; other names and upload failure paths need separate checks.

The paired probe also compares standalone plain, rich, text-file, JPEG-image, MP4-video, and PDF message show and edit pages. Their parsed navigation, footer, and sidebar match Campfire's empty sections; the show pages match in main content at 179, 186, 197, 184, 183, and 184 tokens, and the edit pages match at 78, 78, 76, 63, 62, and 63 tokens. Campfire's JPEG variation uses `jpg` in its signed URL; Rustfire now matches it, and both apps returned the same byte-identical 13,036-byte thumbnail. A reconstructed older Rustfire `jpeg` variation URL also returned those bytes. The sampled video poster was byte-identical at 280 bytes, and the sampled PDF preview was byte-identical at 404 bytes. The probe normalizes independent blob IDs and signatures, intermittent source CSRF inputs on attachment quick-boost forms, CSRF token values, times, and local origins. It does not verify raw full-document HTML, every message format, or other attachment variants.

A representation-route repeat passed for that JPEG thumbnail, WebP video poster, and PDF PNG preview. Each app returned 302 from the redirect and legacy representation URLs to signed disk URLs whose downloads matched the variant bytes. Their proxy URLs returned the same bytes and matching content type, Content-Disposition, long-lived cache-control and Last-Modified headers; each app's weak ETag matched the first 32 SHA-256 hex characters of its own URL path. Both returned empty 304 responses to matching ETag and Last-Modified validators, treated a Range request the same way, and served an attachment disposition on request. Rustfire previously returned 200 directly from its representation redirect URL and had no representation proxy route. The paired fixture uses three media samples and does not measure variant generation or download speed.

The full room with those six messages also matched the pinned source after the same normalization: 183 parsed navigation tokens, 54 footer tokens, four sidebar tokens, 1,217 message-area tokens, and 39 optimistic-template tokens. A checked read sweep used this fixture, 22 Campfire Puma workers, one Rustfire process, and a local Go keep-alive client. Both apps stayed running; one received load at a time. Each five-second trial checked every response for HTTP 200, HTML content type, and six message roots. All trials had zero errors:

| Clients | First server | Rustfire reads/s / p95 | Campfire reads/s / p95 |
| ---: | --- | ---: | ---: |
| 32 | Campfire | 3,521.6 / 11.22 ms | 891.9 / 75.86 ms |
| 32 | Rustfire | 3,507.7 / 11.41 ms | 879.4 / 82.25 ms |
| 128 | Campfire | 3,340.5 / 60.70 ms | 991.7 / 252.92 ms |
| 128 | Rustfire | 3,421.7 / 59.88 ms | 1,073.2 / 203.64 ms |
| 256 | Campfire | 3,482.3 / 126.07 ms | 690.7 / 904.42 ms |
| 256 | Rustfire | 3,481.8 / 127.98 ms | 881.1 / 604.08 ms |
| 512 | Campfire | 3,428.7 / 296.72 ms | 828.7 / 818.87 ms |
| 512 | Rustfire | 3,506.0 / 285.98 ms | 1,005.7 / 644.54 ms |

These short bursts show about 3.2–5.0× more checked room reads/s for Rustfire on this mixed-media fixture. At a provisional 250 ms p95 target, Rustfire passed at 256 clients in both orders and exceeded it at 512; Campfire exceeded it at 256 and 512. The 128-client Campfire result straddled that target across orders. The client shared the server host, timed bodies were checked for status, type, and message count rather than parsed equality, media bytes were fetched before load rather than with each room read, and neither app was driven to a sustained failure limit. This supports a scoped speed and sampled-concurrency advantage, not whole-app speed or maximum-scale parity.

After matching Campfire's account-versioned browser icon links in authenticated and public page heads, the same paired six-message fixture and checked reader ran again for five seconds per app at 32 and 256 clients in both orders. The page probe first compared the icon links and existing parsed sections, and every timed read passed status, content-type, and six-message checks with zero errors:

| Clients | First server | Rustfire reads/s / p95 | Campfire reads/s / p95 |
| ---: | --- | ---: | ---: |
| 32 | Campfire | 3,552.2 / 11.03 ms | 949.8 / 75.52 ms |
| 32 | Rustfire | 3,687.0 / 10.75 ms | 870.5 / 77.91 ms |
| 256 | Campfire | 3,575.4 / 120.99 ms | 853.0 / 403.35 ms |
| 256 | Rustfire | 3,547.7 / 124.17 ms | 801.7 / 429.62 ms |

Rustfire retained a **3.74–4.43×** checked read-throughput advantage in these samples. This validates the sampled workload after the account lookup change; it does not establish a sustained capacity limit or whole-app parity.

The checked read mode used the original room after one message and logo upload, 22 Campfire Puma workers, one Rustfire process, and the Go keep-alive client. Every timed response had HTTP 200, HTML content type, and one message root; no errors occurred. Five-second runs at 32 clients and ten-second runs at 128 and 256 clients gave:

| Clients | Rustfire first | Rustfire reads/s / p95 | Campfire reads/s / p95 |
| ---: | :---: | ---: | ---: |
| 32 | No | 3,487.5 / 11.33 ms | 1,308.7 / 50.95 ms |
| 32 | Yes | 3,373.7 / 11.49 ms | 1,268.5 / 49.78 ms |
| 128 | No | 3,399.3 / 60.11 ms | 1,367.8 / 153.29 ms |
| 128 | Yes | 3,256.6 / 63.38 ms | 1,356.2 / 156.96 ms |
| 256 | No | 3,679.0 / 120.17 ms | 1,368.8 / 301.67 ms |
| 256 | Yes | 3,622.9 / 121.80 ms | 1,350.1 / 304.38 ms |

At a provisional 250 ms p95 read target, Rustfire passed at all three sampled concurrency levels; Campfire exceeded it at 256. These serial, same-host bursts show a room-page advantage under this checked fixture. Both servers remained running during each trial, although only one received load. The client shared the host, every timed body was checked for status/type/message count rather than full parsed equality, raw full-document bytes and source JavaScript/Turbo behavior differ, and neither app was tested to a sustained failure limit. This does not establish a whole-app speed or maximum-scale advantage at full parity.

## Search page shell

`python bench/paired_search_shell.py --messages 100` passed against pinned Campfire on a disposable fixture with one recent search. The parsed empty page matched across navigation (19 tokens), sidebar (19), message area (9), and composer footer (29). The no-match page also matched. The 100-result page matched across navigation (29), sidebar (19), message area (18,509), and footer (29) after normalizing generated CSRF values, signed avatar paths, timestamps, and local origins. A local Chromium check on the preceding three-result fixture showed all results visible, the source-style composer and sidebar present, and no page errors after constraining room-only JavaScript to room pages. This does not cover all queries, roles, or visual environments.

`python bench/paired_search_shell.py --messages 100 --browser` now checks the full scrollable result list in isolated Chromium sessions. Before the browser fix, Rustfire opened at scroll position 0 while Campfire opened at the newest result. After the fix, both had 100 results, a scroll height of 8,918 px, a viewport of 543 px, and a scroll position of 8,375 px. The probe then posted a rich message containing external and same-origin links to each app and confirmed `_blank` and `_top` targets respectively. The parsed empty, no-match, and original 100-result sections still matched. This covers one Chromium viewport and one result set, not all browsers or search interactions.

The first 10,000-message concurrent sweep exposed a Rustfire search bottleneck: the query loaded attachments and boosts for all matching rows before retaining the latest 100, and the handler held a database connection while looking up a return room through a second connection. At 64 clients, the latter exhausted the 32-connection pool and stalled warmup. Rustfire now releases the connection before rendering or looking up the return room and materializes the latest 100 visible IDs before loading full message data. On the same disposable 10,000-message fixture, each server returned 100 results whose parsed search-page sections matched before load. The Go keep-alive client checked every timed response for HTTP 200, HTML content type, and 100 message roots. Both servers stayed running, with only one receiving load at a time; Campfire used 22 Puma workers and Rustfire one process. Each measured trial lasted five seconds. All measured runs completed with zero response errors.

| Clients | First server | Rustfire reads/s / p95 | Campfire reads/s / p95 |
|---:|---|---:|---:|
| 16 | Rustfire | 1,445.1 / 16.63 ms | 274.4 / 131.24 ms |
| 16 | Campfire | 1,367.5 / 17.32 ms | 318.3 / 111.75 ms |
| 64 | Rustfire | 1,509.1 / 58.38 ms | 324.1 / 435.65 ms |
| 64 | Campfire | 1,505.0 / 57.96 ms | 312.3 / 468.33 ms |
| 128 | Rustfire | 1,502.0 / 130.89 ms | 352.9 / 633.61 ms |
| 128 | Campfire | 1,478.9 / 132.64 ms | 318.3 / 685.26 ms |
| 256 | Rustfire | 1,470.4 / 283.95 ms | 371.0 / 1,023.63 ms |
| 256 | Campfire | 1,477.6 / 297.50 ms | 314.9 / 1,309.74 ms |

These runs show roughly 4–5× higher checked search-page read throughput in Rustfire and substantially lower p95 latency at every sampled client count. At a provisional 250 ms p95 read target, Rustfire passed at 128 clients and exceeded it at 256; Campfire passed at 16 and exceeded it at 64. The precise threshold between those samples, sustained capacity, write behavior, memory use, and whole-app advantage at full parity remain unmeasured. The client shared the server host, raw HTML bytes differ, and each timed response was checked for status, type, and message count rather than fully parsed equality.

The warm serial `python bench/paired_search.py --messages 10000 --requests 20` probe on the same code checked all 100 ordered result IDs and body texts in every response. Rustfire measured **3.82 ms median / 4.46 ms p95** with an 866,254-byte response; Campfire measured **24.98 ms median / 63.75 ms p95** with a 1,050,197-byte response. This is a one-client observation, separate from the concurrent sweep.

## Mixed rich-text reads, writes, and room fanout

The paired mixed harness now supports `--rich-writes`, cycling five formatted bodies across 100 scheduled posts: bold links, safe classes beside rejected links, lists, time attributes, and raw images that Campfire removes. With 32 concurrent checked page readers, 100 posts over ten seconds, and signed room subscribers, both apps saved all posts and delivered every expected socket event. The harness compared the complete parsed initial and final latest-40 message pages after normalizing generated times and optional nonempty CSRF inputs. It also compared the parsed markup of every one of the 100 paired Turbo appends, while checking delivery counts and message identity on every socket. All comparisons passed with zero measured read errors, missing or unexpected events, or early socket closes. Campfire used 22 Puma workers and isolated Redis; Rustfire used one release process. Both apps and the load generator ran serially on the same host.

| Subscribers | Rustfire first | Deliveries per app | Rustfire reads/s / read p95 | Rustfire write p95 | Campfire reads/s / read p95 | Campfire write p95 |
| ---: | :---: | ---: | ---: | ---: | ---: | ---: |
| 100 | Yes | 10,000 | 3,263 / 14.03 ms | 7.92 ms | 1,401 / 53.84 ms | 88.83 ms |
| 100 | No | 10,000 | 2,891 / 16.43 ms | 9.76 ms | 1,454 / 41.96 ms | 85.82 ms |
| 1,000 | Yes | 100,000 | 3,121 / 15.28 ms | 8.51 ms | 1,205 / 59.24 ms | 75.82 ms |
| 1,000 | No | 100,000 | 2,991 / 16.09 ms | 9.39 ms | 1,150 / 59.88 ms | 86.53 ms |

Rustfire had higher checked read throughput and lower read and write p95 in all four trials. Each app completed its 100 writes within the ten-second reader interval. These short runs cover five sampled rich-text bodies in one room, not all ActionText attachments or notification side effects. The reader checks response status, type, and message count during the timed interval; it does not compare every measured page body. This does not establish a maximum subscriber count or a whole-app speed advantage at full parity.

## User profile panels

`python bench/paired_user_profiles.py` passed against pinned Campfire `91d294f`. On matched disposable fixtures, the administrator's own page and active, banned, deactivated, and bot user panels had identical parsed element order, attributes, and visible text after normalizing signed avatar, device-transfer, QR, and CSRF values. The matched panel sizes were 62, 76, 32, 15, 15, and 12 parsed tokens, respectively. Navigation, footer, sidebar, and body classes also matched in all six states. After both apps received the same account logo, the same page sections and classes matched for the administrator's own profile, a member's own profile, and a member viewing the administrator; their panels contained 62, 23, and 23 parsed tokens. Each app also accepted the source's form-based `_method=delete` unban request, redirected to `/users/2`, and saved active status. A later run matched six signed-in and anonymous avatar GET responses for `me`, numeric, and malformed user IDs: signed-in requests returned 404 and anonymous requests redirected to sign-in. Signed avatar GETs succeeded for active, banned, and deactivated member and bot profile states in both apps. This checks nine page states, one interaction, and sampled avatar routes; it does not establish raw-document, all-role, or performance parity.

`python bench/paired_profile_update_edges.py` now passes nine sequential profile updates on disposable matched accounts: padded and blank names, non-email-shaped, padded and blank email addresses, a one-character and blank password, a 270-character bio, and an email collision with another user. It compares status, redirect, response-body hash when present, saved name/email/bio, and whether the password digest changed; a fresh sign-in with the one-character password succeeds in both apps. Rustfire now preserves submitted whitespace and long bios, accepts Campfire's unconstrained email and password values, keeps existing fields when omitted, and returns the source's 500 page on the unique-email database error without changing the row. `python bench/paired_profile.py --requests 32` also passed its existing page, avatar, transfer, and normal mutation checks after the change. The short read timings from that regression command are not a capacity result.

## Paired Action Cable presence

`python bench/paired_presence.py` passed against pinned Campfire `91d294f` and the current Rustfire release build. Both apps cleared a seeded fresh three-connection membership when the server started. With a read-channel subscription open and an unread marker set, an HTTP room GET preserved the marker and sent no read event in both apps. The first presence subscription then cleared the marker and announced the read event. A second distinct presence identifier for the same room produced a second read event. Both apps saved connection counts **1 → 2 → 2 → 1 → 0**, ignored an unconfirmed identifier's `absent` message, preserved a new unread marker across `refresh` and unsubscribe, and emitted no read event for refresh. A second sequence seeded a stale three-connection membership and matched **1 → 1 → 0** across subscribe, refresh, and unsubscribe; refresh and unsubscribe retained its unread marker. This verifies the sampled page and socket sequences; it does not measure throughput, sustained connections, reconnect timing, or every presence edge case.

## Paired unread room events

`python bench/paired_unread_events.py` passed against pinned Campfire `91d294f` and the current Rustfire release build. Three authenticated sockets subscribed to `UnreadRoomsChannel`: the author, one room member, and an outsider. For messages sent while the member was connected and visible, disconnected and invisible, and disconnected and visible, both apps sent one `{ "roomId": 1 }` event to each room member and none to the outsider. The member's saved unread marker was absent, absent, and present in those states, respectively. This verifies the broadcast audience and database state for three cases; it does not measure delivery latency, larger rooms, or other notification settings.

## Paired browser typing indicators

`python bench/paired_typing_browser.py` passed against pinned Campfire `91d294f` in Chromium. With three authenticated users in one room, both apps showed the same inactive state, then `Zed`, `Amy, Zed`, `Amy`, and inactive as the two senders typed and cleared their editors. `Zed` remained visible after three seconds with no stop event. A separate one-shot Action Cable start appeared and then expired without an explicit stop. Rustfire now sorts names, uses Campfire's five-second idle purge and one-second start throttle, and does not send an extra stop on blur or after 2.5 seconds. This is a single-room browser interaction check, not a typing fanout throughput or reconnect test.

`python bench/paired_local_time_browser.py` passed against the same pinned source in Chromium. Two seeded messages on different UTC dates produced identical rendered day headings, short message times, and date-time tooltips. Rustfire previously displayed date and time where Campfire displayed time alone, and repeated that text in the tooltip. The check covers initial room rendering in one browser locale, not live insertions or every locale.

`python bench/paired_form_actions_browser.py` passed against pinned Campfire in Chromium. Both apps discarded a message edit with Escape, saved a replacement body with Ctrl+Enter, showed exactly one confirmation when deleting that message, retained it after cancellation, and removed it after acceptance. A JPEG selected in the profile file input was saved without a separate submit click, and Ctrl+Enter saved account custom CSS. Rustfire's former generic confirmation handler prompted on any delete button inside the edit form, blocking ordinary saves; it now checks the actual submitter. The probe covers one administrator and these controls, not all browser form actions or failure cases. The existing six-message room-page and boost-controls probes also passed after the change.

## Eight-channel mixed workload

The current release build and pinned Campfire passed `python bench/paired_message_multi.py --rooms 4 --users 4 --clients 32 --seconds 10 --write-rate 3 --sockets-per-room 50 --browser-channels --campfire-workers 22 --resources` in both server orders. Each app started with 40 messages per room, ran 32 checked HTTP readers across all room/user pairs, and saved 30 plain-text writes per room within the ten-second interval. Two hundred sockets, all using their room's writer session, each subscribed to the eight channels listed in `bench/README.md`. The harness confirmed all subscriptions, **5,100/5,100** presence read events during sequential socket setup, **6,000/6,000** message appends and **6,000/6,000** unread events during the writes, with no early socket closes or unexpected events. All 120 paired append structures matched after the existing timestamp, URL, ID, and CSRF normalization. Every measured page read passed its status, media-type, and 40-message checks; the initial and final pages and all saved writes were checked separately.

| Rustfire first | Rustfire reads/s / p95 | Rustfire write p95 | Rustfire peak PSS MiB | Campfire reads/s / p95 | Campfire write p95 | Campfire peak PSS MiB |
| :---: | ---: | ---: | ---: | ---: | ---: | ---: |
| No | 3,260 / 14.08 ms | 22.17 ms | 66.49 | 1,494 / 41.83 ms | 151.27 ms | 3,211.41 |
| Yes | 3,258 / 13.96 ms | 20.10 ms | 65.26 | 1,486 / 45.85 ms | 153.14 ms | 3,977.03 |

Rustfire had about **2.18×** the checked read throughput and lower read and write p95 in both orders on this sampled workload. The apps ran serially on one 32-logical-CPU host with disposable SQLite; Campfire used 22 Puma workers and isolated Redis, and Rustfire used one process. Peak PSS includes those server processes and Redis, excluding load clients. Socket setup, including the read-event burst, preceded the timed HTTP interval; the capture's elapsed time is not a delivery-latency measurement. This ten-second, plain-text trial uses one socket identity per room, omits the browser's heartbeat-triggered refresh request, and does not establish full app parity, a sustained capacity ceiling, or an advantage for rich text, uploads, push delivery, and other untested work.

## Mixed read/write rerun after message ID sequencing

After Rustfire switched to a single-statement, monotonic message ID allocator, `python bench/paired_message_mix.py --clients 32 --seconds 5 --write-rate 5 --campfire-workers 22` passed in both server orders (`--rustfire-first` for the reverse). Each disposable app began with 40 matched messages, served 32 checked full-HTML readers, and completed 25 scheduled browser-style Turbo posts. Initial parsed markup, final message IDs and saved bodies, every read's status/type/message count, and every write's status/type/target passed. There were no checked errors. Both apps and the Go reader shared the same 32-logical-CPU host; Campfire used 22 Puma workers and isolated Redis, while Rustfire used one release process.

| Rustfire first | Campfire reads/s / p95 | Rustfire reads/s / p95 | Campfire write p95 | Rustfire write p95 |
| :---: | ---: | ---: | ---: | ---: |
| No | 1,494 / 48.78 ms | 3,380 / 14.40 ms | 96.03 ms | 7.74 ms |
| Yes | 1,381 / 49.21 ms | 3,495 / 13.81 ms | 98.89 ms | 7.32 ms |

Rustfire served about **2.26–2.53×** as many checked reads per second in these two short runs, with lower read and write p95. The single writer ran at five scheduled posts per second; the trial does not measure peak write throughput, multiple simultaneous writers, sockets, resource use, sustained capacity, or full feature parity.

## Rich-text presentation and search text

`python bench/paired_rich_filters.py --sweep` passed 155 paired create cases against the pinned Campfire build. The probe compares each message's parsed presentation and saved FTS search text. Four additional blank browser posts matched status, presentation, and search rows; two edits matched the 302 redirect and updated search text. Campfire retains text inside some disallowed presentation tags and leading whitespace in its searchable body; Rustfire now does the same. A separate importer regression confirms that a missing search row is rebuilt from the original rich text while its displayed HTML remains sanitized. This is a sampled behavior check, not a performance or complete ActionText parity claim.

## Thirty-second mixed workload with 400 browser-channel sockets

The release build passed `python bench/paired_message_multi.py --rooms 4 --users 4 --clients 64 --seconds 30 --write-rate 3 --sockets-per-room 100 --browser-channels --campfire-workers 22 --resources` in both server orders. Each app saved 90 plain-text writes in each of four rooms, delivered **36,000/36,000** expected message appends and **36,000/36,000** unread events to 400 sockets, and had no early socket closes, unexpected deliveries, or checked HTTP read errors. The harness checked the parsed structure of all 360 paired appends, the initial and final latest-40 pages, and all saved writes. Presence setup delivered all **20,200/20,200** expected read events before the timed interval.

| Rustfire first | Rustfire reads/s / p95 | Rustfire write p95 | Rustfire peak PSS MiB | Campfire reads/s / p95 | Campfire write p95 | Campfire peak PSS MiB |
| :---: | ---: | ---: | ---: | ---: | ---: | ---: |
| No | 3,139 / 29.06 ms | 32.50 ms | 104.44 | 1,475 / 88.02 ms | 146.62 ms | 3,817.58 |
| Yes | 3,120 / 29.32 ms | 31.43 ms | 103.90 | 1,469 / 102.78 ms | 161.91 ms | 3,968.12 |

Rustfire served **2.12–2.13×** as many checked reads per second and had lower read and write p95 in both orders. Both apps ran serially on one 32-logical-CPU host with disposable SQLite; Campfire used 22 Puma workers and isolated Redis, and Rustfire used one process. Peak PSS includes the server processes and Redis, excluding load clients. Socket setup preceded the 30-second read/write interval, so these figures do not establish a connection-rate or maximum-user limit. All sockets in each room used one account identity. The workload does not cover rich text, uploads, push, hours of steady load, or full application parity; measured HTTP reads checked status, media type, and message count rather than every response body.

## Only-administrator account behavior

`python bench/paired_last_admin.py` passed against pinned Campfire on a disposable fixture with exactly one administrator. Both apps redirect to `/account/edit` and save the member role when that administrator submits an invalid or missing nested role value or explicitly demotes themselves. Both return 400 for a top-level role field on the source route and 403 for a subsequent administrator action after self-demotion. Both permit the administrator to deactivate themselves, removing their session and open-room membership. This verifies the sampled account mutation paths; it does not establish parity for every role, account state, or recovery workflow.

## Browser lightbox interactions

`python bench/paired_lightbox_browser.py` passed against pinned Campfire in local Chromium. The paired fixture posted the same JPEG to both apps. Opening the room invite QR, uploaded image, and profile QR used the existing dialog in both apps; the image source, download link, and share file URL matched each clicked link. Closing each dialog reset those values, and no separate ad hoc dialog was created. This checks three browser interactions only; it does not measure speed or establish broader media parity.

## Thirty-second mixed workload with four socket identities per room

The release build passed `python bench/paired_message_multi.py --rooms 4 --users 4 --clients 64 --seconds 30 --write-rate 3 --sockets-per-room 100 --socket-users-per-room 4 --browser-channels --campfire-workers 22 --resources` in both server orders. Each room had 25 sockets for each of four authenticated users, with all eight source-shaped browser channels subscribed. Each app saved 90 plain-text writes per room, delivered **36,000/36,000** message appends and **36,000/36,000** unread events, and delivered **5,200/5,200** presence read events during socket setup. All 1,440 captured append samples matched in parsed tag order, attribute names, stable values, and text after the documented timestamp, origin, message-ID, and CSRF normalizations. Every measured HTML read passed its status, media-type, and 40-message checks; initial and final pages and saved writes were checked separately. Neither run had checked errors or early socket closes.

| Rustfire first | Rustfire reads/s / p95 | Rustfire write p95 | Rustfire peak PSS MiB | Campfire reads/s / p95 | Campfire write p95 | Campfire peak PSS MiB |
| :---: | ---: | ---: | ---: | ---: | ---: | ---: |
| No | 3,149 / 29.21 ms | 32.82 ms | 101.93 | 1,502 / 81.25 ms | 110.68 ms | 4,131.43 |
| Yes | 3,173 / 29.06 ms | 32.31 ms | 100.60 | 1,476 / 80.42 ms | 118.99 ms | 3,967.72 |

Rustfire served **2.10–2.15×** as many checked reads per second at the same socket count, with lower read and write p95 in both orders. Both apps ran serially on one 32-logical-CPU host with disposable SQLite; Campfire used 22 Puma workers and isolated Redis, while Rustfire used one release process. Socket setup preceded the measured 30 seconds. This run improves identity coverage over the one-account-per-room trial, but it does not establish maximum concurrent users, connection rate, hours of steady load, rich content or upload throughput, push delivery, or full application parity.

## Browser composer previews and mixed attachments

`python bench/paired_composer_browser.py` passed against pinned Campfire in local Chromium. Both apps sorted queued PNG/JPEG cards the same way, rendered the same preview-card elements and visible labels, removed and re-added the first file, showed a pending upload after Send, cleared the queue, and displayed all three saved text and attachment messages. The saved 47 MB PNG and 13 KB JPEG matched the source fixture bytes in both storage systems. The first Rustfire run exposed its former 25 MB request limit; a later paired HTTP probe verified the streamed composer route above its subsequent 128 MiB limit. This is one browser flow and not a throughput test or proof of behavior for larger files, interrupted uploads, or other browsers.

## Thirty-second image uploads with readers and room sockets

`python bench/paired_image_mix.py --clients 64 --seconds 30 --write-rate 2 --campfire-workers 22 --sockets 100` passed against pinned Campfire in both server orders (`--rustfire-first` for the reverse). Each disposable app began with 40 matching text messages, then saved 60 multipart uploads of the same 505,420-byte JPEG. The probe verified every write response, all 60 saved IDs and original file bytes, every measured read's HTTP status, content type, and 40 message roots, and the final 7,202 parsed page tokens after normalizing generated times, origins, signed blobs, and CSRF inputs. Both apps delivered **6,000/6,000** expected image appends to 100 subscribed sockets in each run with zero misses, duplicates, or early closes. The 60 first-socket stream samples matched in parsed structure and stable values after the same normalizations.

| Rustfire first | Rustfire reads/s / p95 | Rustfire upload p95 | Campfire reads/s / p95 | Campfire upload p95 |
| :---: | ---: | ---: | ---: | ---: |
| No | 3,059 / 29.79 ms | 257.08 ms | 1,560 / 77.56 ms | 350.19 ms |
| Yes | 3,128 / 28.98 ms | 249.75 ms | 1,567 / 85.03 ms | 325.00 ms |

Rustfire served **1.96–2.00×** as many checked reads per second and had lower upload p95 in these two socket trials. The same 30-second upload/read workload without sockets also passed in both orders, at 3,103 / 1,585 and 3,007 / 1,551 Rustfire / Campfire reads per second; upload p95 was 250 / 214 and 264 / 270 ms, so the no-socket runs do not show a consistent upload latency advantage. Both servers ran serially on one host; Campfire used 22 Puma workers and isolated Redis, while Rustfire used one release process. Socket setup was outside the timed interval. These runs use one account and room, do not measure connection setup, CPU or peak memory, and do not establish maximum sustained capacity, upload performance for other media, or full application parity.

The same fixture was raised to **120 JPEG uploads in 30 seconds** (`--write-rate 4`) with 64 readers and 100 sockets. Before the image-metadata change, Rustfire took 31.09 and 31.54 seconds to finish the writer in Campfire-first and Rustfire-first orders; Campfire took 29.54 and 29.48 seconds. Both apps still saved all 120 byte-matching images, matched the final 7,202 parsed page tokens and all 120 sampled append structures, delivered **12,000/12,000** expected socket events, and had zero checked read errors. Those trials failed the strict deadline because Rustfire finished late. The [Campfire-first](results/image-mix-4ups-camp-first.json) and [Rustfire-first](results/image-mix-4ups-rust-first.json) reports retain the results.

Rustfire then changed image analysis to read width and height from one `vipsheader -a` process instead of launching `vipsheader` twice. The paired MIME and room-shell probes still passed, including byte-identical sampled JPEG thumbnails and WebP posters. Repeating the 4-upload/s workload passed in both orders:

| Server order | Rustfire writer finish | Campfire writer finish | Rustfire reads/s | Campfire reads/s |
| --- | ---: | ---: | ---: | ---: |
| Campfire first | 29.96 s | 29.46 s | 2,695 | 1,515 |
| Rustfire first | 29.48 s | 29.49 s | 2,662 | 1,410 |

Both repeat runs again saved every file, matched the final parsed page and all captured append structures, delivered **12,000/12,000** socket events per app, and had zero read errors. Rustfire served **1.78–1.89×** as many checked reads per second while both writers met the deadline. The [Campfire-first](results/image-mix-4ups-camp-first-after-header.json) and [Rustfire-first](results/image-mix-4ups-rust-first-after-header.json) reports show write p95 of 278 / 231 ms and 263 / 319 ms for Rustfire / Campfire, so upload latency did not favor the same app in both orders. One Rustfire writer finished only 0.04 seconds before the deadline. This is one scheduled rate and fixture, not a reliable sustained limit or maximum upload capacity.

A further **100 JPEG uploads in 20 seconds** (`--write-rate 5`) exposed another writer limit. Before a JPEG-header optimization, Rustfire completed the writer at 21.29 and 21.09 seconds in the two server orders, while Campfire finished at 19.45 and 19.48 seconds. Rustfire now reads ordinary JPEG dimensions directly from the frame header, falling back to `vipsheader -a` for unrecognized files; thumbnail generation and sharpening still use two `vips` processes. The focused parser test, paired MIME probe, and room-shell probe passed, including the sampled byte-identical thumbnail. The post-change trial met the deadline in Campfire-first order, but missed it in Rustfire-first order:

| Server order | Rustfire writer finish | Campfire writer finish | Rustfire reads/s | Campfire reads/s |
| --- | ---: | ---: | ---: | ---: |
| Campfire first | 19.71 s | 19.47 s | 2,561 | 1,427 |
| Rustfire first | 20.85 s | 19.47 s | 2,384 | 1,341 |

Every trial saved all 100 byte-matching JPEGs, matched the final parsed page and 100 sampled append structures, delivered **10,000/10,000** socket events per app, and had zero checked read errors. The [before Campfire-first](results/image-mix-5ups-camp-first.json), [before Rustfire-first](results/image-mix-5ups-rust-first.json), [after Campfire-first](results/image-mix-5ups-camp-first-after-jpeg-header.json), and [after Rustfire-first](results/image-mix-5ups-rust-first-after-jpeg-header.json) reports retain the measurements. The optimization did not establish a reliable 5-upload/s capacity point; image processing under concurrent reads remains a bottleneck.

Rustfire then moved thumbnailing and sharpening into one in-process libvips pipeline, retaining the two-process path as a failure fallback. A direct comparison matched the former CLI output byte for byte for JPEG, PNG, and WebP fixtures; the paired room-shell and MIME probes passed. With the same 64 readers, 100 sockets, and JPEG fixture, the 5-upload/s workload passed in both server orders. At 6 uploads/s, Rustfire met the 20-second writer deadline in both orders while Campfire missed it in both. The strict paired harness reports failure at 6/s because of Campfire's writer deadline, after checking every original file and event:

| Rate / order | Rustfire writer finish / p95 | Campfire writer finish / p95 | Rustfire reads/s | Campfire reads/s |
| --- | ---: | ---: | ---: | ---: |
| 5/s, Campfire first | 19.39 s / 94.76 ms | 19.45 s / 232.97 ms | 2,559 | 1,403 |
| 5/s, Rustfire first | 19.38 s / 90.70 ms | 19.51 s / 306.42 ms | 2,681 | 1,404 |
| 6/s, Campfire first | 19.42 s / 97.30 ms | 22.19 s / 328.47 ms | 2,521 | 1,478 |
| 6/s, Rustfire first | 19.42 s / 91.35 ms | 20.26 s / 244.02 ms | 2,715 | 1,388 |

Every run had zero checked read errors, saved all 100 or 120 byte-matching JPEGs, matched the final 7,202 parsed page tokens and every first-socket sampled append, and delivered all 10,000 or 12,000 expected socket events per app without misses, duplicates, or early closes. The [5/s Campfire-first](results/image-mix-5ups-camp-first-inprocess.json), [5/s Rustfire-first](results/image-mix-5ups-rust-first-inprocess.json), [6/s Campfire-first](results/image-mix-6ups-camp-first-inprocess.json), and [6/s Rustfire-first](results/image-mix-6ups-rust-first-inprocess.json) reports retain the exact measurements. Socket setup was outside the measured interval. The two apps and clients shared one host and ran serially. These short trials establish a capacity advantage only for this sampled JPEG upload/read/socket workload; they do not measure hours-long stability, maximum capacity, other image formats under load, or whole-app parity.

The same fixture ran for **60 seconds** at 6 and 7 JPEG uploads/s. At 6/s, both apps completed all 360 writes within 60 seconds; Rustfire's upload p95 was 92.05 ms versus Campfire's 183.00 ms, and its 2,559 checked reads/s exceeded Campfire's 1,412. Raising the rate to 7/s produced a repeatable deadline separation:

| Server order at 7/s | Rustfire writer finish / p95 | Campfire writer finish / p95 | Rustfire reads/s | Campfire reads/s |
| --- | ---: | ---: | ---: | ---: |
| Campfire first | 59.46 s / 125.98 ms | 63.34 s / 200.61 ms | 2,371 | 1,385 |
| Rustfire first | 59.43 s / 94.00 ms | 69.22 s / 325.76 ms | 2,505 | 1,441 |

All three 60-second trials saved every original byte, matched the final parsed page and every captured first-socket append, delivered **36,000/36,000** or **42,000/42,000** expected socket events per app, and had zero checked read errors. The 7/s paired commands reported failure only because Campfire's writer exceeded the 60-second interval. The [6/s Campfire-first](results/image-mix-6ups-60s-camp-first-inprocess.json), [7/s Campfire-first](results/image-mix-7ups-60s-camp-first-inprocess.json), and [7/s Rustfire-first](results/image-mix-7ups-60s-rust-first-inprocess.json) reports retain the measurements. This demonstrates higher sampled capacity for the 60-second JPEG upload/read/socket workload, while connection setup, memory, other media, longer steady-state operation, and full-app feature parity remain outside this measurement.

## Four-room rich-text mix with browser-channel sockets

`python bench/paired_message_multi.py --rooms 4 --users 4 --clients 64 --seconds 30 --write-rate 3 --sockets-per-room 100 --socket-users-per-room 4 --browser-channels --rich-writes --campfire-workers 22 --resources` passed in both server orders. The four authenticated writers each posted 90 messages, cycling bold links, safe classes and rejected URLs, lists, time tags, and rejected images. Each app saved all 360 posts, delivered all **36,000/36,000** expected message appends and unread events, and delivered all **5,200/5,200** presence read events during socket setup. The probe compared 1,440 paired captured append structures and their text, initial parsed message pages, saved authors and bodies, final latest-40 IDs, and every timed page read's status, content type, and message count. There were no checked read errors, missing or unexpected events, or early socket closes; every writer met the 30-second deadline.

| Rustfire first | Rustfire reads/s / read p95 | Rustfire write p95 | Rustfire peak PSS MiB | Campfire reads/s / read p95 | Campfire write p95 | Campfire peak PSS MiB |
| :---: | ---: | ---: | ---: | ---: | ---: | ---: |
| No | 3,211 / 28.58 ms | 33.01 ms | 103.34 | 1,344 / 110.75 ms | 149.17 ms | 3,844.16 |
| Yes | 2,760 / 32.86 ms | 35.62 ms | 106.19 | 1,473 / 86.06 ms | 140.50 ms | 3,994.12 |

Rustfire served **1.87–2.39×** as many checked reads per second and had lower read and write p95 in both orders. Both apps ran serially on one 32-logical-CPU host with disposable SQLite databases; Campfire used 22 Puma workers and isolated Redis, while Rustfire used one release process. Peak PSS includes those server processes but excludes the local load clients. Rustfire spent 709–777 aggregate server CPU seconds versus Campfire's 610–656 while completing more reads; these totals are not a per-request efficiency comparison. Socket setup preceded the timed interval. This sampled workload still does not measure connection rate, raw response-byte equality, actual browser rendering, ActionText attachments, upload work, push delivery, hours-long stability, or maximum supported scale.

Release commit `7f9f038` repeated this workload after the valid-CSRF write-route parity changes, in both server orders. Each app again saved all 360 rich posts, delivered 36,000 message appends and 36,000 unread events with no missing or unexpected socket events, and passed all 1,440 paired append-structure checks. All measured reads passed the status, media-type, and message-count checks; both writer sets met the 30-second deadline.

| Rustfire first | Rustfire reads/s / read p95 | Rustfire write p95 | Rustfire peak PSS MiB | Campfire reads/s / read p95 | Campfire write p95 | Campfire peak PSS MiB |
| :---: | ---: | ---: | ---: | ---: | ---: | ---: |
| No | 2,755 / 32.89 ms | 37.11 ms | 115.91 | 1,425 / 97.16 ms | 187.27 ms | 4,038.88 |
| Yes | 2,733 / 33.25 ms | 36.05 ms | 117.98 | 1,440 / 88.41 ms | 114.33 ms | 4,155.82 |

Rustfire served **1.90–1.93×** as many checked reads per second in this repeat, with lower read and write p95 in both orders. The [Campfire-first report](results/rich-browser-400-7f9f038-camp-first.json) and [Rustfire-first report](results/rich-browser-400-7f9f038-rust-first.json) contain the complete counters. This is a 30-second sampled load, not a remeasurement of the earlier 4,000–16,000-socket points or a whole-app parity claim.

### Two thousand browser-channel sockets

The same four-room rich-text command passed in both orders with `--sockets-per-room 500`: 2,000 signed-in sockets, distributed across four accounts per room, each subscribed to all eight browser channels. Both apps saved all 360 scheduled posts within the 30-second reader interval, delivered **180,000/180,000** message appends and **180,000/180,000** unread events, and emitted **126,000/126,000** expected presence read events during socket setup. The probe compared 1,440 captured append structures across the paired apps and separately checked initial/final pages and saved message rows. Neither order had checked read errors, missing or unexpected events, or early socket closes.

| Rustfire first | Rustfire reads/s / read p95 | Rustfire write p95 | Rustfire peak PSS MiB | Campfire reads/s / read p95 | Campfire write p95 | Campfire peak PSS MiB |
| :---: | ---: | ---: | ---: | ---: | ---: | ---: |
| No | 2,607 / 37.02 ms | 38.37 ms | 348.29 | 1,098 / 152.76 ms | 291.67 ms | 4,356.52 |
| Yes | 2,779 / 34.47 ms | 42.11 ms | 353.29 | 1,093 / 145.46 ms | 260.32 ms | 4,619.81 |

Rustfire served **2.37–2.54×** as many checked reads per second and had lower read and write p95 in both orders at this sampled socket count. The capture timer starts after all subscriptions are ready and the HTTP reader begins; earlier apparent misses at this count were caused by a harness timer that began during sequential socket setup, not by a demonstrated Campfire delivery failure. The apps and load clients shared the host, and each app ran serially with the worker and Redis settings above. These 30-second results do not measure connection establishment rate, sustained or maximum capacity, untested features, or a whole-app advantage at full parity.

### Four thousand browser-channel sockets

The same command with `--sockets-per-room 1000` ran in both server orders: 4,000 authenticated sockets across four rooms and four identities per room, subscribed to all eight browser channels. Both orders saved all 360 rich posts in each app, delivered **360,000/360,000** message appends and **360,000/360,000** unread events, and emitted **502,000/502,000** expected presence read events during setup. All 1,440 paired captured append structures matched, and there were no checked read errors, missing or unexpected events, or early socket closes. The strict 30-second writer deadline passed for both apps when Campfire ran first. When Rustfire ran first, Campfire's last writer finished at 30.015 seconds, **15 ms past** that deadline; Rustfire met it in both orders.

| Rustfire first | Rustfire reads/s / read p95 | Rustfire write p95 / last write | Rustfire peak PSS MiB | Campfire reads/s / read p95 | Campfire write p95 / last write | Campfire peak PSS MiB |
| :---: | ---: | ---: | ---: | ---: | ---: | ---: |
| No | 2,854 / 36.12 ms | 38.89 ms / 29.197 s | 658.20 | 759 / 228.73 ms | 444.02 ms / 29.415 s | 5,038.16 |
| Yes | 2,725 / 37.41 ms | 45.92 ms / 29.198 s | 653.03 | 571 / 496.13 ms | 359.22 ms / 30.015 s | 4,728.38 |

Rustfire served **3.76–4.77×** as many checked reads per second at this load, with lower read and write p95 in both orders. Campfire's 15 ms deadline miss is close to measurement noise, so these two runs do not establish a reliable write-capacity boundary. The apps and load clients shared one host; Campfire used 22 Puma workers and isolated Redis, while Rustfire used one release process. Peak PSS includes the server processes and Campfire Redis, not the load clients. Setup time, connection rate, longer steady-state behavior, untested features, and maximum supported scale remain unmeasured.

The full reports are in `bench/results/rich-browser-4k-camp-first.json` and `bench/results/rich-browser-4k-rust-first.json`. The latter records the strict writer-deadline failure even though all events arrived and markup comparisons passed.

Release commit `7f9f038` repeated the 4,000-socket point after the valid-CSRF write-route parity changes, in both server orders. Each app saved all 360 rich posts, delivered 360,000 message appends and 360,000 unread events, and emitted 502,000 expected presence read events during setup. All 1,440 captured append structures matched; neither order had missing or unexpected events, early socket closes, checked read errors, or a writer deadline miss.

| Rustfire first | Rustfire reads/s / read p95 | Rustfire write p95 | Rustfire peak PSS MiB | Campfire reads/s / read p95 | Campfire write p95 | Campfire peak PSS MiB |
| :---: | ---: | ---: | ---: | ---: | ---: | ---: |
| No | 2,390 / 44.50 ms | 41.11 ms | 663.70 | 763 / 234.29 ms | 512.01 ms | 5,132.50 |
| Yes | 2,357 / 44.64 ms | 47.99 ms | 667.70 | 821 / 223.39 ms | 287.78 ms | 5,189.30 |

Rustfire served **2.87–3.13×** as many checked reads per second in this repeat, with lower read and write p95 in both orders. The [Campfire-first report](results/rich-browser-4k-7f9f038-camp-first.json) and [Rustfire-first report](results/rich-browser-4k-7f9f038-rust-first.json) preserve every check and counter. Socket setup, connection rate, longer stability, other features, and maximum supported scale remain outside this 30-second measurement.

The 4,000-socket, four-room workload was also run for **120 seconds** with Campfire first, at the same three rich posts per second per room and 64 checked readers. Each app saved all **1,440** posts and delivered **1,440,000/1,440,000** message appends, **1,440,000/1,440,000** unread events, and **502,000/502,000** presence read events during setup. All **5,760** paired captured append structures matched. Neither app had checked read errors, missing or unexpected events, or early socket closes.

| App | Checked reads/s | Read p95 | Write p95 | Last write | Peak server PSS |
|---|---:|---:|---:|---:|---:|
| Campfire | 749 | 245.14 ms | 408.40 ms | 120.063 s | 5,400.76 MiB |
| Rustfire | 2,862 | 35.37 ms | 40.98 ms | 119.211 s | 655.21 MiB |

Rustfire served **3.82×** as many checked reads per second in this single, longer trial. Its writers met the 120-second deadline; Campfire's last writer crossed it by **63 ms**, so the harness exited nonzero despite complete persistence and delivery. This narrow miss does not establish a reliable Campfire write-capacity boundary. The apps ran serially on one host, with socket setup outside the timed interval. The [full checked report](results/rich-browser-4k-120s-camp-first.json) preserves the counts and strict failure status. This trial is two minutes, not an hours-long stability test or a full-app parity measurement.

### Eight thousand browser-channel sockets

The same four-room, 64-reader, 30-second rich-text workload ran in both orders with `--sockets-per-room 2000`: 8,000 authenticated sockets across four rooms, four identities per room, and eight browser channels per socket. Both apps saved all 360 rich posts and delivered **720,000/720,000** message appends, **720,000/720,000** unread events, and **2,004,000/2,004,000** presence read events during setup. All 1,440 paired captured append structures matched; neither app had checked read errors, missing or unexpected events, or early socket closes. Rustfire met the 30-second writer deadline in both orders. Campfire completed every write and delivery, but its last writer finished **5.14–6.33 seconds after** the deadline, so both paired trials returned a strict failure.

| Rustfire first | Rustfire reads/s / read p95 | Rustfire write p95 / last write | Rustfire peak PSS MiB | Campfire reads/s / read p95 | Campfire write p95 / last write | Campfire peak PSS MiB |
| :---: | ---: | ---: | ---: | ---: | ---: | ---: |
| No | 2,616 / 43.29 ms | 44.69 ms / 29.20 s | 1,259.91 | 309 / 931.44 ms | 584.20 ms / 36.33 s | 5,714.23 |
| Yes | 2,609 / 42.77 ms | 39.07 ms / 29.20 s | 1,264.07 | 221 / 898.65 ms | 618.77 ms / 35.14 s | 5,478.42 |

Rustfire served **8.47–11.79×** as many checked reads per second at this sampled load. The two trials demonstrate that Rustfire sustained this specific 8,000-socket, 12-posts/s workload within its 30-second write window while Campfire did not under the same settings. All Campfire events still arrived; the failure is a throughput deadline, not data loss. Connection setup is outside the timed interval, and the apps and load clients shared one host. This does not establish maximum supported connections, a production deployment limit, hours-long stability, or a whole-app advantage at full parity. The full reports are in `bench/results/rich-browser-8k-camp-first.json` and `bench/results/rich-browser-8k-rust-first.json`.

### Eight thousand browser-channel sockets for two minutes

The four-room rich-text workload was repeated for **120 seconds in both server orders** with 8,000 authenticated sockets, four socket identities per room, eight browser channels per socket, 64 checked readers, and three scheduled posts per second in each room. Each app saved all 1,440 posts and delivered all **2,880,000/2,880,000** expected message appends, **2,880,000/2,880,000** unread events, and **2,004,000/2,004,000** presence read events during setup. The harness checked 5,760 paired append structures per order; neither app missed an event, emitted an unexpected event, or closed a socket early.

| Server order | App | Successful reads/s | Read errors / p95 | Write p95 / last write | Peak server PSS |
|---|---|---:|---:|---:|---:|
| Campfire first | Campfire | 248 | 2 / 960 ms | 966 ms / 138.13 s | 5,994 MiB |
| Campfire first | Rustfire | 2,376 | 0 / 47 ms | 42 ms / 119.19 s | 1,286 MiB |
| Rustfire first | Rustfire | 2,350 | 0 / 48 ms | 48 ms / 119.20 s | 1,279 MiB |
| Rustfire first | Campfire | 270 | 3 / 915 ms | 918 ms / 137.69 s | 5,833 MiB |

Rustfire met the read and writer gates in both orders. Campfire's two or three checked read errors and 17.69–18.13-second writer deadline misses made both paired trials fail strictly, although it saved every post and delivered every event. Rustfire's observed successful-read rate was 8.72–9.57× Campfire's, and its sampled server peak PSS was 4.56–4.66× lower. Campfire's successful-read rates are observed rates from failed trials, not passing throughput results. Socket setup happened before each fixed read/write interval and was not timed as a connection-rate benchmark. The apps and load clients shared one host. These two-minute samples support a checked capacity advantage for this specific workload, not a maximum connection count, hours-long stability, or a whole-app advantage at complete feature parity. See the [Campfire-first report](results/rich-browser-8k-120s-camp-first.json) and [Rustfire-first report](results/rich-browser-8k-120s-rust-first.json).

### Twelve thousand browser-channel sockets

The same four-room rich-text workload was attempted at **12,000** authenticated sockets, four identities per room and eight browser channels per socket. Campfire completed socket setup twice, but its checked reader failed on **17 requests in each attempt**. The first attempt recorded 6,110 successful reads in 33.61 seconds. A diagnostic rerun recorded 3,059 successful reads in 42.15 seconds; its first failure was a **30-second timeout waiting for HTTP response headers**. The harness stopped on the read failures, so it did not verify Campfire's write deadline, socket deliveries, or final pages at this point.

Rustfire passed a separate run on the same fixture and host: **70,741 checked reads, zero errors, 2,357 reads/s, and 54.00 ms read p95**. All 360 scheduled rich posts finished by 29.19 seconds, inside the 30-second window. Every expected **1,080,000 message appends**, **1,080,000 unread events**, and **4,506,000 presence read events** arrived. The run also checked saved message rows, final pages, and captured event IDs. Rustfire's sampled peak server PSS was 1,870.30 MiB. The compact [Rustfire result](results/rich-browser-12k-rust-only.json) and [Campfire failure reports](results/rich-browser-12k-camp-failures.json) retain the detailed counts.

The harness now records read and writer failures without aborting the second app. Two subsequent **paired runs in opposite server orders** used the same 12,000-socket fixture, 64 checked readers, 30-second window, and 12 rich posts/s. In each order, both apps saved all 360 posts and delivered all **1,080,000/1,080,000** message appends, **1,080,000/1,080,000** unread events, and **4,506,000/4,506,000** presence read events. All **1,440** paired captured append structures matched per order, and final pages and event IDs validated. Neither app had missing or unexpected socket events or early closes.

| Server order | App | Successful reads/s | Read errors / p95 | Write p95 / last write | Peak server PSS |
|---|---|---:|---:|---:|---:|
| Campfire first | Campfire | 157 | 7 / 1,135.19 ms | 662.18 ms / 48.47 s | 6,998.00 MiB |
| Campfire first | Rustfire | 2,230 | 0 / 56.02 ms | 43.23 ms / 29.20 s | 1,881.67 MiB |
| Rustfire first | Rustfire | 2,238 | 0 / 54.95 ms | 66.49 ms / 29.21 s | 1,885.93 MiB |
| Rustfire first | Campfire | 122 | 11 / 1,060.62 ms | 863.81 ms / 48.54 s | 6,783.45 MiB |

Rustfire served **14.22–18.34×** as many successful checked reads per second in these two orders, with zero errors, met the writer deadline, and used **3.60–3.72× less sampled peak server PSS**. Campfire's first recorded read error in each order was a timeout awaiting response headers. Its seven and 11 read errors and 18.47–18.54-second writer deadline misses make both paired trials exit nonzero; its successful-read rates are not passing throughput results. The [Campfire-first report](results/rich-browser-12k-paired-camp-first.json) and [Rustfire-first report](results/rich-browser-12k-paired-rust-first.json) preserve per-room writes, every capture group, and the strict failure status. The earlier separate runs above remain useful evidence of variation. These two short, same-host trials do not identify either app's maximum connections, quantify connection setup rate, prove long-term stability, or establish whole-app feature parity. Repeat on separate hosts before using the results as a deployment capacity claim.

### Sixteen thousand browser-channel sockets

The same four-room rich-text workload ran in both server orders with `--sockets-per-room 4000`: 16,000 authenticated sockets, four identities per room, eight subscribed channels per socket, 64 checked readers, and 360 scheduled rich posts over 30 seconds. Each app saved all 360 posts, delivered **1,440,000/1,440,000** message appends and **1,440,000/1,440,000** unread events, and emitted **8,008,000/8,008,000** expected presence read events during setup in each order. All **1,440** paired captured append structures matched after documented generated-value normalization. There were no missing or unexpected socket events, early closes, or paired validation errors.

| Server order | App | Successful reads/s | Read errors / p95 | Write p95 / last write | Peak server PSS |
|---|---|---:|---:|---:|---:|
| Campfire first | Campfire | 82 | 10 / 1,550.24 ms | 1,051.97 ms / 54.86 s | 7,797.39 MiB |
| Campfire first | Rustfire | 2,152 | 0 / 61.48 ms | 55.18 ms / 29.21 s | 2,507.41 MiB |
| Rustfire first | Rustfire | 2,145 | 0 / 61.05 ms | 54.10 ms / 29.20 s | 2,503.32 MiB |
| Rustfire first | Campfire | 63 | 17 / 1,957.52 ms | 1,053.27 ms / 55.10 s | 7,805.34 MiB |

Rustfire met the reader and writer gates in both orders. Campfire's writes and deliveries all completed, but its read errors and 24.86–25.10-second writer deadline misses caused both strict paired trials to exit nonzero. The successful-read rates are observed rates, not passing Campfire throughput: its readers ran for 49.60–49.80 seconds while timed-out requests drained, versus about 30.03 seconds for Rustfire. These short, same-host trials establish a higher checked capacity for this workload at the sampled 16,000-socket point; they do not establish either app's maximum connections, connection setup rate, hours-long stability, or whole-app parity. See the [Campfire-first report](results/rich-16k-camp-first.json) and [Rustfire-first report](results/rich-16k-rust-first.json).

## Push-subscription registration identity

`python bench/paired_push_registration.py` passed against pinned Campfire `91d294f`. The probe seeded the same legacy subscription in both apps, with Rustfire using its former unique `(user_id, endpoint)` schema, then started the servers. Rustfire migrated that table without losing the row. Both apps touched its timestamp on an exact repost while preserving the original user agent, saved a second row when the endpoint was reused with different `p256dh_key` and `auth_key` values, and kept two rows when the original tuple was reposted again. IDs, endpoints, key fields, agents, and row counts matched. `python tests/import_campfire.py` also passed with two source push subscriptions sharing an endpoint but using different keys. The existing paired seven-subscription management-page probe and 36 Rust unit tests passed after this change. These checks do not validate real browser registration or push delivery.

## Live sidebar ordering

`python bench/paired_sidebar_sort_browser.py` passed against pinned Campfire `91d294f` in Chromium. Both apps started with the same two direct rooms in newest-first order. A shared-room create event added “Zulu” in alphabetical order, and a rename to “Aardvark” placed the replacement link first. A message from another user in the older direct room changed its unread marker, advanced its numeric sort timestamp, and moved it ahead of the newer direct room in both apps. Rustfire now applies the source sorted-list rules after live updates and preserves a replace target while removing duplicate links. This is a paired browser behavior check on one room and account fixture, not a scale result.

## Profile sign-out and push cleanup

`python bench/paired_logout_browser.py` passed against pinned Campfire `91d294f` in Chromium. Each browser opened its own profile, received a mocked service-worker registration with a current-device push subscription, and clicked the rendered Log out button. Both called `getRegistration` for their origin, unsubscribed, reached the sign-in page, and deleted only the signed-in user's matching endpoint while preserving another device and another user's identical endpoint. Before the fix, Rustfire submitted the profile form without its endpoint and its CSRF middleware returned 403 because it classified the authenticated method-override POST as sign-in. The paired route-level `python bench/paired_session.py` still passes. This covers one controlled browser path and does not verify real push service delivery or service-worker errors.

The sign-in probe now also compares complete parsed navigation, main, footer, and sidebar sections in initial and rejected states. Rustfire's sign-in form uses Campfire's absolute action URL, panel class spacing, and configured version badge; the matched fixture uses the same administrator name. The 401 rejection displays the source-shaped flash before `<main>`, with matching parsed alert markup and byte-identical SVG. The pinned Campfire template emits a stray closing `</span>` after its alert icon; the probe ignores that browser-discarded tag when comparing the flash. This establishes parsed structure and behavior for these two sign-in states, not raw HTML equality or all browser conditions.

The same disposable sign-in fixture now sends ten requests from one IP within three minutes, including the earlier failed and successful attempts, then an eleventh wrong-password request. Both apps returned 401 for each of the seven additional wrong passwords and 429 for the eleventh request. The 429 main page matched the 401 rejection locally and across apps after CSRF, origin, logo-version, and product-name normalization. This verifies one rate-limit key and interval, not distributed limiter behavior or persistence across restarts.

## Bot-key route methods

`python bench/paired_bot_admin.py` now checks the pinned route's key rotation methods. Direct PUT, direct PATCH, POST with a lower- or upper-case PUT override, and POST with an `X-HTTP-Method-Override: PATCH` header each generated a new 12-character token and redirected to `/account/bots` on both apps. Plain POST, a form POST without an override, and POST with a DELETE override returned 404 without changing the token. Rustfire previously rotated on plain POST and returned 405 for direct PATCH. The workflow also rechecked bot creation, avatar and webhook edits, API access, deactivation, and legacy token migration. This is route and state parity for one administrator and bot, not a throughput result or a complete bot-security audit.

## Built-in sound messages

`python bench/paired_sound_browser.py` passed against pinned Campfire `91d294f`. The asset check matched the source's 56 built-in sound names, each versioned MP3 path and file byte sequence, and all 15 versioned WebP images with their declared dimensions. In paired Chromium rooms, a second user posted `/play bell`, `/play 56k`, and `/play deeper`. Each app rendered the same normalized DOM tree for the live sound, invoked `Audio.play()` once on arrival and once when its button was clicked, and served matching audio and sampled image responses. `/play unknown` remained ordinary text and triggered no audio. Rustfire previously used a different sound element, button attributes, and unversioned asset URLs. The browser test mocks `Audio`, so it verifies invocation and URL selection rather than physical playback; it does not measure throughput.

## Room-area file drop

`python bench/paired_composer_browser.py` now also passes a drop on the composer and a drop on the message area against pinned Campfire `91d294f` in Chromium. A controlled `DataTransfer` file produced matching queued preview cards on both targets. Removing the composer drop emptied the queue; sending the message-area drop saved one text attachment with byte-identical contents in both apps. Before the fix, Rustfire accepted only drops on the composer, so a room-area drop was not prevented and queued nothing. The existing 47 MB PNG plus JPEG picker workflow still passes. This tests two DOM drop targets and one saved small file, not OS-level dragging, all browsers, or upload throughput.

## Browser push opt-in failure

`python bench/paired_notification_browser.py` passed against pinned Campfire `91d294f` in Chromium. Each app's bell was clicked with a mocked granted notification permission, service-worker registration, push subscription, and HTTP 500 from subscription sync. Both called `getRegistration`, `subscribe`, POST, and `unsubscribe` in that order, marked the first run seen, and left the not-allowed dialog closed. Rustfire previously checked for an existing subscription before opting in, left the browser subscription active after a failed POST, and opened the permission-help dialog. This checks one controlled failure path; real browser permissions, service-worker lifecycle, and push delivery remain open.

The probe now also checks successful sync and standalone PWA denial. Both apps save the same endpoint and key fields on success, replace the bell with room notification settings, and leave the dialog closed. In standalone mode, both show the dialog and set `notifications-pwa-first-run-seen` without setting the ordinary browser cookie. Before any bell click, both show a pulsing alert bell and neither has registered a service worker in a fresh Chromium context. Rustfire previously registered one on every page load. This check still mocks permission and push APIs and does not verify delivery from a push service.

Two additional browser prompt branches also match. When no registration exists and permission is initially undecided, both register `/service-worker.js`, request permission, and subscribe after a grant. If the prompt is declined, both keep the bell, mark the first run seen, and leave the help dialog closed. Rustfire previously opened that dialog after a declined prompt. The test uses controlled permission responses rather than a person interacting with a browser's native prompt.

The same paired Chromium probe now installs a mock existing subscription before either page script runs. Both apps query the registration for the current origin, read the existing subscription once, and replace the bell with the settings form. For Safari-style install instructions, a synthetic `beforeinstallprompt` is prevented and reveals the installer; clicking Install now calls `prompt()` once, and `appinstalled` removes the installer class in both apps. In simulated standalone mode, neither handles the install event. Rustfire previously invoked `prompt()` twice because a page-wide profile handler also caught the room button, and it left the installer class after `appinstalled`. The install probe checks JavaScript behavior, not an actual PWA installation.

## Test-push payload

The revised `python bench/paired_push_invalid_keys.py` probe passed against pinned Campfire `91d294f`. Campfire's delivery pool and Rustfire's message-triggered background worker both removed subscriptions with a 65-byte P-256 point outside the curve and a short valid-base64 point, while retaining malformed-base64 and blank-auth rows. Each app's direct test-notification action returned the same full 500 page for those four key failures and retained the rows. A saved endpoint outside Campfire's push-service allowlist yielded the same empty 302 redirect and was retained. Campfire's encryptor accepted 1-, 16-, and 32-byte auth secrets and a compressed P-256 key; Rustfire's request builder produced decryptable ciphertext for those inputs using a fallback for the nonstandard values. Both encryption paths accepted a 4,078-byte message, producing 4,182 encrypted bytes, and rejected 4,079 bytes. Key failures happened before an outbound push HTTP request. The source web-push library raises `ExpiredSubscription` for HTTP 410, which Campfire's background pool rescues, but raises an unrescued `InvalidSubscription` for HTTP 404; Rustfire now invalidates only background HTTP 410 results. Real push-service responses and browser delivery remain untested.

`python bench/paired_push_response_statuses.py` passed the follow-up controlled response check. Synthetic 404, 410, 503, and 204 outcomes sent through Campfire's delivery pool left subscription IDs `[1, 3, 4, 5, 6, 7]`: only the background 410 row was removed. Direct 410 and 404 exceptions left their rows intact. Rustfire's production response handler reached the same persisted IDs on disposable SQLite and cleared its subscription-present flag when a final background 410 removed the last row. This verifies classification and persistence with controlled outcomes, not an outbound TLS exchange, provider-specific body handling, or live browser delivery.

`python bench/paired_push_test_payload.py` passed against pinned Campfire `91d294f`. The source's `WebPush::Notification` encoder produced a fixed JSON payload, which Rustfire's production test-push payload helper matched as parsed JSON: title, body, icon, unread badge count, and the absolute click URL. Rustfire previously used a relative click path in test notifications. This check isolates payload construction; it does not test encryption, an outbound HTTP request, expiration handling, or a real browser notification.

## Device-transfer route methods

`python bench/paired_transfer_edges.py` passed ten source-signed link cases on disposable accounts. Both apps render an invalid-link transfer page, reject malformed, altered, expired, banned-user, and deactivated-user links with an empty HTTP 400, and create no session for those failures. Rejected HTML, JSON, Turbo Stream, and wildcard requests have matching media types. A valid link and its replay each establish a fresh session and redirect to `/`. Rustfire previously omitted the HTML media type and mislabeled the Turbo Stream error. The existing cross-app POST-form, PATCH, and PUT transfer workflow in `paired_profile.py --requests 32` also passed after the change. This tests local token and account-state behavior, not external QR scanning or device-specific browsers.

`python bench/paired_profile.py --requests 32` passed against pinned Campfire `91d294f` after extending the transfer probe. With each app's signed link opened in a fresh client, both accepted the source-shaped POST form with a PUT override, direct PATCH, and direct PUT; each redirected to the root and established a session that could open `/rooms/1`. The direct requests used the page's global CSRF token because Campfire's hidden form token is scoped to the form method. Rustfire previously returned 405 to direct PATCH. The profile, avatar, and mutation checks in the same run also passed. The 32-request read timings are a short regression sample with different HTML body sizes, not a feature-equivalent capacity claim.

## Paired concurrent generic-file composer uploads

`python bench/paired_upload_capacity.py` compared the pinned Campfire and current Rustfire release builds in both server orders at 4, 16, and 64 concurrent clients. Each client first posted one untimed 8 MiB binary message on its own persistent connection. The timed phase then posted three more files per client at 4 and 16 clients, or two at 64. Every POST returned 200 with a Turbo response containing its client ID. Both databases contained the expected messages, and every saved attachment had the same `application/octet-stream` type, declared byte size where available, and SHA-256 as the submitted file. Campfire used 22 Puma workers and isolated Redis; Rustfire used one process. The servers ran serially on the same 32-logical-CPU host as the Python load generator.

| Clients | Server order | Rustfire uploads/s, p95 | Campfire uploads/s, p95 |
| ---: | --- | ---: | ---: |
| 4 | Campfire first | 481.40, 11.10 ms | 25.03, 174.66 ms |
| 4 | Rustfire first | 516.43, 7.54 ms | 19.87, 284.42 ms |
| 16 | Campfire first | 654.25, 32.43 ms | 42.55, 527.39 ms |
| 16 | Rustfire first | 690.16, 32.89 ms | 39.79, 802.65 ms |
| 64 | Campfire first | 642.21, 124.06 ms | 46.27, 1,696.75 ms |
| 64 | Rustfire first | 618.07, 149.89 ms | 55.02, 1,660.24 ms |

Rustfire completed **11.23–25.99×** as many checked generic-file uploads per second in these short trials. The six [raw reports](results/composer-upload-capacity-camp-first.json) ([4 reverse](results/composer-upload-capacity-rust-first.json), [16 first](results/composer-upload-capacity-16-camp-first.json), [16 reverse](results/composer-upload-capacity-16-rust-first.json), [64 first](results/composer-upload-capacity-64-camp-first.json), [64 reverse](results/composer-upload-capacity-64-rust-first.json)) retain elapsed times, sampled process-tree PSS, CPU seconds, and sample counts. The PSS sampler waits 50 ms between process-tree reads, which also take time; it produced only two or three readings for each Rustfire timed phase, so those samples cannot establish its actual peak memory. These are bursts lasting 0.02–0.21 seconds on Rustfire and 0.48–2.77 seconds on Campfire, not sustained-capacity tests. The load generator shared the host and assembled each multipart body in memory. No sockets subscribed during these trials; Campfire and Rustfire may perform different unobserved broadcast and background work. The comparison covers generic files and checked HTTP/storage outcomes, not image previews, push delivery, full response equality, or whole-application performance at complete parity.

With the same 16-client, 8 MiB fixture and **only the hidden multipart CSRF field** (no CSRF header), both app orders passed all 16 warmup and 48 timed uploads, Turbo response checks, and stored-file hashes. Rustfire completed 692.18 uploads/s at 32.88 ms p95 versus Campfire's 41.35 at 573.33 ms with Campfire first; reversing the order yielded 635.90 at 34.95 ms versus 43.23 at 570.92 ms. That is **14.71–16.74×** more checked uploads per second in these short bursts. The [Campfire-first](results/composer-upload-form-csrf-16-camp-first.json) and [Rustfire-first](results/composer-upload-form-csrf-16-rust-first.json) reports retain elapsed times and sampled resources. The message handler checks the multipart CSRF field while streaming the file, avoiding a buffered copy in middleware. These two 0.07–0.08-second Rustfire intervals cannot establish sustained upload capacity or actual peak memory.

A separate [one-client paired probe](results/composer-upload-form-csrf-129mib.json) with a 129 MiB file and a form-only CSRF token passed the warmup and timed upload in both apps, checking Turbo responses and saved hashes. Rustfire had previously returned 413 before reaching the streaming handler. This is a size-compatibility check with one timed sample, not a capacity estimate.

## Live inline PDF and QuickTime embeds

`python bench/paired_inline_upload.py` now direct-uploads a minimal PDF and Campfire's `alpha-centuri.mov` fixture alongside text and JPEG files. For both media types, the paired source and Rustfire messages have the same parsed presentation and searchable text, retain byte-identical originals, and serve byte-identical preview images (PNG for PDF, JPEG for QuickTime). The QuickTime clip exposed a prior mismatch: Rustfire extracted frame zero, while the pinned Rails 8.2 Active Storage previewer selects a representative scene or keyframe. Rustfire now uses that filter for inline previews and message posters. This is a functional comparison of two fixtures, not a timed workload or evidence for all media formats.

`python bench/paired_attachment_mime.py` also checks the ordinary QuickTime message poster from the same 65.84-second clip. Both apps detect its MIME type from the bytes despite a `text/plain` upload label, preserve the original file, match the parsed Turbo message presentation, and serve the same WebP poster SHA-256. The live inline probe now checks a one-pixel PNG too. The offline import check compares its precomputed PNG, a minimal PDF's PNG, and the clip's JPEG against the preview SHA-256 values measured from pinned Campfire. The importer applies the same libvips sharpening and representative-frame filter. These checks cover sampled fixture bytes and rendering paths, not an exhaustive media corpus.

## Missing-action and method route parity

`python bench/paired_csrf_write_routes.py` passed 29 matched requests against the pinned source. It checks status, media type, redirect, and complete error-body hash while varying valid, invalid, and missing CSRF tokens across account, room, message, ban, bot-key, health, and missing routes. A valid HTML message POST to a deleted room also matches the source's 200 response, document title and body class, empty navigation and sidebar, and normalized composer-frame markup. The prior Rustfire response was a packaged 500. A missing `account` group now returns an empty 400 with the source's HTML media type. The existing 144-case invalid-CSRF sweep, 33-case missing-record write probe, and missing-action route probe passed after the change. The sampled checks do not cover every write route, content type, or method override.

`python bench/paired_route_methods.py` passed ten paired route cases against pinned Campfire `91d294f`: the working account edit and health GETs, missing account/session/bot/user/profile actions, POST to health, anonymous POST to a bot key, and DELETE on the custom-style edit URL. For each, Rustfire and Campfire returned the same 200 or 404 status and neither sent an `Allow` header. Rustfire previously served the account settings page at `/account` even though Campfire has no `show` action there, and Axum returned 405 for several other missing actions. The probe supplies valid CSRF tokens for signed-in non-GET requests; absent-token precedence, 404 body content, and the rest of the route/method matrix remain unchecked.

The paired first-run probe also checks the source's 4-4-4 alphanumeric join-code shape. `python bench/paired_account_users.py` now rotates a join code in both apps, checks the 302 redirect to `/account/edit`, confirms the old invitation returns 404 and the new one returns 200, and preserves its existing account-role and layout checks. Rustfire previously used UUID join codes and redirected rotation to `/account`, a missing action in Campfire. These are route and state checks, not a throughput measurement.

## Touch boost form handoff

`python bench/paired_touch_boost_browser.py` passed against pinned Campfire in a 390-by-844 Chromium viewport with touch capability injected before page scripts load. In both apps, clicking the message-menu custom boost action synchronously focused a temporary invisible input; after the asynchronous boost form loaded, the real input received focus, the temporary input was removed, and the form requested smooth centered scrolling. `python bench/paired_boost_draft_browser.py` still passed for the desktop draft and submit workflow. This checks DOM behavior under emulation, not whether a physical iOS or Android keyboard stays open.

## Invalid room IDs and missing GET actions

The expanded `python bench/paired_room_redirects.py` passed 24 signed-in and 17 anonymous room paths against pinned Campfire. Unknown text IDs such as `/rooms/abc` and `/rooms/opens/abc` redirect to the root after authentication, while `/rooms/new` returns 404 even for an anonymous request because Campfire has no `new` action there. IDs with a numeric prefix, including `1abc` and `1.0`, resolve to room 1 in both apps. Four direct-room namespace GETs return the source's 500 response; Rustfire now matches its status, media type, and complete error-page hash for those URLs.

The expanded `python bench/paired_route_methods.py` passed 43 route and method cases. Missing room-message `new`, boost-show, and push-subscription-show actions return 404 without an `Allow` header. On eight GET route families, wholly alphabetic IDs return 404 for signed-in users and redirect anonymous users to sign-in; IDs with numeric prefixes reach existing user, room, message, and boost records or the route's normal format response. The test seeds a real message before checking its show, edit, and boost paths. Rustfire previously returned a path-parser 400 on those malformed paths. This compares status, `Allow`, and redirect destination, not response bodies or the full route matrix.

## Uncommon inline media MIME types

`python bench/paired_inline_upload.py` now checks two more direct-uploaded rich-text attachments against pinned Campfire. An unknown `image/x-unsupported` subtype with nonimage bytes renders as a generic file in both apps. A valid QuickTime clip declared as `video/x-unsupported` renders an inline preview in both apps; the served JPEG previews have the same SHA-256. Original bytes, parsed message presentation, and searchable text also match. Rustfire previously rendered that video as a generic file.

`python tests/import_campfire.py` also passes with those MIME types in a disposable source database. It imports the unknown image as a file and creates the same precomputed JPEG preview for the valid uncommon video. A blob falsely labeled `video/x-unsupported` but containing text still causes an atomic import failure because its preview cannot be decoded. The importer test was updated to check saved ActionText source separately from its flattened rendered HTML. These samples do not establish parity for every image or video codec and container.

## GET format negotiation and QR suffixes

`python bench/paired_get_formats.py --route-inventory --compare-redirects --role ROLE` derives the application GET paths from pinned Campfire's Rails route table and checks an HTML request at each path and its `.html`, `.json`, and `.turbo_stream` forms. With representative IDs and disposable fixtures, Rustfire matches status, media type, and redirect destination for the sampled administrator, member, and anonymous routes. It now also matches Campfire's 500 responses for invalid QR data, its missing room-settings controller, and direct-room namespace requests. This is route-level coverage with one representative path per Rails route, not a full successful-body, side-effect, or method audit. A separate hand-picked 468-case administrator GET format sweep still passes.

The inventory found missing-action 404 media types, suffixed room URLs captured as IDs, a member-visible room edit page that had been forbidden, and anonymous redirects on generated message routes. Rustfire now follows the source for those checked cases. The edit page hides its delete control from members, as the pinned view does; write authorization remains on the update action.

With four `Accept` headers, the route-inventory suffix sweep now passes **1,172/1,172** cases for each of administrator, member, and anonymous access, with no source 500s excluded. A separate `?format=` sweep passes **888/888** cases for each role, also with no exclusions. Both sweeps compare status, media type, redirect destination, complete 403/406 bodies, and the length and SHA-256 of 500 bodies. The hand-picked conflicting suffix/query sweep passes 288/288 administrator cases. These checks exposed and closed manifest, service-worker, missing-avatar, bot-message, invalid-QR, and room-settings format differences. Successful HTML and JSON bodies, state changes, and security under every route are outside these sweeps.

`python bench/paired_success_bodies.py` passed nine successful GET body cases against the pinned source. Rustfire now returns the Rails health endpoint's byte-identical green HTML document and JSON object; after normalizing only the live timestamp, the JSON serialization matches. The source and Rustfire service-worker JavaScript are byte-identical. One aligned seeded bot message produced matching complete parsed HTML tokens and JSON values for `.html`, `.json`, and `?format=html` requests after normalizing generated CSRF tokens, signed avatar values, and local origins. The probe exposed Rustfire's prior `"ok"` health bodies and a missing charset on bot JSON. This is one message fixture and selected successful routes, not a full successful-response body audit.

`python bench/paired_turbo_native_routes.py` now passes 150 paired GET/HEAD responses for the three Hotwire Native navigation routes. The pinned source returns the same short HTML body even for JSON, Turbo, XML, unknown suffixes, and `?format=json`; Rustfire now does too. The probe compares body SHA-256, media type, Vary, cache, security, and version headers for signed-in and anonymous clients. This checks the HTTP routes, not a native mobile client's navigation behavior.

`python bench/paired_format_routes.py` passed 19 GET cases against pinned Campfire `91d294f`. The probe checks status and response media type for message, room, user, account, and search pages requested as `.json`, `.turbo_stream`, `.html`, or with an `Accept: application/json` header. The sampled HTML-only pages return 406 for unsupported formats in both apps. Format-suffixed QR URLs return SVG with byte-identical bodies, and the manifest and service worker retain their media types. Rustfire previously exposed JSON on HTML-only message reads, returned 200 or 404 on selected unsupported formats, and rejected QR URLs with suffixes. This covers the listed GETs and one seeded message, not the full Rails route-format matrix or all `Accept` combinations.

## Message-create format negotiation

`python bench/paired_message_create_formats.py` passed seven public POST cases against pinned Campfire `91d294f`. Explicit `.turbo_stream` URLs and Turbo-first `Accept` headers returned HTTP 200 Turbo responses; HTML and JSON formats returned the same 406 status, media type, and body in both apps. Every POST, including the 406 responses, saved its distinct message with matching ID, client ID, and text. Rustfire previously returned 404 for suffixed POSTs, redirected HTML requests, and exposed a 201 JSON response to public `Accept: application/json`. A private Rustfire JSON media type remains for smoke-test ID lookups. The `bench/fanout.mjs` load driver now sends the same Turbo `Accept` header to both apps; earlier fanout reports used different POST response formats and should not be read as a matched HTTP-response workload. This probe does not compare complete Turbo bodies or all Rails format variants.

## Message-edit format negotiation

`python bench/paired_message_update_formats.py` passed five PATCH and PUT cases against pinned Campfire `91d294f`. HTML and Turbo-first requests redirect to the message; explicit `.html` overrides a JSON Accept header. JSON requests, including an explicit `.json` URL, save the edit and then return the source's 500 JSON error because its `show.json` template is missing. Both apps matched status, response media type, redirect path, complete response body, and saved text after every request. Rustfire previously returned 200 JSON on the public JSON request and rejected suffixed edit URLs with a path-parser error. The local room editor now requests HTML and handles the redirect before reloading the message. The paired Chromium form-action workflow passed after this change, including Escape to discard and Control+Enter to save in each app. The smoke and WebSocket suites retain an internal Rustfire JSON media type for response inspection. This is sampled parity with the pinned source, including its error, not a claim that JSON editing is a usable Campfire API.

## Message-delete format negotiation

`python bench/paired_message_delete_formats.py` passed six DELETE cases against pinned Campfire `91d294f`. Turbo-first `Accept` and explicit `.turbo_stream` requests returned byte-identical remove frames with the same media type. HTML and JSON formats returned byte-identical 406 responses. Every request deleted its seeded message in both apps, including the 406 cases; Campfire also removed each ActionText row. Rustfire previously returned a redirect or an HTML-typed Turbo frame for unsuffixed requests and rejected suffixed IDs before deletion. The room's delete request now explicitly asks for Turbo, and `python bench/paired_form_actions_browser.py` passed its edit and confirmed/cancelled delete workflow in both apps. This covers sampled status, body, and deletion effects on simple messages, not every attachment cleanup or broadcast edge case.

## Large webhook attachment replies

`python bench/paired_webhook_replies.py` passed fifteen reply shapes against pinned Campfire `91d294f`, including a response just over 26 MiB labeled `application/zip`, a text response streamed in three pieces over eight seconds, and stalled text and partially received ZIP responses. The probe matched webhook request payloads, reply IDs and metadata, the complete stored attachment bytes, and bot message-list JSON; it also checked that Rustfire removed the incomplete ZIP temporary file. Rustfire streams attachment replies into a temporary file and moves the file into attachment storage when saving the message. It previously rejected replies above 25 MiB and buffered all attachment replies in memory. Successful text replies no longer have a Rustfire-only 1 MiB cap. The webhook client now has separate seven-second connect and per-read timeouts, matching the source's `Net::HTTP` settings; a read timeout creates the same bot reply even after response headers or partial file bytes arrive. This verifies these local response shapes, not webhook throughput, peak memory, retries, redirects, or every MIME type.

The same probe now also passes `application/octet-stream` and an unregistered `application/x-rustfire-test` response. Campfire's MIME lookup saves both as `attachment.` because their MIME symbols are nil; Rustfire now follows that behavior. It also checks three aliases: `application/javascript` saves as `text/javascript` with `.js`, `audio/mp4` saves as `audio/aac` with `.m4a`, and `application/xhtml+xml` saves as `text/html` with `.html`. The paired check compares reply rows, full attachment bytes, and bot message-list JSON for all 15 response cases. A missing Content-Type still produces no reply. This extends local MIME coverage without proving every possible response header or webhook failure mode.

## Paired text webhook reply load

`python bench/paired_webhook_reply_capacity.py` scheduled 120 rich user posts at 24 per second to each disposable app, in both server orders. Posts ran concurrently so a slow HTTP response could not delay the next scheduled start; the sampled p95 start lag was below 1 ms in all six runs. One bot was mentioned in an open room on every post. The local webhook endpoint waited 25 ms, returned the same plain-text reply, and recorded every request. Each app saved all 120 triggers and 120 bot replies, sent exactly 120 webhooks, drained its webhook queue without a failed job, and matched the outbound user, room, and message-body payloads after excluding generated message IDs. The probe checked every payload's ID and URL path against its own saved trigger. Campfire ran one Puma worker, isolated Redis, and either four or 16 Resque workers; Rustfire ran one release process. Only one app received load at a time on the same host.

| Campfire workers | Sockets | Server order | App | HTTP post p95 | Webhook p95 | Reply drain after posting | Total trial | Sampled peak server PSS |
|---:|---:|---|---|---:|---:|---:|---:|---:|
| 4 | 0 | Rustfire first | Rustfire | 2.79 ms | 123.70 ms | 0.096 s | 5.057 s | 29.58 MiB |
| 4 | 0 | Rustfire first | Campfire | 186.76 ms | 16,818.24 ms | 17.830 s | 22.853 s | 874.70 MiB |
| 4 | 0 | Campfire first | Campfire | 202.62 ms | 16,309.68 ms | 17.083 s | 22.090 s | 865.62 MiB |
| 4 | 0 | Campfire first | Rustfire | 3.22 ms | 126.55 ms | 0.047 s | 5.008 s | 29.09 MiB |
| 16 | 0 | Rustfire first | Rustfire | 3.38 ms | 124.70 ms | 0.055 s | 5.016 s | 29.63 MiB |
| 16 | 0 | Rustfire first | Campfire | 328.20 ms | 4,501.83 ms | 4.469 s | 9.694 s | 2,478.40 MiB |
| 16 | 0 | Campfire first | Campfire | 297.83 ms | 3,790.43 ms | 3.873 s | 8.931 s | 2,478.62 MiB |
| 16 | 0 | Campfire first | Rustfire | 3.46 ms | 122.38 ms | 0.046 s | 5.008 s | 29.52 MiB |
| 16 | 64 | Rustfire first | Rustfire | 3.33 ms | 125.99 ms | 0.085 s | 5.046 s | 51.17 MiB |
| 16 | 64 | Rustfire first | Campfire | 2,693.39 ms | 4,923.21 ms | 2.458 s | 10.130 s | 2,639.55 MiB |
| 16 | 64 | Campfire first | Campfire | 2,554.87 ms | 4,703.63 ms | 2.329 s | 9.861 s | 2,568.62 MiB |
| 16 | 64 | Campfire first | Rustfire | 3.47 ms | 123.31 ms | 0.114 s | 5.076 s | 51.64 MiB |

At zero sockets, Rustfire saved every reply within 0.05–0.10 seconds after posting ended. Campfire cleared its reply backlog 3.87–4.47 seconds later with 16 workers and 17.08–17.83 seconds later with four. With 64 authenticated room-message sockets, each app delivered all **7,680 user-post appends and 7,680 bot-reply appends** per order, with zero misses, unexpected frames, or early closes. Rustfire completed the 120 HTTP posts in about five seconds; Campfire completed them over 7.53–7.67 seconds, despite sub-millisecond scheduling lag, then saved the last reply 2.33–2.46 seconds later. In both orders, all 120 captured user-post appends and all 120 captured bot-reply appends on one socket matched Campfire's parsed tags, stable attributes, and text after normalizing generated IDs, timestamps, local origins, and CSRF inputs. All 64 sockets received the same 120 saved bot-reply IDs. The initial single-event comparison exposed Rustfire's missing bot bio in the message author and avatar titles; the rendering fix passed every captured reply in the full runs. The comparison does not cover other Turbo events or browser channels. Peak server PSS is sampled every 200 ms and includes Campfire's Puma, Resque workers, and Redis, but excludes the local receiver and load clients. The six reports are [four workers, Rustfire first](results/webhook-replies-rust-first.json), [four workers, Campfire first](results/webhook-replies-camp-first.json), [16 workers, Rustfire first](results/webhook-replies-16-rust-first.json), [16 workers, Campfire first](results/webhook-replies-16-camp-first.json), [64 sockets, Rustfire first](results/webhook-replies-64s-rust-first.json), and [64 sockets, Campfire first](results/webhook-replies-64s-camp-first.json). These short same-host runs support a speed and sampled-memory advantage for this one-bot text-reply workload; they do not measure push delivery, attachment replies under load, other browser channels, longer stability, a capacity limit, or full-app equivalence.

## Paired mixed text and HTML webhook reply load

With `--reply-mode mixed`, the same 120 rich mentions at 24 scheduled posts/s produced alternating indexed `text/plain` and `<strong>` `text/html` replies. Both apps saved all 60 of each kind, with each reply body tied to its triggering post number. The full webhook payloads matched after generated-ID normalization. With 64 subscribed room-message sockets, each app delivered all 7,680 post and 7,680 reply appends per order. The 120 post and 120 reply appends captured on one socket matched parsed Campfire tags, stable attributes, and text after generated-value normalization; reply IDs were checked against saved rows and agreed across all sockets. There were no missing or unexpected events or early closes.

| Server order | App | HTTP post p95 | Webhook p95 | Reply drain after posting | Total trial | Sampled peak server PSS |
|---|---|---:|---:|---:|---:|---:|
| Rustfire first | Rustfire | 2.81 ms | 123.34 ms | 0.085 s | 5.047 s | 49.61 MiB |
| Rustfire first | Campfire | 3,874.55 ms | 6,111.13 ms | 2.463 s | 11.315 s | 2,618.36 MiB |
| Campfire first | Campfire | 4,107.86 ms | 7,170.98 ms | 3.227 s | 12.421 s | 2,591.05 MiB |
| Campfire first | Rustfire | 2.83 ms | 123.41 ms | 0.106 s | 5.067 s | 49.35 MiB |

Campfire ran one Puma worker and 16 Resque workers with isolated Redis; Rustfire ran one release process. Runs were serial on the same host, with PSS sampled every 200 ms and load clients excluded. Rustfire finished this checked workload 2.24–2.45× sooner in the two orders. See the [Rustfire-first](results/webhook-replies-mixed-64s-rust-first.json) and [Campfire-first](results/webhook-replies-mixed-64s-camp-first.json) reports. These short runs do not measure attachment replies under load, push delivery, other browser channels, longer stability, maximum capacity, or full-app equivalence.

## Thirty-second mixed webhook reply load

The same mixed text and HTML fixture scheduled 450 posts at 15/s over 30 seconds, with 64 room-message sockets and 16 Campfire Resque workers. Every webhook payload, saved reply body, and queue-drain condition passed in both server orders. Each app delivered all **28,800 post appends and 28,800 reply appends** per order, with no misses, unexpected events, or early socket closes. All 450 post and 450 reply appends captured on one socket matched parsed Campfire markup after generated-value normalization, and all sockets received the same saved reply IDs. The p95 post-start scheduling lag stayed below 0.7 ms.

| Server order | App | HTTP post p95 | Webhook p95 | Reply drain after posting | Total trial | Sampled peak server PSS |
|---|---|---:|---:|---:|---:|---:|
| Rustfire first | Rustfire | 3.14 ms | 124.81 ms | 0.069 s | 30.006 s | 55.82 MiB |
| Rustfire first | Campfire | 382.70 ms | 7,326.67 ms | 7.727 s | 37.761 s | 2,652.47 MiB |
| Campfire first | Campfire | 1,186.73 ms | 12,882.78 ms | 13.204 s | 43.541 s | 2,747.85 MiB |
| Campfire first | Rustfire | 3.00 ms | 124.81 ms | 0.059 s | 29.996 s | 54.26 MiB |

Rustfire cleared all replies within 0.06–0.07 seconds of the final HTTP post; Campfire's last reply took another 7.73–13.20 seconds. The two total-trial ratios were 1.26× and 1.45×. Campfire's HTTP posts completed at an observed 14.83–14.98/s, close to the offered 15/s, but its webhook latency rose during this run. Both apps finished all work and delivered every checked event. The [Rustfire-first](results/webhook-replies-mixed-30s-rust-first.json) and [Campfire-first](results/webhook-replies-mixed-30s-camp-first.json) reports retain the raw measurements. This is a 30-second same-host workload, not a maximum-capacity or hours-long stability test; it excludes attachment replies, push, other browser channels, and full-app parity.

## Message-list response and cursor parity

`python bench/paired_success_bodies.py` passed 14 matched successful GET bodies: health and service-worker responses, bot message-list HTML/JSON, and the ordinary room message list with three seeded messages, both paging directions, and empty 204 pages. The parsed message-list markup matched after normalizing generated CSRF tokens, avatar signatures, and local origins. `python bench/paired_message_cache.py --messages 3 --campfire-workers 1` and the same probe with `--messages 40` both passed. Its cursor sweep matched successful page IDs and parsed markup, conditional 304 behavior, and the status, media type, and SHA-256 body hash of source-shaped 404 responses for invalid or missing IDs. Campfire gives `before` precedence over `after` and accepts a numeric ID prefix; Rustfire now follows those sampled cases. Neither probe establishes all response-body or full-app parity.

## Pinned browser modules as the default frontend

Rustfire now serves Campfire's pinned JavaScript import map and all 104 compiled JavaScript assets by default; `RUSTFIRE_FRONTEND=rustfire` retains the prior browser script as a fallback. The paired room-shell probe checks the same 92 import-map entries and module preloads, stylesheet URLs, and module entrypoint on room and standalone message pages. It now reads all 119 distinct referenced stylesheet and import-map asset URLs from both running apps; every response matched in bytes, media type, and cache control. It also passed its original, direct, private, plain, rich, text, JPEG, MP4, PDF, and unsafe-filename fixtures after Rustfire's search SELECT gained the author bio column required by its shared message decoder. The original browser uploader's wildcard-Accept multipart request now receives a Turbo Stream, and Turbo's redirected sign-in GET can load the HTML room page.

The paired Chromium probes passed clicked lightboxes, replies, multi-file composer previews and uploads, typing and unread state, offline/reconnect behavior, boost drafts, logout, and message edit, avatar upload, and custom-style form actions with the pinned browser modules. These are sampled interactions; remaining routes, browser profiles, push behavior, and full document bytes are not verified.

A further paired Chromium sweep passed plain and rich composer keyboard shortcuts, date/time tooltips, mocked push opt-in and PWA install prompts, the 51-user room-member filter, scroll maintenance around edits and boosts, live room sorting, sound playback invocation and asset selection, touch-emulated boost handoff, and pasted-link previews. `python bench/paired_rich_filters.py --sweep` passed 163 paired rich-text presentations and search-index bodies, including newly added pasted formatting, URL, image-attribute, and line-separator cases. These checks cover the sampled inputs and mocked browser APIs; they do not establish complete rich-text or cross-browser parity.

With the source modules as the default, `python bench/paired_room_shell.py --read-clients 32 --seconds 5 --campfire-workers 22` passed in both server orders. Its timed reader checked each HTTP 200, HTML media type, and one-message root, with zero errors:

| Server order | Rustfire reads/s / p95 | Campfire reads/s / p95 |
| :--- | ---: | ---: |
| Campfire first | 3,289.9 / 12.49 ms | 1,240.4 / 46.98 ms |
| Rustfire first | 3,531.1 / 11.34 ms | 1,136.9 / 70.43 ms |

Rustfire served 2.65–3.11× as many checked room reads in these two short, same-host samples. The asset graph and sampled page sections matched before timing, but the client did not parse every measured body and the complete HTML documents still differ. This is one room and one account at 32 clients; it does not establish whole-app or maximum sustained capacity.

## Missing-record write routes

`python bench/paired_write_route_edges.py` passed 33 authenticated, valid-CSRF writes to missing users, bots, rooms, messages, boosts, and push subscriptions against pinned Campfire `91d294f`. It compared each status, media type, redirect path, body length, and SHA-256. The source returned its full 4,237-byte 404 page on sampled missing records and its 4,887-byte 500 page on three missing-room paths; Rustfire now matches those bytes. The sweep also found source redirects to `/` for absent room deletion and open/private room updates, and a 400 for an empty profile update. After the fix, Rustfire's release build passed this sweep, the 15-case security-header probe, 46 unit tests, smoke, and the existing route, CSRF, message-cache, and message-format probes. The source's 500 responses reflect its behavior on these requests; this test does not imply those paths are healthy or cover all invalid writes.


## Room and message document metadata

`python bench/paired_room_shell.py --sample-dir /tmp/rustfire-room-head-parity` passed against the pinned Campfire checkout and the current Rustfire release build. The probe now compares every ordered head meta tag after normalizing the generated CSRF token, with both disposable fixtures using the same VAPID key. It checks body classes on standalone message detail and edit pages before and after uploading an account logo. Rustfire no longer emits an extra charset meta tag, and those message pages now add Campfire's `account-has-logo` class. Parsed room sections and message content matched for original, direct, and private rooms and the sampled plain, rich, text, JPEG, MP4, PDF, and unsafe-filename messages. These checks cover sampled pages; complete document serialization, other routes, and browser states remain open.

## Complete parsed room and message documents

`python bench/paired_room_shell.py --sample-dir /tmp/rustfire-full-dom-parity` now compares the complete parsed `<head>` and `<body>` in addition to the existing component checks. It found an extra legacy Trix script on Rustfire room pages; the pinned Campfire frontend no longer loads that script. The probe then passed for original, direct, and private rooms; plain, rich, text-file, JPEG, MP4, PDF, and unsafe-filename messages; standalone message show/edit pages; and an account logo upload. The comparator normalizes generated CSRF tokens, local origins, timestamps, signed blob paths, versioned logo URLs, and the product name. It omits hidden CSRF inputs in the later cached message fragments because the pinned Campfire fixture emits them inconsistently. This establishes parsed markup parity for the sampled documents, not byte-identical HTML or other page states. Raw original-room responses were 38,800 bytes from Campfire and 36,618 bytes from Rustfire.

The paired Chromium composer probe passed after removing the extra script: file previews, removal, dropping, and browser sends matched Campfire. Two five-second, 32-client warm room-read runs passed with zero checked errors. Their timed client checked status, media type, and one-message room content; it did not parse every timed response.

| Server order | Rustfire reads/s / p95 | Campfire reads/s / p95 |
| :--- | ---: | ---: |
| Campfire first | 3,229.6 / 12.26 ms | 1,205.6 / 59.46 ms |
| Rustfire first | 3,389.6 / 11.88 ms | 1,178.2 / 59.87 ms |

Rustfire served 2.68–2.88× as many checked reads per second under this sampled workload. These runs do not establish sustained capacity or full-app speed at feature parity.

## Complete parsed account documents

`python bench/paired_account_update_edges.py` passed 16 account update edge cases against the pinned source. It checks HTTP status, redirect path, response body, and saved account name and effective room-creation restriction after each request. The cases cover padded and blank names, several true and false setting inputs including an empty value, unknown account fields, flat fields, and missing, blank, and scalar account groups. Campfire stores an empty setting as JSON null; Rustfire stores false, which has the same effective restriction in this check. The logo upload and 51-user account workflow probes also passed after the change. This is a sampled form and saved-behavior check, not full account administration parity.

`python bench/paired_account_users.py --users 51 --sample-dir /tmp/rustfire-account-document-parity` and `python bench/paired_account_users.py --users 1100` passed against the pinned Campfire source. The probe now compares the complete parsed `<head>` and `<body>` for administrator and member account settings pages. On the 1,100-user fixture, the administrator page matched 146 head and 40,929 body tokens; the member page matched 146 head and 16,610 body tokens. The administrator document also matched after changing the room-creation restriction, uploading a logo, and deleting it; the logo state added ten body tokens. The comparison normalizes session CSRF values, local origins, generated timestamps, and versioned logo URLs. Both fixtures use the same disposable VAPID key. Rustfire now renders Campfire's version badge in the account footer and applies `account-has-logo` after an upload. Raw HTML bytes, other account states, and browser layout remain outside this document check.

A separate `--users 1100 --clients 1 8 32 --seconds 2 --campfire-workers 22` run passed the same initial document and workflow checks, with zero read errors in both apps. It measured the 500-user page-2 Turbo response and the full account settings page on the same host, running Rustfire first and Campfire second. The page-2 bodies were 874,855 and 1,003,862 bytes; account settings bodies were 1,951,677 and 2,236,954 bytes. The parsed documents matched despite serialization-size differences.

| Path | Clients | Rustfire reads/s / p95 | Campfire reads/s / p95 |
| :--- | ---: | ---: | ---: |
| User page 2 | 1 | 312.3 / 4.15 ms | 8.4 / 152.91 ms |
| User page 2 | 8 | 1,463.2 / 7.67 ms | 48.9 / 270.88 ms |
| User page 2 | 32 | 1,564.4 / 30.59 ms | 99.7 / 539.28 ms |
| Account settings | 1 | 164.8 / 8.15 ms | 3.9 / 313.40 ms |
| Account settings | 8 | 756.7 / 12.94 ms | 25.0 / 381.61 ms |
| Account settings | 32 | 1,025.4 / 47.94 ms | 41.8 / 1,477.06 ms |

At 32 clients, Rustfire served about 15.7× as many checked user-page reads and 24.5× as many checked account-page reads per second. These two-second, one-order samples do not establish sustained capacity or a whole-app speed advantage. The timed user-page client checked status, row count, and first/last users; the timed account client checked status and required controls. Complete parsed parity was checked before timing, not on every timed response.

## Complete parsed invitation and sign-in documents

`python bench/paired_join.py` passed the existing signup, duplicate-email redirect, case-sensitive email, and open-room membership checks. It now also compares the complete parsed `<head>` and `<body>` of the initial invitation page (144 and 282 tokens) and the duplicate-email redirect's sign-in page (145 and 196 tokens). `python bench/paired_session.py` matched complete parsed normal sign-in (145 head, 196 body tokens), rejected sign-in (145, 204), and rate-limited sign-in (145, 204), along with its earlier session, push-subscription removal, and redirect checks. Both disposable fixtures use Rustfire's VAPID key and the same account owner name. Generated CSRF tokens, versioned logo URLs, local origins, and product names are normalized. For the rejected and rate-limited pages only, the comparator removes one stray `</span>` emitted by Campfire's flash layout before parsing; browsers discard that malformed closing tag. Rustfire now emits the source's `<meta name="turbo-visit-control" content="reload">` on sign-in pages.

`python bench/paired_first_run.py` also passed its account, administrator, room, avatar, and repeat-redirect checks. Its initial first-run page matched 144 parsed head and 257 body tokens after aligning the disposable VAPID key. The probe now also checks that an omitted `user` group and flat fields return an empty 400 and leave the account uncreated. With `--edge-inputs`, both apps created the administrator with a blank name and an `invalid-email` address; the initial page still matched 144 head and 257 body tokens. Rustfire previously rejected that submission. These checks cover selected input states, not every malformed form or raw HTML bytes.

`python bench/paired_partial_signup.py` then passed five omitted-field shapes for both first-run setup and invitation signup. It compared each HTTP status, redirect, complete response body, and saved account, user, room, membership, session, and password-digest state. With only a name, with no email, or with no password, both apps created a user; an empty password produced a null digest in both. With no name, first-run setup persisted the account but no user and returned the same 4,887-byte 500 page, while invitation signup kept its existing account and returned that page. All three explicitly blank fields created a user with a null digest. These source behaviors may leave an account without a usable administrator; the probe verifies parity for these inputs rather than treating them as successful setup.

`python bench/paired_logout_browser.py` also passed after the metadata change: the browser sign-out flow unsubscribed the current device and removed only its matching push endpoint. These are sampled public-page states and one Chromium interaction; other first-run states, browser profiles, raw HTML bytes, and public-page throughput remain unverified.

## Complete application write-route rejection inventory

On this release build, `python bench/paired_unsafe_route_inventory.py --all-accepts --compare-error-bodies` and its `--role anonymous` repeat expanded all 80 non-Rails POST, PATCH, PUT, and DELETE method/path pairs in pinned Campfire `91d294f` across HTML, JSON, Turbo Stream, and wildcard Accept headers. Each role matched **320/320** response statuses, media types, redirect paths, and applicable packaged error-body hashes. The signed-in run initially matched 54/80 HTML cases; the anonymous run initially matched 276/320 all-Accept cases. On release commit `808f35e`, a `--role member` repeat also matched **320/320**. This gate checks invalid-CSRF and unauthenticated rejection order on disposable fixtures, not successful writes or side effects.

## Profile form method overrides

`python bench/paired_profile_method_overrides.py` compared fourteen valid and invalid CSRF POSTs to the profile route in both apps. The initial Rustfire build saved a name on plain and DELETE-override multipart POSTs that Campfire rejected. After matching Rack's multipart and URL-encoded method selection, including last duplicate-field precedence and the HTTP override header fallback, all **14/14** response and saved-row checks passed. The existing profile workflow, avatar upload, two-way device transfer, nine profile update edges, and both 320-case write-route rejection sweeps also passed. This checks small form bodies on disposable accounts, not the throughput or maximum size of profile uploads.

## Valid-CSRF empty-form write inventory

`python bench/paired_valid_write_route_inventory.py --all-accepts` generated all 80 non-Rails write method/path pairs from pinned Campfire `91d294f` and submitted an authenticated, valid-CSRF empty form to each app on fresh disposable databases with HTML, JSON, Turbo Stream, and wildcard Accept headers. The initial release build matched **52/80** HTML cases; an intermediate build matched **60/80**. After aligning missing-parameter handling, missing-room responses, bot API errors, private-IP ban errors, namespaced room deletion, and null involvement updates, the complete four-format administrator sweep matched **320/320** response statuses, media types, redirect paths, applicable packaged error-body hashes, and common table-count deltas. The member sweep initially differed on the media type of 22 forbidden responses; after adding Campfire's HTML content type, `--role member --all-accepts` matched **320/320**. The runner also checks saved involvement values and follow-up profile/involvement responses; a targeted member repeat with the member ID in that check matched **4/4**. A separate 16-case message-form probe checks malformed and nested parameters, saved message text, client IDs, and table deltas; it matched **16/16**. The Rust unit suite passed **52/52**. This diagnostic uses empty forms and selected message forms; it does not establish parity for meaningful values on every route, all row contents, background jobs, or complete application behavior.

## Room-name parameter and closed-room format edges

`python bench/paired_room_name_edges.py` found and then checked sixteen populated form shapes against pinned Campfire, each with HTML, JSON, Turbo Stream, and wildcard Accept headers. The final release build matched **64/64** response statuses, media types, redirects, applicable packaged error-body hashes, common table-count deltas, saved room name/type, membership IDs, room timestamp-change flags, and follow-up room GET status and media type. Campfire accepts blank and whitespace room names, saves `NULL` when creation omits the permitted name, and keeps the prior name when an update omits it. An update with no permitted attribute changes leaves `updated_at` untouched. Scalar `room=...` and array `room[]=...` values produce packaged 500 responses without saving. Successful closed-room creates and updates save their rows but return a packaged 500 for JSON and Turbo Stream requests; HTML and wildcard requests redirect. Rustfire now matches these observed outcomes. The room-sidebar workflow, 33-case missing-record write probe, and smoke suite passed afterward. The bot-admin workflow also found that Rustfire's index returned 500 with a bot in a null-named open room; after treating the saved name as an empty display string, both apps returned a matching parsed bot-index head (**146 tokens**) and body (**179 tokens**) in that state. Other room form combinations, full follow-up page content, and background effects remain outside this matrix.

## Private-room membership form values

`python bench/paired_room_membership_edges.py` compared 23 closed-room creation and update bodies against pinned Campfire `91d294f`, each under HTML, JSON, Turbo Stream, and wildcard Accept headers. The release build matched **92/92** cases in response status, media type, redirect or applicable error-body hash, common table-count deltas, saved room name/type and membership IDs, and follow-up room GET status and media type. Campfire casts numeric prefixes in submitted IDs: `2foo`, padded `2`, and `2.9` all select user 2. On an update, an explicitly submitted list with no valid IDs preserves existing members, while an omitted list or a numeric ID for a nonexistent user removes them. Rustfire now follows those sampled outcomes. This does not compare every possible form shape, membership notification, or full follow-up page body.

## Room form/query precedence and direct-room participant shapes

`python bench/paired_direct_form_edges.py` compared 27 participant input shapes for direct-room creation against pinned Campfire `91d294f` across HTML, JSON, Turbo Stream, and wildcard Accept headers. The release build matched **108/108** cases in response status, media type, redirect or applicable error-body hash, common table-count deltas, saved room type and membership IDs, and follow-up room GET status and media type. Campfire returns a packaged 500 before creating a room when `user_ids` is a scalar or a nested hash. Array values create or reuse a direct room, casting numeric prefixes in IDs. If the URL and body both provide `user_ids`, the URL value takes precedence; the earlier Rustfire build combined them. Rustfire now matches these sampled outcomes.

`python bench/paired_room_query_edges.py` checked eight open/private room create/update URL and form combinations under the same four Accept headers. The release build matched **32/32** responses, common table-count deltas, saved room names and memberships, update timestamp-change flags, and follow-up room GET status and media type. Campfire merges query parameters over form parameters by top-level group: a `room[...]` query group replaces the body's `room` group, while a `user_ids[...]` query group replaces its body counterpart. Rustfire now follows that rule for these namespaced room writes. Both probes use disposable fixtures; they do not cover every parameter shape, complete page body, or background notification.

## Message form/query precedence

`python bench/paired_message_parameter_edges.py` now passes **27/27** message-create forms against pinned Campfire `91d294f`: the earlier sixteen body shapes, six URL-encoded query/body combinations, and five multipart query/body combinations. The comparator checks status, media type, redirect or applicable error-body hash, message count, saved plain text and client ID, and whether a file remained attached. Campfire's query `message` group replaces the entire form `message` group, including an uploaded file; Rustfire now does the same in these cases. The source returns a packaged 500 for a query scalar `message=scalar`, while a query group with an unknown nested field creates a blank message.

With `--edit-query-only`, the same probe passed **6/6** message-edit query/body cases on seeded messages. It checks response and saved text, client ID, attachment presence, and whether `updated_at` changed. A query group with only an unknown field is a successful no-op, while a submitted `client_message_id` alone updates that ID and advances the timestamp. Rustfire now matches those sampled effects. The existing five-case public edit format probe, Rust unit suite (**52/52**), and smoke suite passed afterward. These are sampled message writes; other multipart, attachment-edit, and WebSocket effects remain parity work.

## Current-build paired rich-message mix after query parsing

Release commit `40cd030` ran `bench/paired_message_multi.py` with four rooms, four users, 32 checked readers, three rich posts per second per room for ten seconds, and 100 browser-channel sockets per room spread across the users. Campfire used 22 Puma workers and Redis; Rustfire used one process. Both apps ran serially on the same host in each server order. The [full paired reports](results/message-query-current-build-mix-2026-09-27.json) preserve the command, resource samples, deliveries, and validation results.

| Server order | Checked reads/s, Rustfire / Campfire | Read p95 ms, Rustfire / Campfire | Write p95 ms, Rustfire / Campfire |
| --- | ---: | ---: | ---: |
| Campfire first | 2,291.5 / 1,373.3 (**1.67×**) | 25.0 / 55.2 | 25.1 / 153.2 |
| Rustfire first | 2,753.4 / 1,406.2 (**1.96×**) | 17.6 / 52.0 | 17.6 / 154.8 |

Each order saved all 120 scheduled writes within the deadline, delivered all 12,000 message appends and 12,000 unread events per app, matched 480 captured append structures, and had zero checked read errors or paired validation errors. This is a short local workload point with no attachment uploads or push delivery. It supports a speed advantage for this equivalent tested mix on the current build; it does not establish a whole-app or sustained maximum-capacity advantage.

## Multipart message edits and attachment replacement

The pinned Campfire source accepts multipart PATCH edits with a body, a replacement file, or only a replacement file. Rustfire previously returned 415 because its edit route extracted only URL-encoded forms. `python bench/paired_message_parameter_edges.py --edit-multipart-only` now matches **6/6** paired cases on messages that start with a text attachment, including replacement by JPEG and QuickTime originals. A query `message` group suppresses the multipart file in both apps. The probe compares status, media type, redirect, saved text, attachment name and MIME type, SHA-256 of the original file bytes, and whether `updated_at` changed. The later queued-job probe checks when the superseded file is removed.

The same probe with `--edit-multipart-post-only` matches **4/4** POST forms carrying `_method=patch` or `_method=put`, including file-only replacement, query suppression, and JPEG replacement. `--edit-post-only` matches **3/3** URL-encoded method overrides. The 27-case message-create probe, six-case edit query probe, five-case public edit format probe, 52 Rust unit tests, smoke suite, and paired Chromium edit/form workflow passed after the streaming parser change. These checks compare saved original media bytes; replacement previews, large edited uploads, body-token-only multipart CSRF, and full socket-side edit effects remain to be compared.

The follow-up release build matches **7/7** direct multipart edits and **5/5** POST multipart overrides after adding a malformed JPEG replacement to each group. Campfire accepts the edit even though the image cannot be processed; Rustfire now does too. Fetching its signed preview returns the same packaged 500 page instead of Rustfire's former empty 404. The valid JPEG preview and QuickTime poster match Campfire's response type and SHA-256 after replacement, including a JPEG sent through a POST override. `cargo test --quiet` passed 52 tests, `python tests/smoke.py` passed, and Python syntax and diff checks passed. Other media formats, very large edited files, socket-side replacement effects, and old-file cleanup timing remain unverified.

## Thirty-second JPEG uploads with full browser-channel subscriptions

The paired image-mix driver now supports `--browser-channels`, which makes every socket subscribe to the page's room, presence, read, unread, typing, heartbeat, and signed sidebar channels. A 10-second setup check at 2 uploads/s, 8 readers, and 20 sockets passed all 20 writes, 400 appends, 400 unread events, and 210 presence read events per app. The checked 30-second trial then used `--clients 64 --seconds 30 --write-rate 7 --sockets 100 --browser-channels --campfire-workers 22` in both server orders on the same host. Each app saved all 210 byte-matching 505,420-byte JPEGs, matched the final parsed page and all 210 first-socket append structures, delivered all 21,000 appends, 21,000 unread events, and 5,050 presence read events, and had zero checked read errors, missing events, unexpected events, or early socket closes.

| Server order | Rustfire writer finish / upload p95 | Campfire writer finish / upload p95 | Checked reads/s, Rustfire / Campfire |
| --- | ---: | ---: | ---: |
| Campfire first | 29.44 s / 92.17 ms | 36.78 s / 371.80 ms | 2,446 / 1,340 (**1.83×**) |
| Rustfire first | 29.42 s / 93.53 ms | 32.11 s / 280.77 ms | 2,416 / 1,367 (**1.77×**) |

Rustfire met the 30-second writer deadline in both orders. Campfire completed every write and delivery but missed that deadline by 2.11–6.78 seconds, so the strict paired commands exited nonzero after saving checked [Campfire-first](results/image-mix-7ups-30s-browser-camp-first.json) and [Rustfire-first](results/image-mix-7ups-30s-browser-rust-first.json) reports. This shows higher sampled capacity for one account uploading to one room with concurrent page reads and browser-channel sockets. It does not establish the maximum supported upload rate, hours-long stability, multiuser upload capacity, or whole-app feature parity.

## Live replacement events after message edits

The new `bench/capture_message_replace.mjs` subscribes to each app's signed `RoomMessagesChannel` before an edit and captures its presentation replacement. Adding `--edit-stream` to the paired message-parameter probe exposed a media-specific mismatch: Campfire's immediate JPEG edit event had no dimensions because it queued Active Storage analysis, while Rustfire's synchronous analyzer had already supplied width, height, and a container aspect ratio. Rustfire now defers edit media processing until the signed representation is requested. Its valid JPEG and QuickTime previews still match Campfire's response type and SHA-256, and the malformed JPEG edit and representation failure still match.

With the release build, complete parsed Turbo replacement events matched after signed blob URL normalization for **7/7 direct multipart**, **5/5 POST multipart**, **6/6 query**, and **3/3 URL-encoded POST** edit cases. The scalar query case returns the same 500 response in both apps and does not emit an edit event. The stream check includes the `replace` action, presentation target, `maintain_scroll`, element order, attributes, and text. This covers one signed room subscriber per edit, not fanout under load; later media metadata updates after Campfire's queued analysis, old-file purge timing, and larger edited uploads remain to be checked.

## Queued attachment analysis and purge after message edits

Campfire's multipart edit enqueues `ActiveStorage::PurgeJob` for the old blob and `ActiveStorage::AnalyzeJob` for the new one. An isolated source probe observed both old and new blob records immediately after a JPEG edit, with the old file still on disk and the new metadata containing only `identified`. After a Resque worker ran, only the new blob remained, its metadata contained `640×640` dimensions and `analyzed`, and the old file was gone. Rustfire previously deleted the old file during the request and left new dimensions unset indefinitely.

Rustfire now stores a replaced attachment record and durable SQLite jobs, preserving the old signed file link until a background purge. An analyzer job fills dimensions without generating the preview. `python bench/paired_message_edit_jobs.py` matched the source for text, JPEG, malformed JPEG, and QuickTime replacements: the old signed file served the original bytes before the jobs, returned 404 after them, and its local file was removed; the new media dimensions and final parsed message presentation matched. The JPEG case replaced the attachment again after the first purge; both apps assigned ID 3, retired the second old signed link after the next purge, and rendered the final text attachment equally. The paired 7/7 direct and 5/5 POST multipart stream checks, 52 Rust unit tests, smoke suite, paired MIME and direct-upload probes, and 200-socket banned-content job probe passed after the change. `python bench/attachment_jobs_restart.py` also passed: pending image analysis and an expired claimed purge resumed after stopping and restarting Rustfire. When the old file path was a directory, purge failed and retained its job and archive; after restoring the file and expiring the claim, the retry removed both. The paired edit probe, 52 Rust unit tests, and smoke suite passed again after adding deletion-error propagation. Analyzer failure modes, crashes during filesystem work, and other media formats remain to be checked.

## Large message attachment edits

A 129 MiB plus one byte multipart text-file replacement exposed Rustfire's 128 MiB default body limit on message edit routes: Campfire returned 302 and saved the original bytes, while Rustfire returned 400 and retained the old attachment. Rustfire now streams those edit bodies under the route without that limit. Its CSRF middleware also lets the edit handler validate a form token as the attachment streams, matching the existing message-create path.

`python bench/paired_message_parameter_edges.py --edit-large` and `--edit-large --large-post` now match the pinned source in response, saved attachment name/type/SHA-256, unchanged message text, and updated timestamp. Both forms also match with `--form-csrf-only`, without an `X-CSRF-Token` header. With `--invalid-form-csrf`, both match Campfire's rejection and retain the old attachment and timestamp. The direct PATCH case matches the parsed replacement stream with `--edit-stream`. The 7/7 ordinary multipart PATCH and 5/5 POST edit-stream probes, 52 Rust unit tests, and smoke suite passed after the change. These are single-upload fixtures on the same host; they do not establish concurrent large-upload capacity, larger file limits, or sustained performance.

## Concurrent attachment replacements and purge

`python bench/paired_concurrent_message_edits.py` seeded a separate text attachment for every client on disposable, aligned Campfire and Rustfire accounts. Each client sent a multipart PATCH for a different message at the same start signal. Campfire used 22 Puma workers plus one live Resque worker; Rustfire used one release process and its background job loop. The probe checked every 302 response, saved original-file SHA-256, unique new blob IDs, removal of every old file after the purge queues drained, and complete parsed message presentation after signed URL normalization. All cases passed in both server orders. The response burst excludes setup and later purge; the lifecycle column includes the time until all old files were removed.

| Workload | Order | Rustfire response burst | Campfire response burst | Rustfire edit + purge | Campfire edit + purge |
| --- | --- | ---: | ---: | ---: | ---: |
| 4 × 8 MiB | Campfire first | 0.009 s | 0.316 s | 0.824 s | 1.075 s |
| 4 × 8 MiB | Rustfire first | 0.010 s | 0.246 s | 0.822 s | 1.053 s |
| 16 × 8 MiB | Campfire first | 0.035 s | 0.620 s | 0.904 s | 3.402 s |
| 16 × 8 MiB | Rustfire first | 0.045 s | 0.589 s | 0.910 s | 3.266 s |
| 4 × 129 MiB | Campfire first | 0.078 s | 0.794 s | 0.691 s | 1.655 s |
| 4 × 129 MiB | Rustfire first | 0.056 s | 0.811 s | 0.721 s | 1.621 s |

The [16-client Campfire-first](results/concurrent-message-edits-16x8mib-camp-first.json), [16-client Rustfire-first](results/concurrent-message-edits-16x8mib-rust-first.json), [large-file Campfire-first](results/concurrent-message-edits-4x129mib-camp-first.json), and [large-file Rustfire-first](results/concurrent-message-edits-4x129mib-rust-first.json) reports retain response rates, median/p95 latency, and purge-drain times. The 4-client 8 MiB reports are saved alongside them. These are short, same-host, memory-cached bursts without WebSocket subscribers, mixed traffic, or resource sampling. They show an advantage for the tested concurrent edit lifecycle, not a sustained throughput or whole-app capacity limit.

## Empty and scalar attachment fields

The paired message-form probe exposed attachment parameter gaps on both create and edit. Campfire saves a named zero-byte file; Rustfire previously ignored it. A multipart file field with an empty filename returns 400 with the same complete response body before mutation, while a nonblank scalar `message[attachment]` returns Campfire's packaged 500. A blank scalar attachment field creates a message without a file, or clears an existing file on edit. Rustfire previously retained the old file on that edit.

Rustfire now distinguishes file fields from scalar fields and retains named zero-byte originals. Clearing an attachment archives its signed record and queues a background purge; the old signed link serves its original bytes before the worker and returns 404 afterward, matching a paired Campfire Resque worker run. A file-only message also drops its old filename from search in both apps. After that purge, both apps assign ID 2 to the next uploaded attachment; Rustfire now preserves blob ID high-water marks across deletion and import. `python bench/paired_message_parameter_edges.py` matches **35/35** creation shapes; `--edit-query-only`, `--edit-multipart-only --edit-stream`, and `--edit-multipart-post-only --edit-stream` match **8/8**, **15/15**, and **7/7** cases. The create comparator now checks original filename, MIME type, and SHA-256 when a file is saved. Two query-field cases and two multipart query-over-body cases also match Campfire’s attachment parameter precedence. `python bench/paired_message_edit_jobs.py` passes text, JPEG, QuickTime, malformed JPEG, and blank-field removal lifecycles. The 52 Rust unit tests and paired import roundtrip pass. Four duplicate multipart-field cases also match Campfire’s last-value precedence, including file then blank, blank then file, two files, and an empty filename followed by a valid file. Other attachment-clearing combinations remain unverified.

## Reconnect timestamp and import ID edges

`python bench/paired_import_roundtrip.py` now seeds and deletes a Campfire blob above the highest live ID before migration. The imported Rustfire sequence retains that source high-water mark while the sampled avatar, attachment, bot, and room presentation checks still pass.

`python bench/paired_room_refresh.py --iterations 30 --since-edges` matches **16/16** timestamp query shapes against pinned Campfire, including numeric prefixes, surrounding spaces, oversized signed values, duplicate parameters, and scalar/array ordering. The check compares response status, media type, selected message IDs and Turbo actions, and the complete error-body hash. The latest single-host, sequential warm-cache sample measured **0.218 / 0.368 ms** median / p95 for Rustfire versus **6.760 / 15.310 ms** for Campfire, with **17,146 / 20,506** response bytes. The parsed two-message replacement content matched after generated-value normalization; raw body sizes differ. This is a reconnect-route sample, not a sustained throughput or whole-app performance claim.

## Member write routes and profile parameter groups

`python bench/paired_valid_write_route_inventory.py --role member` matched **80/80** valid-CSRF, empty-form method/path pairs. A second sweep with `--body 'foo=bar'` initially found two profile-update mismatches: Rustfire redirected when Campfire returned an empty HTTP 400 for a request without a `user` parameter group. The remaining generated-message redirect differed because the disposable source database had an older message ID sequence; aligning that sequence in the Rustfire fixture made the generated path comparable. After the profile fix, the full populated sweep matched **80/80** response statuses, media types, redirects, applicable error hashes, table-count deltas, and the runner's route-specific follow-up checks.

`python bench/paired_profile_update_edges.py` now matches **14/14** sequential cases. Added inputs cover a method-only form, an unknown top-level field, an unknown nested user field, and scalar and array `user` values. The comparator now also checks whether `updated_at` changed. Campfire leaves it unchanged for a blank password or an unknown nested field, returns an empty 400 when the required user group is missing, and returns its packaged 500 for scalar/array user values; Rustfire now follows those outcomes. The paired 19-case multipart method-override workflow, including an unscoped avatar file, and the profile page/transfer/normal-mutation workflow passed afterward. These selected forms do not establish profile parity for every parameter ordering, avatar file type, or authorization state.

## Profile query precedence and avatar scalar fields

`python bench/paired_profile_update_edges.py` now matches **23/23** sequential cases after adding URL query/form combinations and scalar avatar fields. On the sampled requests, Campfire's URL `user` group replaces the form's `user` group, including when the query group has only an unknown nested field. A scalar query `user` produces the same packaged 500 as a scalar form group; a nonblank scalar `user[avatar]` also produces that 500 before mutation, while a blank value is a no-op. Rustfire now follows those response and saved-profile outcomes. `python bench/paired_profile_method_overrides.py` matches **21/21** cases, including two multipart avatar uploads suppressed by query user groups and checks that the avatar record did not change. The profile page/transfer/normal-mutation workflow passed with 32 sample reads per concurrency point. These are profile compatibility checks; the brief read timings do not establish sustained capacity.

## Account update query precedence and timestamp changes

`python bench/paired_account_update_edges.py` now matches **30/30** sequential account-form cases. It checks response status, redirect, applicable packaged error-body hash, saved name, effective room-creation restriction, and whether `updated_at` changed. The added cases show that a URL `account` group replaces the body group, including a multipart logo. Campfire leaves the timestamp unchanged when the submitted setting already has the saved effective value, returns its packaged 500 for an unknown nested setting or nonblank scalar `account[logo]`, and treats a blank scalar logo as a no-op. Rustfire now matches these sampled outcomes. The paired account-administration workflow and account-logo workflow passed afterward; the latter checks that a query-suppressed multipart JPEG leaves both the existing logo identity and account timestamp unchanged. This is account-form parity on a disposable fixture, not complete administration or sustained-performance evidence.

The account-format regression check initially found that Rustfire's empty JSON PATCH/PUT response lacked Campfire's `{"status":400,"error":"Bad Request"}` body. After correcting the shared Rails-style 400 serializer, `python bench/paired_valid_write_route_inventory.py --all-accepts --filter '^(POST|PATCH|PUT) /account(\.1)?$'` matched **12/12** account method/format cases. The full `--accept application/json` empty-form sweep matched **80/80** non-Rails write routes. The Rust unit suite passed **52/52**. These checks compare response shape and selected table effects, not every successful JSON body.
