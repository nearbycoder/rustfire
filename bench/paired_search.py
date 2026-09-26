"""Compare full search-result pages on matched, disposable 10,000-message fixtures.

Run after cargo build --release and with Redis running. This checks the last
100 result IDs and message text before reporting warm, serial GET latency.
The fixture gives both apps the same simple rich-text body. Raw HTML bytes
still differ, so the timing is a search-page observation.
"""

import argparse
from datetime import datetime, timedelta, timezone
import http.client
import os
import pathlib
import re
import sqlite3
import statistics
import subprocess
import tempfile
import time

from direct_lookup import free_port, p95, start_server, stop_server
from paired_direct_lookup import login_campfire, seed_campfire, seed_rustfire, wait_for_server


def seed_messages(rust_path, camp_path, count):
    origin = datetime(2026, 1, 1, tzinfo=timezone.utc)
    stamps = [
        (i, (origin + timedelta(seconds=i)).strftime("%Y-%m-%d %H:%M:%S.%f"),
         (origin + timedelta(seconds=i)).isoformat(timespec="microseconds").replace("+00:00", "Z"),
         1767225600000000000 + i * 1_000_000_000)
        for i in range(1, count + 1)
    ]
    with sqlite3.connect(camp_path) as camp, sqlite3.connect(rust_path) as rust:
        name, updated_at = camp.execute("SELECT name,updated_at FROM users WHERE id=1").fetchone()
        room_name = camp.execute("SELECT name FROM rooms WHERE id=1").fetchone()[0]
        rust.execute("UPDATE users SET name=?1,updated_at=?2 WHERE id=1", [name, updated_at])
        rust.execute("UPDATE rooms SET name=? WHERE id=1", [room_name])
        rust.executemany(
            "INSERT INTO messages(id,room_id,creator_id,body,body_html,client_message_id,created_at,created_at_ns,updated_at,updated_at_ns) VALUES(?1,1,1,?2,?3,?4,?5,?6,?5,?6)",
            ((i, f"benchmark message {i}", f"<div>benchmark message {i}</div>", f"fixture-{i}", rust_stamp, stamp_ns) for i, _, rust_stamp, stamp_ns in stamps),
        )
        camp.executemany(
            "INSERT INTO messages(id,client_message_id,created_at,creator_id,room_id,updated_at) VALUES(?1,?2,?3,1,1,?3)",
            ((i, f"fixture-{i}", camp_stamp) for i, camp_stamp, _, _ in stamps),
        )
        camp.executemany(
            "INSERT INTO action_text_rich_texts(name,body,record_type,record_id,created_at,updated_at) VALUES('body',?2,'Message',?1,?3,?3)",
            ((i, f"<div>benchmark message {i}</div>", camp_stamp) for i, camp_stamp, _, _ in stamps),
        )
        camp.executemany(
            "INSERT INTO message_search_index(rowid,body) VALUES(?1,?2)",
            ((i, f"benchmark message {i}") for i in range(1, count + 1)),
        )


def measure(port, cookie, count, requests):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=90)
    samples = []
    body_bytes = set()
    expected = [f"fixture-{i}" for i in range(count - 99, count + 1)]
    try:
        for iteration in range(requests + 3):
            started = time.perf_counter()
            connection.request("GET", "/searches?q=benchmark", headers={"Cookie": cookie, "Accept": "text/html"})
            response = connection.getresponse()
            body = response.read()
            elapsed_ms = (time.perf_counter() - started) * 1000
            if response.status != 200:
                raise AssertionError((response.status, body[:200]))
            page = body.decode()
            if not re.search(r"id=['\"]search-results['\"]", page):
                raise AssertionError("search results container missing")
            ids = re.findall(r"id=['\"]message_(fixture-\d+)['\"]", page)
            if ids != expected:
                raise AssertionError((ids[:3], ids[-3:], len(ids), expected[:3], expected[-3:]))
            contents = re.findall(r"benchmark message (\d+)</", page)
            if contents != [str(i) for i in range(count - 99, count + 1)]:
                raise AssertionError(("search result content differs", contents[:3], contents[-3:], len(contents)))
            if iteration >= 3:
                samples.append(elapsed_ms)
                body_bytes.add(len(body))
    finally:
        connection.close()
    return statistics.median(samples), p95(samples), sorted(body_bytes)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--messages", type=int, default=10_000)
    parser.add_argument("--requests", type=int, default=20)
    parser.add_argument("--campfire-repo", type=pathlib.Path, default=pathlib.Path("/tmp/once-campfire-reference"))
    parser.add_argument("--ruby", type=pathlib.Path, default=pathlib.Path("/tmp/rustfire-baseline/local/bin/ruby"))
    parser.add_argument("--bundle-path", type=pathlib.Path, default=pathlib.Path("/tmp/rustfire-baseline/bundle"))
    args = parser.parse_args()
    if args.messages < 100 or args.requests < 1:
        parser.error("messages must be at least 100 and requests must be positive")
    repo = args.campfire_repo.resolve()
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    if revision != "91d294f4a09f9bbe37f9548959bfcb43645678fb":
        parser.error(f"Campfire source is at {revision}, not the pinned compatibility target")
    ruby = args.ruby.resolve()
    with tempfile.TemporaryDirectory(prefix="paired-search-") as scratch:
        temp = pathlib.Path(scratch)
        rust_path, camp_path = temp / "rust.sqlite3", temp / "camp.sqlite3"
        rust_port, camp_port = free_port(), free_port()
        seed_rustfire(rust_path, rust_port, [])
        camp_env = seed_campfire(repo, ruby, args.bundle_path.resolve(), repo / "storage/db/production.sqlite3", camp_path, [], camp_port, temp)
        seed_messages(rust_path, camp_path, args.messages)
        rust = start_server(rust_path, rust_port, {"RUSTFIRE_DISABLE_PUSH": "1", "RUSTFIRE_DISABLE_WEBHOOKS": "1"})
        try:
            rust_result = measure(rust_port, "session_token=benchmark-session", args.messages, args.requests)
        finally:
            stop_server(rust)
        print(f"rustfire median_ms={rust_result[0]:.2f} p95_ms={rust_result[1]:.2f} body_bytes={rust_result[2]}", flush=True)
        camp_env["WEB_CONCURRENCY"] = "1"
        with open(temp / "puma.log", "w+") as log:
            camp = subprocess.Popen(
                [str(ruby), str(ruby.parent / "bundle"), "exec", "puma", "-C", "config/puma.rb"],
                cwd=repo, env=camp_env, stdout=log, stderr=log,
            )
            try:
                wait_for_server(camp_port, camp)
                cookie, _ = login_campfire(camp_port)
                camp_result = measure(camp_port, cookie, args.messages, args.requests)
            except Exception:
                log.flush()
                log.seek(0)
                print(log.read()[-2000:])
                raise
            finally:
                stop_server(camp)
        print(f"campfire median_ms={camp_result[0]:.2f} p95_ms={camp_result[1]:.2f} body_bytes={camp_result[2]}")
        print(f"messages={args.messages} results=100 requests={args.requests} warmup=3 clients=1")


if __name__ == "__main__":
    main()
