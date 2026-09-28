"""Compare Campfire and Rustfire message-page cache validators on disposable fixtures.

Requires the pinned Campfire checkout, bundled Ruby, and a release Rustfire build.
"""

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import http.client
import json
import pathlib
import re
import sqlite3
import subprocess
import tempfile
import threading
import time

from direct_lookup import ROOT, free_port, start_server, stop_server
from message_markup import check_message_markup
from paired_banned_content import REPOSITORY, RUBY, BUNDLE, REVISION, start_redis
from paired_direct_lookup import login_campfire, seed_campfire, seed_rustfire, wait_for_server
from paired_room_refresh import seed_messages


def fetch(port, cookie, path, conditional=None):
    headers = {"Cookie": cookie}
    if conditional:
        headers.update(conditional)
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
    try:
        connection.request("GET", path, headers=headers)
        response = connection.getresponse()
        body = response.read()
        return response.status, dict(response.getheaders()), body
    finally:
        connection.close()


def get_header(headers, name):
    return next((value for key, value in headers.items() if key.lower() == name.lower()), None)


def update_timestamp(database, rails, timestamp):
    with sqlite3.connect(database) as db:
        if rails:
            formatted = timestamp.strftime("%Y-%m-%d %H:%M:%S.%f")
        else:
            formatted = timestamp.isoformat().replace("+00:00", "Z")
        db.execute("UPDATE messages SET updated_at=? WHERE id=2 AND room_id=1", (formatted,))
        if not rails:
            nanos = int(timestamp.timestamp()) * 1_000_000_000 + timestamp.microsecond * 1000
            db.execute("UPDATE messages SET updated_at_ns=? WHERE id=2 AND room_id=1", (nanos,))


def set_distinct_creation_times(database, rails):
    # Direct inserts with an exact .000000 timestamp compare differently against
    # Rails' bound SQLite datetime without a fractional part; real records do not
    # normally land on that fixture boundary.
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    with sqlite3.connect(database) as db:
        for message_id in (1, 2, 3):
            created = base + timedelta(seconds=(message_id - 1) * 60, microseconds=message_id * 123456)
            if rails:
                db.execute("UPDATE messages SET created_at=? WHERE id=?", (created.strftime("%Y-%m-%d %H:%M:%S.%f"), message_id))
            else:
                nanos = int(created.timestamp()) * 1_000_000_000 + created.microsecond * 1000
                db.execute("UPDATE messages SET created_at=?,created_at_ns=? WHERE id=?", (created.isoformat().replace("+00:00", "Z"), nanos, message_id))


def seed_additional_messages(database, rails, total):
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    with sqlite3.connect(database) as db:
        for message_id in range(4, total + 1):
            created = base + timedelta(seconds=(message_id - 1) * 60, microseconds=message_id * 1000 + 456)
            body, client_id = f"cache message {message_id}", f"refresh-{message_id}"
            if rails:
                stamp = created.strftime("%Y-%m-%d %H:%M:%S.%f")
                db.execute("INSERT INTO messages(id,room_id,creator_id,client_message_id,created_at,updated_at) VALUES(?,1,1,?,?,?)", (message_id, client_id, stamp, stamp))
                db.execute("INSERT INTO action_text_rich_texts(name,body,record_type,record_id,created_at,updated_at) VALUES('body',?,'Message',?,?,?)", (body, message_id, stamp, stamp))
            else:
                stamp = created.isoformat().replace("+00:00", "Z")
                nanos = int(created.timestamp()) * 1_000_000_000 + created.microsecond * 1000
                db.execute("INSERT INTO messages(id,room_id,creator_id,body,client_message_id,created_at,created_at_ns,updated_at,updated_at_ns) VALUES(?,1,1,?,?,?,?,?,?)", (message_id, body, client_id, stamp, nanos, stamp, nanos))


def check(port, cookie, database, rails, sample_path=None):
    path = "/rooms/1/messages"
    first_status, first_headers, first_body = fetch(port, cookie, path)
    etag = get_header(first_headers, "ETag")
    modified = get_header(first_headers, "Last-Modified")
    cache_control = get_header(first_headers, "Cache-Control")
    assert first_status == 200 and first_body and etag and etag.startswith('W/"') and modified, (first_status, first_headers)
    if sample_path:
        sample_path.write_bytes(first_body)
    ids = lambda body: [int(value) for value in re.findall(rb'id=["\']message_refresh-(\d+)["\']', body)]

    by_etag = fetch(port, cookie, path, {"If-None-Match": etag})
    by_date = fetch(port, cookie, path, {"If-Modified-Since": modified})
    stale = fetch(port, cookie, path, {"If-Modified-Since": "Thu, 01 Jan 1970 00:00:00 GMT"})
    assert by_etag[0] == 304 and not by_etag[2], (by_etag[0], len(by_etag[2]))
    assert by_date[0] == 304 and not by_date[2], (by_date[0], len(by_date[2]))
    assert stale[0] == 200 and stale[2], (stale[0], len(stale[2]))
    assert get_header(by_etag[1], "ETag") == etag
    assert get_header(by_date[1], "Last-Modified") == modified

    pages = {}
    for label, suffix in (("before", "?before=3"), ("after", "?after=1")):
        status, headers, body = fetch(port, cookie, path + suffix)
        page_etag = get_header(headers, "ETag")
        assert status == 200 and body and page_etag, (label, status, headers)
        again = fetch(port, cookie, path + suffix, {"If-None-Match": page_etag})
        assert again[0] == 304 and not again[2], (label, again[0], len(again[2]))
        pages[label] = (page_etag, len(body), ids(body))
    assert len({etag, pages["before"][0], pages["after"][0]}) == 3, (etag, pages)

    empty = fetch(port, cookie, path + "?before=1")
    assert empty[0] == 204 and not empty[2], (empty[0], len(empty[2]))
    cursor_cases = (
        "?before=bad", "?after=bad", "?before=0", "?after=999",
        "?before=2&after=1", "?before=2&after=bad", "?before=2abc",
        "?before=2&before=3", "?before=2&before%5B%5D=3",
        "?before%5B%5D=3&before=2", "?before%5B%5D=2",
        "?before%5B%5D=", "?before%5Bvalue%5D=2", "?after%5B%5D=1",
        "?before=2&after%5B%5D=1", "?after=1.0",
    )
    cursor_results = {}
    cursor_bodies = {}
    for suffix in cursor_cases:
        status, response_headers, body = fetch(port, cookie, path + suffix)
        if status == 200:
            cursor_etag = get_header(response_headers, "ETag")
            assert cursor_etag and fetch(port, cookie, path + suffix, {"If-None-Match": cursor_etag})[0] == 304, (suffix, status, cursor_etag)
            cursor_bodies[suffix] = body
        cursor_results[suffix] = (status, get_header(response_headers, "Content-Type"), hashlib.sha256(body).hexdigest() if status >= 400 else ids(body))
    for suffix in ("?before%5B%5D=2", "?before%5B%5D=", "?before=2&before%5B%5D=3", "?before=2"):
        status, response_headers, body = fetch(port, cookie, path + suffix, {"Accept": "application/json"})
        cursor_results[f"JSON {suffix}"] = (
            status, get_header(response_headers, "Content-Type"),
            hashlib.sha256(body).hexdigest() if status >= 400 else ids(body),
        )

    changed_at = datetime.now(timezone.utc) + timedelta(seconds=10)
    update_timestamp(database, rails, changed_at)
    changed = fetch(port, cookie, path, {"If-None-Match": etag, "If-Modified-Since": modified})
    new_etag = get_header(changed[1], "ETag")
    new_modified = get_header(changed[1], "Last-Modified")
    assert changed[0] == 200 and changed[2] and new_etag and new_etag != etag, (changed[0], etag, new_etag)
    assert new_modified and new_modified != modified, (modified, new_modified)
    mixed = fetch(port, cookie, path, {"If-None-Match": etag, "If-Modified-Since": new_modified})
    assert mixed[0] == 200 and mixed[2], (mixed[0], len(mixed[2]))
    result = {
        "initial": first_status,
        "etag": etag,
        "etag_conditional": by_etag[0],
        "date_conditional": by_date[0],
        "stale_date": stale[0],
        "empty_page": empty[0],
        "cursor_results": cursor_results,
        "changed": changed[0],
        "stale_etag_fresh_date": mixed[0],
        "cache_control": cache_control,
        "body_bytes": len(first_body),
        "initial_ids": ids(first_body),
        "before_bytes": pages["before"][1],
        "after_bytes": pages["after"][1],
        "before_etag": pages["before"][0],
        "after_etag": pages["after"][0],
        "before_ids": pages["before"][2],
        "after_ids": pages["after"][2],
    }
    return result, new_etag, new_modified, first_body, cursor_bodies


def measure_cached_read(binary, port, cookie, etag, modified, clients, seconds):
    command = [
        str(binary), "--base", f"http://127.0.0.1:{port}", "--path", "/rooms/1/messages",
        "--cookie", cookie, "--expected-sha256", hashlib.sha256(b"").hexdigest(),
        "--expected-status", "304", "--expected-etag", etag,
        "--expected-last-modified", modified, "--if-none-match", etag,
        "--accept", "text/html", "--clients", str(clients), "--seconds", str(seconds),
    ]
    process = subprocess.run(command, text=True, capture_output=True, timeout=max(90, seconds + 60))
    if process.returncode:
        raise AssertionError(f"Conditional message read failed: {process.stdout}\n{process.stderr}")
    report = json.loads(process.stdout.strip().splitlines()[-1])
    assert report["errors"] == 0 and report["successes"] > 0, report
    return report


def resource_snapshot(root_pids):
    import psutil

    processes = []
    for pid in root_pids:
        parent = psutil.Process(pid)
        processes.extend((parent, *parent.children(recursive=True)))
    pss = cpu = 0
    count = 0
    for process in {process.pid: process for process in processes}.values():
        try:
            memory = process.memory_full_info()
            usage = process.cpu_times()
        except psutil.Error:
            continue
        pss += getattr(memory, "pss", memory.rss)
        cpu += usage.user + usage.system
        count += 1
    return pss, cpu, count


def measure_full_read(binary, port, cookie, etag, modified, clients, seconds, messages, resource_pids=()):
    command = [
        str(binary), "--base", f"http://127.0.0.1:{port}", "--path", "/rooms/1/messages",
        "--cookie", cookie, "--expected-status", "200", "--expected-etag", etag,
        "--expected-last-modified", modified, "--expected-message-count", str(messages),
        "--expected-csrf-count", str(messages * 8), "--accept", "text/html",
        "--clients", str(clients), "--seconds", str(seconds),
    ]
    baseline = resource_snapshot(resource_pids) if resource_pids else None
    stop = threading.Event()
    observed = [baseline] if baseline else []

    def sample_resources():
        while not stop.wait(0.5):
            observed.append(resource_snapshot(resource_pids))

    started = time.monotonic()
    process = subprocess.Popen(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    sampler = threading.Thread(target=sample_resources, daemon=True) if resource_pids else None
    if sampler:
        sampler.start()
    try:
        stdout, stderr = process.communicate(timeout=max(90, seconds + 60))
    except subprocess.TimeoutExpired:
        process.kill()
        process.communicate()
        raise AssertionError("Full message read client timed out")
    finally:
        if sampler:
            stop.set()
            sampler.join()
            observed.append(resource_snapshot(resource_pids))
    if process.returncode:
        raise AssertionError(f"Full message read failed: {stdout}\n{stderr}")
    report = json.loads(stdout.strip().splitlines()[-1])
    assert report["errors"] == 0 and report["successes"] > 0, report
    if observed:
        report["server_cpu_seconds_including_warmup"] = round(observed[-1][1] - observed[0][1], 3)
        report["server_peak_pss_mib"] = round(max(item[0] for item in observed) / 1048576, 2)
        report["server_processes_peak"] = max(item[2] for item in observed)
        report["resource_window_seconds"] = round(time.monotonic() - started, 3)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--messages", type=int, default=40, help="messages on the cached latest page, 3–40")
    parser.add_argument("--clients", type=int, nargs="*", default=[], help="optional conditional-304 concurrency sweep")
    parser.add_argument("--full-clients", type=int, nargs="*", default=[], help="optional full-200 HTML concurrency sweep")
    parser.add_argument("--resources", action="store_true", help="sample server CPU time and peak PSS during full-200 trials (requires psutil)")
    parser.add_argument("--seconds", type=float, default=15)
    parser.add_argument("--campfire-workers", type=int, default=22)
    parser.add_argument("--rustfire-first", action="store_true", help="reverse the serial trial order")
    parser.add_argument("--sample-dir", type=pathlib.Path, help="save the initial full HTML response from each app")
    args = parser.parse_args()
    if any(client < 1 for client in args.clients + args.full_clients) or args.seconds <= 0 or args.campfire_workers < 1 or not (3 <= args.messages <= 40):
        parser.error("client counts, seconds, and Campfire workers must be positive; messages must be 3–40")
    if args.resources:
        try:
            import psutil  # noqa: F401
        except ImportError:
            parser.error("--resources requires the psutil Python package")
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPOSITORY, text=True).strip() == REVISION
    if args.sample_dir:
        args.sample_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="paired-message-cache-") as scratch:
        temp = pathlib.Path(scratch)
        rust_db, camp_db = temp / "rust.sqlite3", temp / "camp.sqlite3"
        rust_port, camp_port, redis_port = free_port(), free_port(), free_port()
        binary = temp / "checked_get"
        if args.clients or args.full_clients:
            subprocess.run(["go", "build", "-o", str(binary), "bench/checked_get.go"], cwd=ROOT, check=True)
        seed_rustfire(rust_db, rust_port, [])
        camp_env = seed_campfire(REPOSITORY, RUBY, BUNDLE, REPOSITORY / "storage/db/production.sqlite3", camp_db, [], camp_port, temp)
        camp_env["REDIS_URL"] = f"redis://127.0.0.1:{redis_port}"
        camp_env["WEB_CONCURRENCY"] = str(args.campfire_workers)
        seed_messages(rust_db, camp_db)
        set_distinct_creation_times(rust_db, False)
        set_distinct_creation_times(camp_db, True)
        seed_additional_messages(rust_db, False, args.messages)
        seed_additional_messages(camp_db, True, args.messages)
        redis, redis_log = start_redis(temp, redis_port)
        try:
            def run_campfire():
                with open(temp / "puma.log", "w+") as log:
                    camp = subprocess.Popen([str(RUBY), str(RUBY.parent / "bundle"), "exec", "puma", "-C", "config/puma.rb"], cwd=REPOSITORY, env=camp_env, stdout=log, stderr=log)
                    try:
                        wait_for_server(camp_port, camp)
                        cookie, _ = login_campfire(camp_port)
                        result, etag, modified, body, cursors = check(camp_port, cookie, camp_db, True, args.sample_dir / "campfire-messages.html" if args.sample_dir else None)
                        performance = {clients: measure_cached_read(binary, camp_port, cookie, etag, modified, clients, args.seconds) for clients in args.clients}
                        full_performance = {clients: measure_full_read(binary, camp_port, cookie, etag, modified, clients, args.seconds, args.messages, (camp.pid, redis.pid) if args.resources else ()) for clients in args.full_clients}
                        return result, performance, full_performance, body, cursors
                    finally:
                        stop_server(camp)

            def run_rustfire():
                rust = start_server(rust_db, rust_port)
                try:
                    result, etag, modified, body, cursors = check(rust_port, "session_token=benchmark-session", rust_db, False, args.sample_dir / "rustfire-messages.html" if args.sample_dir else None)
                    performance = {clients: measure_cached_read(binary, rust_port, "session_token=benchmark-session", etag, modified, clients, args.seconds) for clients in args.clients}
                    full_performance = {clients: measure_full_read(binary, rust_port, "session_token=benchmark-session", etag, modified, clients, args.seconds, args.messages, (rust.pid,) if args.resources else ()) for clients in args.full_clients}
                    return result, performance, full_performance, body, cursors
                finally:
                    stop_server(rust)

            if args.rustfire_first:
                (rust_result, rust_performance, rust_full, rust_body, rust_cursors), (camp_result, camp_performance, camp_full, camp_body, camp_cursors) = run_rustfire(), run_campfire()
            else:
                (camp_result, camp_performance, camp_full, camp_body, camp_cursors), (rust_result, rust_performance, rust_full, rust_body, rust_cursors) = run_campfire(), run_rustfire()
        finally:
            redis.terminate()
            redis.wait(timeout=10)
            redis_log.close()
        assert {key: value for key, value in rust_result.items() if key not in ("body_bytes", "before_bytes", "after_bytes")} == {key: value for key, value in camp_result.items() if key not in ("body_bytes", "before_bytes", "after_bytes")}, (rust_result, camp_result)
        check_message_markup(camp_body, rust_body, args.messages)
        assert camp_cursors.keys() == rust_cursors.keys(), (camp_cursors.keys(), rust_cursors.keys())
        for suffix in camp_cursors:
            check_message_markup(camp_cursors[suffix], rust_cursors[suffix], len(re.findall(rb'id=["\']message_refresh-\d+["\']', camp_cursors[suffix])))
        print("PASS paired message-list ETag, Last-Modified, pagination, empty page, and invalidation")
        print({"rustfire": rust_result, "campfire": camp_result})
        for clients in args.clients:
            print(json.dumps({"clients": clients, "seconds": args.seconds, "campfire_workers": args.campfire_workers, "rustfire_first": args.rustfire_first, "rustfire": rust_performance[clients], "campfire": camp_performance[clients]}, sort_keys=True))
        for clients in args.full_clients:
            print(json.dumps({"mode": "full_html_200", "clients": clients, "seconds": args.seconds, "campfire_workers": args.campfire_workers, "rustfire_first": args.rustfire_first, "rustfire": rust_full[clients], "campfire": camp_full[clients]}, sort_keys=True))


if __name__ == "__main__":
    main()
