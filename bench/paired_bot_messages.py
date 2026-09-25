"""Compare the pinned Campfire and Rustfire bot message-list JSON on matched fixtures.

Run after cargo build --release. Both servers use one room, one bot, and the same
messages. The probe rejects semantic JSON differences after URL origin removal.
"""

import argparse
import concurrent.futures
from datetime import datetime, timedelta, timezone
import http.client
import json
import pathlib
import sqlite3
import statistics
import subprocess
import tempfile
import threading
import time
import urllib.parse

from direct_lookup import free_port, p95, start_server, stop_server
from paired_direct_lookup import seed_campfire, seed_rustfire, wait_for_server


BOT_KEY = "3-abcdefgh1234"
PATH = f"/rooms/1/{BOT_KEY}/messages"
STAMP = "2026-01-01T00:00:00Z"


def seed_messages(rust_database, camp_database, count, include_attachments, scramble_timestamps=False):
    moment = datetime(2026, 1, 1, 0, 0, 0, 123456, tzinfo=timezone.utc)
    cache_stamp = datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
    rust_rows = []
    camp_rows = []
    rich_rows = []
    rust_attachments = []
    camp_blobs = []
    camp_attachments = []
    for number in range(1, count + 1):
        instant = moment + timedelta(seconds=number)
        created = instant.isoformat().replace("+00:00", "Z")
        camp_created = instant.strftime("%Y-%m-%d %H:%M:%S.%f")
        content = f"message {number}"
        attached = include_attachments and number % 10 == 0
        markup = f"<div>{content}</div>" if number % 2 == 0 and not attached else None
        rust_rows.append((number, "" if attached else content, markup, f"fixture-{number}", created, int(instant.timestamp()) * 1_000_000_000 + instant.microsecond * 1000))
        camp_rows.append((number, f"fixture-{number}", camp_created, cache_stamp))
        if attached:
            filename = f"sample-{number}.txt"
            rust_attachments.append((number, filename, f"fixture-file-{number}", created))
            camp_blobs.append((number, f"fixture-key-{number}", filename, camp_created))
            camp_attachments.append((number, camp_created))
        else:
            rich_rows.append((number, markup or content, camp_created))

    with sqlite3.connect(rust_database) as db:
        db.execute("UPDATE users SET name='Test Admin',updated_at=? WHERE id=1", [cache_stamp])
        db.execute("UPDATE users SET role=2,bot_token=?,updated_at=? WHERE id=3", [BOT_KEY.split("-", 1)[1], cache_stamp])
        db.execute("INSERT INTO memberships(room_id,user_id,involvement,created_at) VALUES(1,3,'mentions',?)", [STAMP])
        db.executemany(
            "INSERT INTO messages(id,room_id,creator_id,body,body_html,client_message_id,created_at,created_at_ns,updated_at) VALUES(?1,1,1,?2,?3,?4,?5,?6,?5)",
            rust_rows,
        )
        db.executemany(
            "INSERT INTO attachments(message_id,filename,content_type,stored_name,created_at) VALUES(?1,?2,'text/plain',?3,?4)",
            rust_attachments,
        )
        if scramble_timestamps:
            db.execute("UPDATE messages SET created_at='2026-01-02T00:00:00.123456Z',created_at_ns=? WHERE id=1", [int(datetime(2026, 1, 2, tzinfo=timezone.utc).timestamp()) * 1_000_000_000 + 123456000])
            db.execute("UPDATE messages SET created_at='2025-12-31T00:00:00.123456Z',created_at_ns=? WHERE id=?", [int(datetime(2025, 12, 31, tzinfo=timezone.utc).timestamp()) * 1_000_000_000 + 123456000, count])
    with sqlite3.connect(camp_database) as db:
        db.execute("UPDATE users SET updated_at=? WHERE id=1", [cache_stamp])
        db.execute("UPDATE users SET role=2,bot_token='abcdefgh1234',updated_at=? WHERE id=3", [cache_stamp])
        db.execute("INSERT INTO memberships(room_id,user_id,involvement,created_at,updated_at) VALUES(1,3,'mentions',?,?)", [STAMP, STAMP])
        db.executemany(
            "INSERT INTO messages(id,room_id,creator_id,client_message_id,created_at,updated_at) VALUES(?1,1,1,?2,?3,?4)",
            camp_rows,
        )
        db.executemany(
            "INSERT INTO action_text_rich_texts(name,body,record_type,record_id,created_at,updated_at) VALUES('body',?2,'Message',?1,?3,?3)",
            rich_rows,
        )
        db.executemany(
            "INSERT INTO active_storage_blobs(id,key,filename,content_type,metadata,service_name,byte_size,created_at) VALUES(?1,?2,?3,'text/plain','{}','local',10,?4)",
            camp_blobs,
        )
        db.executemany(
            "INSERT INTO active_storage_attachments(name,record_type,record_id,blob_id,created_at) VALUES('attachment','Message',?1,?1,?2)",
            camp_attachments,
        )
        if scramble_timestamps:
            db.execute("UPDATE messages SET created_at='2026-01-02 00:00:00.123456' WHERE id=1")
            db.execute("UPDATE messages SET created_at='2025-12-31 00:00:00.123456' WHERE id=?", [count])


def request(connection):
    connection.request("GET", PATH, headers={"Accept": "application/json"})
    response = connection.getresponse()
    body = response.read()
    if response.status != 200:
        raise AssertionError((response.status, body[:300]))
    return body


def measure(port, iterations, expected_ids):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    samples = []
    expected_body = None
    try:
        for iteration in range(iterations + 2):
            started = time.perf_counter()
            body = request(connection)
            elapsed = (time.perf_counter() - started) * 1000
            if expected_body is None:
                expected_body = body
                ids = [message["id"] for message in json.loads(body)]
                if ids != expected_ids:
                    raise AssertionError((ids, expected_ids))
            elif body != expected_body:
                raise AssertionError("Message-list response changed during trial")
            if iteration >= 2:
                samples.append(elapsed)
    finally:
        connection.close()
    return statistics.median(samples), p95(samples), expected_body


def measure_concurrent(port, clients, duration, expected_body):
    ready = threading.Barrier(clients + 1, timeout=30)
    start = threading.Event()
    clock = {}

    def worker():
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
        samples = []
        errors = 0
        try:
            for _ in range(2):
                if request(connection) != expected_body:
                    raise AssertionError("Concurrent warmup response differs")
            ready.wait()
            start.wait()
            while time.perf_counter() < clock["deadline"]:
                started = time.perf_counter()
                try:
                    if request(connection) == expected_body:
                        samples.append((time.perf_counter() - started) * 1000)
                    else:
                        errors += 1
                except OSError:
                    errors += 1
                    connection.close()
                    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
        finally:
            connection.close()
        return samples, errors, time.perf_counter()

    with concurrent.futures.ThreadPoolExecutor(max_workers=clients) as executor:
        futures = [executor.submit(worker) for _ in range(clients)]
        ready.wait()
        clock["started"] = time.perf_counter()
        clock["deadline"] = clock["started"] + duration
        start.set()
        results = [future.result() for future in futures]
    samples = [sample for group, _, _ in results for sample in group]
    errors = sum(count for _, count, _ in results)
    elapsed = max(ended for _, _, ended in results) - clock["started"]
    if not samples:
        raise AssertionError("Concurrent trial had no successful responses")
    return len(samples) / elapsed, p95(samples), len(samples), errors


def normalized(payload):
    messages = json.loads(payload)
    for message in messages:
        for target, key in [(message, "url"), (message["creator"], "avatar_url")]:
            parsed = urllib.parse.urlsplit(target[key])
            target[key] = parsed.path + ("?" + parsed.query if parsed.query else "")
    return messages


def page(port, query):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    try:
        connection.request("GET", PATH + query, headers={"Accept": "application/json"})
        response = connection.getresponse()
        body = response.read()
        if response.status != 200:
            raise AssertionError((response.status, body[:300]))
        return body
    finally:
        connection.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--messages", type=int, default=10000)
    parser.add_argument("--iterations", type=int, default=30)
    parser.add_argument("--clients", type=int, nargs="*", default=[])
    parser.add_argument("--seconds", type=float, default=3.0)
    parser.add_argument("--campfire-workers", type=int, default=1)
    parser.add_argument("--include-attachments", action="store_true")
    parser.add_argument("--scramble-timestamps", action="store_true")
    parser.add_argument("--campfire-repo", type=pathlib.Path, default=pathlib.Path("/tmp/once-campfire-reference"))
    parser.add_argument("--ruby", type=pathlib.Path, default=pathlib.Path("/tmp/rustfire-baseline/local/bin/ruby"))
    parser.add_argument("--bundle-path", type=pathlib.Path, default=pathlib.Path("/tmp/rustfire-baseline/bundle"))
    args = parser.parse_args()
    if args.messages < 40 or args.iterations < 1 or any(client < 1 for client in args.clients) or args.seconds <= 0 or args.campfire_workers < 1:
        parser.error("messages must be at least 40; iterations, clients, seconds, and workers must be positive")
    if args.scramble_timestamps and args.messages < 41:
        parser.error("scrambled timestamp pagination needs at least 41 messages")
    repo = args.campfire_repo.resolve()
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    if revision != "91d294f4a09f9bbe37f9548959bfcb43645678fb":
        parser.error(f"Campfire source is at {revision}, not the pinned compatibility target")
    source_database = repo / "storage/db/production.sqlite3"
    if not source_database.is_file():
        parser.error("Campfire production fixture database is missing")
    ruby = args.ruby.resolve()
    bundle_path = args.bundle_path.resolve()

    with tempfile.TemporaryDirectory(prefix="paired-bot-messages-bench-") as scratch:
        temp = pathlib.Path(scratch)
        rust_database, camp_database = temp / "rust.sqlite3", temp / "camp.sqlite3"
        rust_port, camp_port = free_port(), free_port()
        seed_rustfire(rust_database, rust_port, [])
        camp_env = seed_campfire(repo, ruby, bundle_path, source_database, camp_database, [], camp_port, temp)
        camp_env["WEB_CONCURRENCY"] = str(args.campfire_workers)
        seed_messages(rust_database, camp_database, args.messages, args.include_attachments, args.scramble_timestamps)
        expected_ids = (list(range(args.messages - 39, args.messages)) + [1]
                        if args.scramble_timestamps else list(range(args.messages - 39, args.messages + 1)))

        rust_process = start_server(rust_database, rust_port, {"RUSTFIRE_CAMPFIRE_SECRET_KEY_BASE": camp_env["SECRET_KEY_BASE"]})
        try:
            rust_median, rust_p95, rust_body = measure(rust_port, args.iterations, expected_ids)
            rust_pages = ([page(rust_port, f"?before={expected_ids[0]}"), page(rust_port, f"?after={expected_ids[0]}")]
                          if args.scramble_timestamps else [])
            rust_concurrent = {clients: measure_concurrent(rust_port, clients, args.seconds, rust_body) for clients in args.clients}
        finally:
            stop_server(rust_process)

        log = open(temp / "puma.log", "w+")
        camp_process = subprocess.Popen(
            [str(ruby), str(ruby.parent / "bundle"), "exec", "puma", "-C", "config/puma.rb"],
            cwd=repo, env=camp_env, stdout=log, stderr=log,
        )
        try:
            wait_for_server(camp_port, camp_process)
            if args.campfire_workers > 1:
                time.sleep(5)
            camp_median, camp_p95, camp_body = measure(camp_port, args.iterations, expected_ids)
            camp_pages = ([page(camp_port, f"?before={expected_ids[0]}"), page(camp_port, f"?after={expected_ids[0]}")]
                          if args.scramble_timestamps else [])
            camp_concurrent = {clients: measure_concurrent(camp_port, clients, args.seconds, camp_body) for clients in args.clients}
        except Exception:
            log.flush()
            log.seek(0)
            print(log.read()[-2000:])
            raise
        finally:
            stop_server(camp_process)
            log.close()

        rust_json, camp_json = normalized(rust_body), normalized(camp_body)
        if rust_json != camp_json:
            for rust_message, camp_message in zip(rust_json, camp_json):
                if rust_message != camp_message:
                    raise AssertionError(("Message JSON differs", rust_message, camp_message))
            raise AssertionError("Message-list length differs")
        for direction, rust_page, camp_page in zip(("before", "after"), rust_pages, camp_pages):
            if normalized(rust_page) != normalized(camp_page):
                raise AssertionError((f"{direction} page differs", [item["id"] for item in normalized(rust_page)], [item["id"] for item in normalized(camp_page)]))
        print(f"messages={args.messages} returned=40 iterations={args.iterations} campfire_workers={args.campfire_workers} attachments={args.include_attachments} scrambled={args.scramble_timestamps} normalized_json_equal=true")
        print(f"rustfire_median_ms={rust_median:.3f} rustfire_p95_ms={rust_p95:.3f} body_bytes={len(rust_body)}")
        print(f"campfire_median_ms={camp_median:.3f} campfire_p95_ms={camp_p95:.3f} body_bytes={len(camp_body)}")
        for clients in args.clients:
            rust_rate, rust_tail, rust_count, rust_errors = rust_concurrent[clients]
            camp_rate, camp_tail, camp_count, camp_errors = camp_concurrent[clients]
            print(f"clients={clients} rustfire_rps={rust_rate:.1f} rustfire_p95_ms={rust_tail:.3f} rustfire_successes={rust_count} rustfire_errors={rust_errors}")
            print(f"clients={clients} campfire_rps={camp_rate:.1f} campfire_p95_ms={camp_tail:.3f} campfire_successes={camp_count} campfire_errors={camp_errors}")


if __name__ == "__main__":
    main()
