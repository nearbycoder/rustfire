"""Compare a large imported four-room history, search, and paired read rates.

Requires the pinned Campfire checkout, bundled Ruby, Redis, Go, and a release
Rustfire build. The source database is copied into a disposable fixture.
"""

import argparse
import base64
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import time
import urllib.parse

from direct_lookup import free_port, start_server, stop_server
from paired_banned_content import start_redis
from paired_direct_lookup import login_campfire, seed_campfire, wait_for_server
from paired_large_import import MESSAGE_IDS, SOURCE_REVISION, get, pages
from paired_room_shell import assert_equal, section


ROOMS = (1, 2, 3, 4)
ACCESS = ((1, (1, 2, 3)), (2, ROOMS))
STAMP = "2026-01-01 00:00:00.000000"
SEARCH_QUERIES = ("Imported", "room 4")


def seed_large_graph(database, messages_per_room):
    with sqlite3.connect(database) as db:
        db.execute("UPDATE users SET email_address='member@example.invalid',password_digest=(SELECT password_digest FROM users WHERE id=1) WHERE id=2")
        db.executemany("INSERT INTO rooms(id,name,type,creator_id,created_at,updated_at) VALUES(?,?,?,?,?,?)", (
            (2, "Private History", "Rooms::Closed", 1, STAMP, STAMP),
            (3, None, "Rooms::Direct", 1, STAMP, STAMP),
            (4, "Member Archive", "Rooms::Closed", 2, STAMP, STAMP),
        ))
        db.executemany("INSERT INTO memberships(room_id,user_id,involvement,created_at,updated_at) VALUES(?,?,'everything',?,?)", (
            (room_id, user_id, STAMP, STAMP)
            for room_id in (2, 3, 4)
            for user_id in ((1, 2) if room_id != 4 else (2,))
        ))
        messages, rich_texts, search_rows = [], [], []
        base = datetime.fromisoformat(STAMP)
        for offset in range(messages_per_room):
            for room_id in ROOMS:
                message_id = offset * len(ROOMS) + room_id
                stamp = (base + timedelta(milliseconds=message_id)).strftime("%Y-%m-%d %H:%M:%S.%f")
                creator_id = 2 if room_id == 4 or offset % 2 else 1
                plain = f"Imported room {room_id} message {offset + 1:05d}"
                body = f"<div>{plain} <strong>bold</strong></div>" if offset % 10 == 0 else f"<div>{plain}</div>"
                messages.append((message_id, room_id, creator_id, f"large-graph-{message_id}", stamp, stamp))
                rich_texts.append((body, message_id, stamp, stamp))
                search_rows.append((message_id, plain + (" bold" if offset % 10 == 0 else "")))
        db.executemany("INSERT INTO messages(id,room_id,creator_id,client_message_id,created_at,updated_at) VALUES(?,?,?,?,?,?)", messages)
        db.executemany("INSERT INTO action_text_rich_texts(name,body,record_type,record_id,created_at,updated_at) VALUES('body',?,'Message',?,?,?)", rich_texts)
        db.executemany("INSERT INTO message_search_index(rowid,body) VALUES(?,?)", search_rows)
        db.execute("INSERT INTO push_subscriptions(id,user_id,endpoint,p256dh_key,auth_key,user_agent,created_at,updated_at) VALUES(1,1,'https://push.example.test/large-graph','test-p256dh','test-auth','test',?,?)", (STAMP, STAMP))


def fixture_vapid_keys():
    generator_x = bytes.fromhex("6b17d1f2e12c4247f8bce6e563a440f277037d812deb33a0f4a13945d898c296")
    generator_y = bytes.fromhex("4fe342e2fe1a7f9b8ee7eb4a7c0f9e162bce33576b315ececbb6406837bf51f5")
    public = base64.urlsafe_b64encode(b"\x04" + generator_x + generator_y).rstrip(b"=").decode()
    private = base64.urlsafe_b64encode(bytes(31) + b"\x01").rstrip(b"=").decode()
    return public, private


def capture_histories(port, cookies, messages_per_room, label):
    histories = {}
    for user_id, rooms in ACCESS:
        for room_id in rooms:
            name = f"{label} user {user_id} room {room_id}"
            histories[(user_id, room_id)] = list(pages(port, cookies[user_id], messages_per_room, name, room_id))
    return histories


def expected_search_ids(user_id, query, messages_per_room):
    if query == "room 4":
        return [] if user_id == 1 else list(range(4 * max(1, messages_per_room - 99), 4 * messages_per_room + 1, 4))
    reachable = [message_id for message_id in range(1, 4 * messages_per_room + 1)
        if user_id == 2 or message_id % 4 != 0]
    return reachable[-100:]


def capture_searches(port, cookies, messages_per_room, label):
    searches = {}
    for user_id in cookies:
        for query in SEARCH_QUERIES:
            path = "/searches?" + urllib.parse.urlencode({"q": query})
            status, _, body = get(port, cookies[user_id], path)
            assert status == 200, (label, user_id, query, status, body[:200])
            ids = [int(value) for value in MESSAGE_IDS.findall(body)]
            expected = expected_search_ids(user_id, query, messages_per_room)
            assert ids == expected, (label, user_id, query, ids[:10], ids[-10:], expected[:10], expected[-10:])
            searches[(user_id, query)] = (path, body)
            print(f"{label} user {user_id} search {query!r}: {len(ids)} visible messages", flush=True)
    return searches


def compare_searches(source, target):
    for key, (_, source_body) in source.items():
        _, target_body = target[key]
        options = dict(normalize_times=True, normalize_avatar_paths=True,
            normalize_blob_paths=True, normalize_text_origins=True, ignore_csrf_inputs=True)
        for part in ("head", "body"):
            assert_equal(f"imported user {key[0]} search {key[1]!r} {part}",
                section(source_body, part, **options), section(target_body, part, **options))


def csrf_count(body):
    return body.count(b'name="authenticity_token"') + body.count(b"name='authenticity_token'")


def checked_search_targets(source, target, cookies):
    targets = []
    for (user_id, query), (path, body) in source.items():
        ids = [int(value) for value in MESSAGE_IDS.findall(body)]
        if not ids:
            continue
        target_body = target[(user_id, query)][1]
        assert len(ids) == len(MESSAGE_IDS.findall(target_body))
        assert csrf_count(body) == csrf_count(target_body) > 0
        message_id = ids[-1]
        room_id = (message_id - 1) % len(ROOMS) + 1
        number = (message_id - 1) // len(ROOMS) + 1
        marker = f"Imported room {room_id} message {number:05d}"
        assert marker.encode() in body and marker.encode() in target_body
        targets.append({"path": path, "cookie": cookies[user_id],
            "expected_message_count": len(ids), "expected_csrf_count": csrf_count(body),
            "expected_contains": marker})
    return targets


def run_checked_reads(binary, port, targets_path, clients, seconds):
    command = (str(binary), "--base", f"http://127.0.0.1:{port}", "--targets-file", str(targets_path),
        "--accept", "text/html", "--expected-content-type", "text/html", "--expected-status", "200",
        "--clients", str(clients), "--seconds", str(seconds))
    result = subprocess.run(command, check=True, capture_output=True, text=True)
    report = json.loads(result.stdout)
    assert report["errors"] == 0 and report["successes"] > 0, report
    return report


def measure_reads(temp, repository, ruby, environment, target_db, uploads,
        camp_port, rust_port, targets, clients_list, seconds, campfire_workers, label="messages"):
    binary = temp / "checked_get"
    subprocess.run(("go", "build", "-o", str(binary), "bench/checked_get.go"), check=True)
    targets_path = temp / f"{label}-targets.json"
    targets_path.write_text(json.dumps(targets))
    trials = []
    for clients in clients_list:
        for order in (("Campfire", "Rustfire"), ("Rustfire", "Campfire")):
            for name in order:
                if name == "Campfire":
                    trial_env = dict(environment, WEB_CONCURRENCY=str(campfire_workers),
                        PIDFILE=str(temp / "benchmark-puma.pid"))
                    with (temp / "benchmark-puma.log").open("w+") as log:
                        process = subprocess.Popen((str(ruby), str(ruby.parent / "bundle"), "exec", "puma", "-C", "config/puma.rb"),
                            cwd=repository, env=trial_env, stdout=log, stderr=log)
                        try:
                            wait_for_server(camp_port, process)
                            report = run_checked_reads(binary, camp_port, targets_path, clients, seconds)
                        except Exception:
                            log.flush()
                            log.seek(0)
                            print(log.read()[-3000:])
                            raise
                        finally:
                            stop_server(process)
                else:
                    process = start_server(target_db, rust_port, {"RUSTFIRE_UPLOAD_DIR": str(uploads),
                        "RUSTFIRE_CAMPFIRE_SECRET_KEY_BASE": environment["SECRET_KEY_BASE"]})
                    try:
                        report = run_checked_reads(binary, rust_port, targets_path, clients, seconds)
                    finally:
                        stop_server(process)
                trials.append({"clients": clients, "order": list(order), "server": name, "result": report})
                print(f"{label} {clients} clients {name}: {report['rps']:.0f}/s p95 {report['p95_ms']:.2f}ms errors {report['errors']}", flush=True)
    return trials


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--campfire-repo", type=Path, default=Path("/tmp/once-campfire-reference"))
    parser.add_argument("--ruby", type=Path, default=Path("/tmp/rustfire-baseline/local/bin/ruby"))
    parser.add_argument("--bundle-path", type=Path, default=Path("/tmp/rustfire-baseline/bundle"))
    parser.add_argument("--messages-per-room", type=int, default=1_000)
    parser.add_argument("--read-clients", type=int, nargs="*", default=[])
    parser.add_argument("--search-clients", type=int, nargs="*", default=[])
    parser.add_argument("--read-seconds", type=float, default=5.0)
    parser.add_argument("--campfire-workers", type=int, default=22)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--search-report", type=Path)
    args = parser.parse_args()
    if args.messages_per_room < 41 or args.read_seconds <= 0 or args.campfire_workers < 1 or any(count < 1 for count in (*args.read_clients, *args.search_clients)):
        parser.error("messages per room must be at least 41; seconds, workers, and clients must be positive")
    if args.report and not args.read_clients:
        parser.error("--report requires --read-clients")
    if args.search_report and not args.search_clients:
        parser.error("--search-report requires --search-clients")
    repository, ruby, bundle_path = args.campfire_repo.resolve(), args.ruby.resolve(), args.bundle_path.resolve()
    revision = subprocess.check_output(("git", "rev-parse", "HEAD"), cwd=repository, text=True).strip()
    assert revision == SOURCE_REVISION, revision
    with tempfile.TemporaryDirectory(prefix="paired-large-graph-import-") as directory:
        temp = Path(directory)
        source_db, target_db, uploads = temp / "campfire.sqlite3", temp / "rustfire.sqlite3", temp / "uploads"
        camp_port, rust_port, redis_port = free_port(), free_port(), free_port()
        environment = seed_campfire(repository, ruby, bundle_path,
            repository / "storage/db/production.sqlite3", source_db, [], camp_port, temp)
        seed_large_graph(source_db, args.messages_per_room)
        vapid_public, vapid_private = fixture_vapid_keys()
        environment["VAPID_PUBLIC_KEY"], environment["VAPID_PRIVATE_KEY"] = vapid_public, vapid_private
        environment["REDIS_URL"] = f"redis://127.0.0.1:{redis_port}"
        redis, redis_log = start_redis(temp, redis_port)
        try:
            with (temp / "puma.log").open("w+") as log:
                camp = subprocess.Popen((str(ruby), str(ruby.parent / "bundle"), "exec", "puma", "-C", "config/puma.rb"),
                    cwd=repository, env=environment, stdout=log, stderr=log)
                try:
                    wait_for_server(camp_port, camp)
                    admin_cookie, _ = login_campfire(camp_port)
                    member_cookie, _ = login_campfire(camp_port, "member@example.invalid")
                    cookies = {1: admin_cookie, 2: member_cookie}
                    source_histories = capture_histories(camp_port, cookies, args.messages_per_room, "Campfire")
                    source_searches = capture_searches(camp_port, cookies, args.messages_per_room, "Campfire")
                except Exception:
                    log.flush()
                    log.seek(0)
                    print(log.read()[-3000:])
                    raise
                finally:
                    stop_server(camp)
            started = time.monotonic()
            importer = subprocess.run(("python", "tools/import_campfire.py", "--source-db", str(source_db),
                "--source-files", str(repository / "storage/files"), "--target-db", str(target_db),
                "--target-uploads", str(uploads), "--rustfire-bin", "target/release/rustfire"),
                env=dict(os.environ, RUSTFIRE_CAMPFIRE_SECRET_KEY_BASE=environment["SECRET_KEY_BASE"],
                    RUSTFIRE_CAMPFIRE_VAPID_PUBLIC_KEY=vapid_public, RUSTFIRE_CAMPFIRE_VAPID_PRIVATE_KEY=vapid_private),
                check=True, text=True, capture_output=True)
            elapsed = time.monotonic() - started
            counts = json.loads(importer.stdout)
            total_messages = args.messages_per_room * len(ROOMS)
            assert counts["rooms"] == 4 and counts["messages"] == total_messages and counts["memberships"] == 7 and counts["push_subscriptions"] == 1, counts
            print(f"import: {total_messages} messages in {elapsed:.2f}s", flush=True)
            rust = start_server(target_db, rust_port, {"RUSTFIRE_UPLOAD_DIR": str(uploads),
                "RUSTFIRE_CAMPFIRE_SECRET_KEY_BASE": environment["SECRET_KEY_BASE"]})
            try:
                target_histories = capture_histories(rust_port, cookies, args.messages_per_room, "Rustfire")
                target_searches = capture_searches(rust_port, cookies, args.messages_per_room, "Rustfire")
            finally:
                stop_server(rust)
            compare_searches(source_searches, target_searches)
            search_targets = checked_search_targets(source_searches, target_searches, cookies)
            targets = []
            for (user_id, room_id), source_pages in source_histories.items():
                imported_pages = target_histories[(user_id, room_id)]
                assert source_pages == imported_pages, (user_id, room_id,
                    next(((number, source, target) for number, (source, target) in enumerate(zip(source_pages, imported_pages), 1)
                        if source != target), (len(source_pages), len(imported_pages))))
                latest = source_pages[0]
                assert len(latest[0]) == 40
                targets.append({"path": f"/rooms/{room_id}/messages", "cookie": cookies[user_id],
                    "expected_etag": latest[1], "expected_last_modified": latest[2],
                    "expected_message_count": 40, "expected_csrf_count": 320})
            checked_pages = sum(len(history) for history in source_histories.values())
            print(f"PASS {checked_pages} paired pages across four rooms and two users, with exact validators and parsed bodies", flush=True)
            if args.read_clients:
                trials = measure_reads(temp, repository, ruby, environment, target_db, uploads,
                    camp_port, rust_port, targets, args.read_clients, args.read_seconds, args.campfire_workers)
                if args.report:
                    args.report.parent.mkdir(parents=True, exist_ok=True)
                    args.report.write_text(json.dumps({"source_revision": revision,
                        "rustfire_revision": subprocess.check_output(("git", "rev-parse", "HEAD"), text=True).strip(),
                        "fixture": {"rooms": 4, "users": 2, "messages_per_room": args.messages_per_room,
                            "total_messages": total_messages, "targets": len(targets), "pages_checked": checked_pages},
                        "seconds": args.read_seconds, "campfire_workers": args.campfire_workers,
                        "trials": trials}, indent=2) + "\n")
            if args.search_clients:
                trials = measure_reads(temp, repository, ruby, environment, target_db, uploads,
                    camp_port, rust_port, search_targets, args.search_clients, args.read_seconds,
                    args.campfire_workers, label="search")
                if args.search_report:
                    args.search_report.parent.mkdir(parents=True, exist_ok=True)
                    args.search_report.write_text(json.dumps({"source_revision": revision,
                        "rustfire_revision": subprocess.check_output(("git", "rev-parse", "HEAD"), text=True).strip(),
                        "fixture": {"rooms": 4, "users": 2, "messages_per_room": args.messages_per_room,
                            "total_messages": total_messages, "search_targets": len(search_targets),
                            "search_result_counts": [item["expected_message_count"] for item in search_targets],
                            "search_queries": list(SEARCH_QUERIES)},
                        "seconds": args.read_seconds, "campfire_workers": args.campfire_workers,
                        "trials": trials}, indent=2) + "\n")
        finally:
            redis.terminate()
            redis.wait(timeout=10)
            redis_log.close()


if __name__ == "__main__":
    main()
