"""Import a large seeded Campfire fixture and compare every paged message response.

Requires the pinned checkout, bundled Ruby, Redis, and a release Rustfire build.
The pinned database schema is copied, then deterministic, distinct-time messages
are inserted into a disposable fixture so both apps can traverse every row.
"""

import argparse
from datetime import datetime, timedelta
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import tempfile
import time

from direct_lookup import free_port, start_server, stop_server
from paired_banned_content import start_redis
from paired_direct_lookup import login_campfire, seed_campfire, wait_for_server
from paired_message_cache import measure_cached_read, measure_full_read
from paired_room_shell import AGENT, section


SOURCE_REVISION = "91d294f4a09f9bbe37f9548959bfcb43645678fb"
MESSAGE_IDS = re.compile(rb'data-message-id=["\'](\d+)')


def get(port, cookie, path):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    try:
        connection.request("GET", path, headers={"Cookie": cookie, "User-Agent": AGENT, "Accept": "text/html"})
        response = connection.getresponse()
        return response.status, {key.lower(): value for key, value in response.getheaders()}, response.read()
    finally:
        connection.close()


def page_signature(body):
    tokens = section(b"<body>" + body + b"</body>", "body", normalize_times=True,
        normalize_avatar_paths=True, normalize_blob_paths=True, normalize_text_origins=True,
        ignore_csrf_inputs=True)
    return hashlib.sha256(repr(tokens).encode()).hexdigest(), len(tokens)


def pages(port, cookie, expected, label, room_id=1):
    path = f"/rooms/{room_id}/messages"
    seen = set()
    total_bytes = 0
    number = 0
    while True:
        status, headers, body = get(port, cookie, path)
        if status == 204:
            assert not body and number and len(seen) == expected, (label, number, len(seen), expected)
            break
        assert status == 200, (label, path, status, body[:300])
        ids = [int(value) for value in MESSAGE_IDS.findall(body)]
        assert ids and len(ids) <= 40 and not (set(ids) & seen), (label, number, ids)
        assert headers.get("etag", "").startswith('W/"') and headers.get("last-modified"), (label, path, headers)
        signature, token_count = page_signature(body)
        result = (ids, headers["etag"], headers["last-modified"], signature, token_count)
        yield result
        seen.update(ids)
        total_bytes += len(body)
        number += 1
        if number % 50 == 0:
            print(f"{label}: {number} pages, {len(seen)} messages", flush=True)
        path = f"/rooms/{room_id}/messages?before={ids[0]}"
    print(f"{label}: {number} pages, {len(seen)} messages, {total_bytes} raw HTML bytes", flush=True)


def seed_messages(database, count):
    base = datetime.fromisoformat("2026-01-01 00:00:00")
    messages, rich_texts, search_rows = [], [], []
    for id in range(1, count + 1):
        timestamp = (base + timedelta(milliseconds=id)).strftime("%Y-%m-%d %H:%M:%S.%f")
        plain = f"Large import message {id:05d}"
        body = f"<div>{plain} <strong>bold</strong></div>" if id % 10 == 0 else f"<div>{plain}</div>"
        messages.append((id, f"large-import-{id}", timestamp))
        rich_texts.append((body, id, timestamp, timestamp))
        search_rows.append((id, plain + (" bold" if id % 10 == 0 else "")))
    with sqlite3.connect(database) as db:
        db.executemany("INSERT INTO messages(id,room_id,creator_id,client_message_id,created_at,updated_at) VALUES(?,1,1,?,?,?)",
            ((id, client_id, timestamp, timestamp) for id, client_id, timestamp in messages))
        db.executemany("INSERT INTO action_text_rich_texts(name,body,record_type,record_id,created_at,updated_at) VALUES('body',?,'Message',?,?,?)", rich_texts)
        db.executemany("INSERT INTO message_search_index(rowid,body) VALUES(?,?)", search_rows)


def measure_reads(temp, repository, ruby, environment, target_db, uploads,
        camp_port, rust_port, cookie, validator, clients_list, seconds, campfire_workers):
    binary = temp / "checked_get"
    subprocess.run(("go", "build", "-o", str(binary), "bench/checked_get.go"), check=True)
    etag, modified = validator
    trials = []
    for mode in ("full", "conditional"):
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
                                report = (measure_full_read(binary, camp_port, cookie, etag, modified, clients, seconds, 40)
                                    if mode == "full" else measure_cached_read(binary, camp_port, cookie, etag, modified, clients, seconds))
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
                            report = (measure_full_read(binary, rust_port, cookie, etag, modified, clients, seconds, 40)
                                if mode == "full" else measure_cached_read(binary, rust_port, cookie, etag, modified, clients, seconds))
                        finally:
                            stop_server(process)
                    trials.append({"mode": mode, "clients": clients, "order": list(order), "server": name, "result": report})
                    print(f"{mode} {clients} {name}: {report['rps']:.0f}/s p95 {report['p95_ms']:.2f}ms errors {report['errors']}", flush=True)
    return trials


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--campfire-repo", type=Path, default=Path("/tmp/once-campfire-reference"))
    parser.add_argument("--ruby", type=Path, default=Path("/tmp/rustfire-baseline/local/bin/ruby"))
    parser.add_argument("--bundle-path", type=Path, default=Path("/tmp/rustfire-baseline/bundle"))
    parser.add_argument("--messages", type=int, default=16_230, help="number of seeded messages (at least 41)")
    parser.add_argument("--read-clients", type=int, nargs="*", default=[], help="optional full-200 and conditional-304 client counts")
    parser.add_argument("--read-seconds", type=float, default=5.0)
    parser.add_argument("--campfire-workers", type=int, default=22)
    parser.add_argument("--report", type=Path, help="save structured paired read trials")
    args = parser.parse_args()
    if args.messages < 41 or any(clients < 1 for clients in args.read_clients) or args.read_seconds <= 0 or args.campfire_workers < 1:
        parser.error("messages must be at least 41; clients, seconds, and workers must be positive")
    if args.report and not args.read_clients:
        parser.error("--report requires --read-clients")
    repository, ruby, bundle_path = args.campfire_repo.resolve(), args.ruby.resolve(), args.bundle_path.resolve()
    revision = subprocess.check_output(("git", "rev-parse", "HEAD"), cwd=repository, text=True).strip()
    assert revision == SOURCE_REVISION, revision
    with tempfile.TemporaryDirectory(prefix="paired-large-import-") as directory:
        temp = Path(directory)
        source_db, target_db, uploads = temp / "campfire.sqlite3", temp / "rustfire.sqlite3", temp / "uploads"
        camp_port, rust_port, redis_port = free_port(), free_port(), free_port()
        environment = seed_campfire(repository, ruby, bundle_path,
            repository / "storage/db/production.sqlite3", source_db, [], camp_port, temp)
        seed_messages(source_db, args.messages)
        expected = args.messages
        environment["REDIS_URL"] = f"redis://127.0.0.1:{redis_port}"
        bundle = ruby.parent / "bundle"
        redis, redis_log = start_redis(temp, redis_port)
        try:
            with (temp / "puma.log").open("w+") as log:
                camp = subprocess.Popen((str(ruby), str(bundle), "exec", "puma", "-C", "config/puma.rb"),
                    cwd=repository, env=environment, stdout=log, stderr=log)
                try:
                    wait_for_server(camp_port, camp)
                    cookie, _ = login_campfire(camp_port)
                    source_pages = list(pages(camp_port, cookie, expected, "Campfire"))
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
                env=dict(os.environ, RUSTFIRE_CAMPFIRE_SECRET_KEY_BASE=environment["SECRET_KEY_BASE"]),
                check=True, text=True, capture_output=True)
            elapsed = time.monotonic() - started
            counts = json.loads(importer.stdout)
            assert counts["messages"] == expected, counts
            print(f"import: {counts['messages']} messages in {elapsed:.2f}s", flush=True)
            rust = start_server(target_db, rust_port, {"RUSTFIRE_UPLOAD_DIR": str(uploads),
                "RUSTFIRE_CAMPFIRE_SECRET_KEY_BASE": environment["SECRET_KEY_BASE"]})
            try:
                target_pages = pages(rust_port, cookie, expected, "Rustfire")
                for number, (original, imported) in enumerate(zip(source_pages, target_pages, strict=True), 1):
                    assert original == imported, (number, original[:3], imported[:3], original[3:], imported[3:])
                print(f"PASS {len(source_pages)} complete message pages with matching IDs, ETags, Last-Modified, and parsed bodies")
            finally:
                stop_server(rust)
            if args.read_clients:
                trials = measure_reads(temp, repository, ruby, environment, target_db, uploads,
                    camp_port, rust_port, cookie, source_pages[0][1:3], args.read_clients,
                    args.read_seconds, args.campfire_workers)
                if args.report:
                    args.report.parent.mkdir(parents=True, exist_ok=True)
                    args.report.write_text(json.dumps({"source_revision": revision,
                        "rustfire_revision": subprocess.check_output(("git", "rev-parse", "HEAD"), text=True).strip(),
                        "fixture": {"messages": expected, "rooms": 1, "rendered_messages": 40,
                            "rich_text_interval": 10, "pages_checked": len(source_pages)},
                        "seconds": args.read_seconds, "campfire_workers": args.campfire_workers,
                        "trials": trials}, indent=2) + "\n")
        finally:
            redis.terminate()
            redis.wait(timeout=10)
            redis_log.close()


if __name__ == "__main__":
    main()
