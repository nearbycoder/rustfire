"""Run paired rendered-message reads while posting messages at a fixed rate.

Each app uses a disposable 40-message SQLite fixture and isolated Campfire Redis.
The workload has one authenticated writer and N concurrent readers in one room.
"""

import argparse
import http.client
import json
import pathlib
import re
import select
import sqlite3
import subprocess
import tempfile
import threading
import time
import urllib.parse

from direct_lookup import ROOT, free_port, start_server, stop_server
from message_markup import check_message_markup
from paired_banned_content import BUNDLE, REPOSITORY, REVISION, RUBY, start_redis
from paired_direct_lookup import login_campfire, seed_campfire, seed_rustfire, wait_for_server
from paired_message_cache import fetch, seed_additional_messages, set_distinct_creation_times
from paired_room_refresh import seed_messages
from paired_turbo_fanout import MessageTagSequence, check_message_times, stable_message_attributes


class StreamWithoutCsrfInputs(MessageTagSequence):
    def handle_starttag(self, tag, attrs):
        if tag == "input" and ("name", "authenticity_token") in attrs:
            assert any(key == "value" and value for key, value in attrs), "Empty stream CSRF input"
            return
        super().handle_starttag(tag, attrs)


def writer(port, cookie, csrf, count, seconds, result):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    latencies = []
    started = time.monotonic()
    span = max(0.1, seconds - 0.5)
    try:
        for index in range(count):
            delay = started + index * span / count - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            client_id = f"mixed-{index + 1}"
            fields = urllib.parse.urlencode({
                "message[body]": f"mixed message {index + 1}",
                "message[client_message_id]": client_id,
                "authenticity_token": csrf,
            })
            begun = time.monotonic()
            connection.request("POST", "/rooms/1/messages", fields, {
                "Cookie": cookie,
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "text/vnd.turbo-stream.html, text/html",
            })
            response = connection.getresponse()
            body = response.read()
            if response.status != 200 or response.getheader("Content-Type", "").split(";")[0] != "text/vnd.turbo-stream.html" or f"id=\"message_{client_id}\"".encode() not in body and f"id='message_{client_id}'".encode() not in body:
                raise AssertionError((index, response.status, response.getheader("Content-Type"), body[:200]))
            latencies.append((time.monotonic() - begun) * 1000)
        result.update({"writes": len(latencies), "write_p95_ms": sorted(latencies)[int(0.95 * (len(latencies) - 1))], "write_elapsed_s": time.monotonic() - started})
    except Exception as error:
        result["error"] = repr(error)
    finally:
        connection.close()


def measure(binary, port, cookie, csrf, clients, seconds, count, sockets=0, invalid_sample=None, stream_sample=None, stream_events=None):
    capture = None
    if sockets:
        capture_command = [
            "node", "bench/capture_message_appends.mjs", "--base", f"http://127.0.0.1:{port}",
            "--cookie", cookie, "--sockets", str(sockets), "--messages", str(count),
            "--timeout", str(round((seconds + 30) * 1000)),
        ]
        if stream_sample:
            capture_command.extend(("--sample-file", str(stream_sample)))
        if stream_events:
            capture_command.extend(("--events-file", str(stream_events)))
        capture = subprocess.Popen(capture_command, cwd=ROOT, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        ready, _, _ = select.select([capture.stdout], [], [], 60)
        marker = capture.stdout.readline().strip() if ready else ""
        if marker != "READY":
            capture.kill()
            stdout, stderr = capture.communicate()
            raise AssertionError(f"Socket capture did not start: {marker}\n{stdout}\n{stderr}")
    command = [
        str(binary), "--base", f"http://127.0.0.1:{port}", "--path", "/rooms/1/messages",
        "--cookie", cookie, "--expected-status", "200", "--expected-content-type", "text/html",
        "--expected-message-count", "40",
        "--accept", "text/html", "--clients", str(clients), "--seconds", str(seconds),
        "--signal-start",
    ]
    if invalid_sample:
        command.extend(("--invalid-sample", str(invalid_sample)))
    reader = subprocess.Popen(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        ready, _, _ = select.select([reader.stderr], [], [], max(90, seconds + 60))
        marker = reader.stderr.readline().strip() if ready else ""
        if marker != "MEASURE_START":
            raise AssertionError(f"Reader did not start: {marker}")
        writes = {}
        thread = threading.Thread(target=writer, args=(port, cookie, csrf, count, seconds, writes))
        thread.start()
        stdout, stderr = reader.communicate(timeout=max(90, seconds + 60))
        thread.join(timeout=45)
        if thread.is_alive():
            raise AssertionError("Writer did not finish")
        if "error" in writes:
            raise AssertionError(f"Writer failed: {writes['error']}")
        report = json.loads(stdout.strip().splitlines()[-1])
        assert reader.returncode == 0 and report["errors"] == 0, (report, stderr)
        assert writes["writes"] == count, writes
        result = {"reads": report, "writes": writes}
        if capture:
            stdout, stderr = capture.communicate(timeout=max(60, seconds + 45))
            delivery = json.loads(stdout.strip().splitlines()[-1])
            assert capture.returncode == 0 and delivery["missed"] == delivery["unexpected"] == delivery["closed_early"] == 0, (delivery, stderr)
            result["sockets"] = delivery
        return result
    finally:
        if reader.poll() is None:
            reader.kill()
            reader.communicate()
        if capture and capture.poll() is None:
            capture.kill()
            capture.communicate()


def saved_messages(database, count, rails):
    with sqlite3.connect(database) as db:
        if rails:
            rows = db.execute("SELECT m.id,m.client_message_id,t.body FROM messages m JOIN action_text_rich_texts t ON t.record_type='Message' AND t.record_id=m.id AND t.name='body' WHERE m.id>40 ORDER BY m.id").fetchall()
        else:
            rows = db.execute("SELECT id,client_message_id,body FROM messages WHERE id>40 ORDER BY id").fetchall()
    assert len(rows) == count, (len(rows), count)
    for index, (message_id, client_id, body) in enumerate(rows):
        assert (message_id, client_id) == (41 + index, f"mixed-{index + 1}")
        assert f"mixed message {index + 1}" in body


def check_final_page(body, count):
    ids = [int(value) for value in re.findall(rb"data-message-id=['\"](\d+)['\"]", body)]
    expected = list(range(1 + count, 41 + count))
    assert ids == expected, (ids, expected)


def check_stream_events(camp_file, rust_file, count):
    camp_events = json.loads(camp_file.read_text())
    rust_events = json.loads(rust_file.read_text())
    assert len(camp_events) == len(rust_events) == count
    for index, (camp_html, rust_html) in enumerate(zip(camp_events, rust_events), 1):
        assert isinstance(camp_html, str) and isinstance(rust_html, str), index
        camp, rust = StreamWithoutCsrfInputs(), StreamWithoutCsrfInputs()
        camp.feed(camp_html)
        rust.feed(rust_html)
        check_message_times(camp.attributes, f"{camp_file} event {index}")
        check_message_times(rust.attributes, f"{rust_file} event {index}")
        assert rust.tags == camp.tags, f"Message append {index} tag structure differs"
        assert rust.attribute_keys == camp.attribute_keys, f"Message append {index} attribute keys differ"
        assert stable_stream_values(rust.attributes) == stable_stream_values(camp.attributes), f"Message append {index} static attributes differ"
        assert rust.text == camp.text, f"Message append {index} text differs"


def stable_stream_values(attributes):
    values = []
    for tag, attrs in stable_message_attributes(attributes):
        normalized = []
        for key, value in attrs:
            if key == "data-copy-to-clipboard-content-value":
                url = urllib.parse.urlsplit(value)
                assert url.scheme in {"http", "https"} and url.netloc and url.path.startswith("/rooms/1/@"), value
                value = url.path
            normalized.append((key, value))
        values.append((tag, tuple(normalized)))
    return values


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--clients", type=int, default=32)
    parser.add_argument("--seconds", type=float, default=10)
    parser.add_argument("--write-rate", type=float, default=10, help="target message POSTs per second")
    parser.add_argument("--campfire-workers", type=int, default=22)
    parser.add_argument("--sockets", type=int, default=0, help="signed room-stream subscribers; zero disables socket capture")
    parser.add_argument("--rustfire-first", action="store_true")
    parser.add_argument("--sample-dir", type=pathlib.Path, help="save initial and first invalid read responses")
    args = parser.parse_args()
    if args.clients < 1 or args.seconds < 2 or args.write_rate <= 0 or args.campfire_workers < 1 or args.sockets < 0:
        parser.error("clients, seconds, write rate, and Campfire workers must be positive; seconds must be at least 2")
    count = round(args.seconds * args.write_rate)
    if count < 1 or count > 1000:
        parser.error("write count must be 1–1000")
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPOSITORY, text=True).strip() == REVISION
    if args.sample_dir:
        args.sample_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="paired-message-mix-") as scratch:
        temp = pathlib.Path(scratch)
        binary = temp / "checked_get"
        subprocess.run(["go", "build", "-o", str(binary), "bench/checked_get.go"], cwd=ROOT, check=True)
        rust_db, camp_db = temp / "rust.sqlite3", temp / "camp.sqlite3"
        rust_port, camp_port, redis_port = free_port(), free_port(), free_port()
        seed_rustfire(rust_db, rust_port, [])
        camp_env = seed_campfire(REPOSITORY, RUBY, BUNDLE, REPOSITORY / "storage/db/production.sqlite3", camp_db, [], camp_port, temp)
        camp_env["REDIS_URL"] = f"redis://127.0.0.1:{redis_port}"
        camp_env["WEB_CONCURRENCY"] = str(args.campfire_workers)
        seed_messages(rust_db, camp_db)
        for database, rails in ((rust_db, False), (camp_db, True)):
            set_distinct_creation_times(database, rails)
            seed_additional_messages(database, rails, 40)
        with sqlite3.connect(camp_db) as camp, sqlite3.connect(rust_db) as rust:
            camp.execute("DELETE FROM sqlite_sequence WHERE name='messages'")
            camp.execute("INSERT INTO sqlite_sequence(name,seq) VALUES('messages',40)")
            rust.executemany("UPDATE users SET name=?2,updated_at=?3 WHERE id=?1", camp.execute("SELECT id,name,updated_at FROM users WHERE id IN(1,2)").fetchall())
            rust.execute("UPDATE rooms SET name=? WHERE id=1", [camp.execute("SELECT name FROM rooms WHERE id=1").fetchone()[0]])
        redis, redis_log = start_redis(temp, redis_port)
        try:
            def run_rustfire():
                rust = start_server(rust_db, rust_port, {
                    "RUSTFIRE_CAMPFIRE_SECRET_KEY_BASE": camp_env["SECRET_KEY_BASE"],
                    "RUSTFIRE_PUBLIC_URL": "http://127.0.0.1",
                })
                try:
                    first = fetch(rust_port, "session_token=benchmark-session", "/rooms/1/messages")
                    if args.sample_dir:
                        (args.sample_dir / "rustfire-initial.html").write_bytes(first[2])
                    result = measure(binary, rust_port, "session_token=benchmark-session", "benchmark-csrf", args.clients, args.seconds, count, args.sockets, args.sample_dir / "rustfire-invalid.html" if args.sample_dir else None, (args.sample_dir or temp) / "rustfire-stream.html" if args.sockets else None, (args.sample_dir or temp) / "rustfire-events.json" if args.sockets else None)
                    check_final_page(fetch(rust_port, "session_token=benchmark-session", "/rooms/1/messages")[2], count)
                    return first[2], result
                finally:
                    stop_server(rust)

            def run_campfire():
                with open(temp / "puma.log", "w+") as log:
                    camp = subprocess.Popen([str(RUBY), str(RUBY.parent / "bundle"), "exec", "puma", "-C", "config/puma.rb"], cwd=REPOSITORY, env=camp_env, stdout=log, stderr=log)
                    try:
                        wait_for_server(camp_port, camp)
                        cookie, csrf = login_campfire(camp_port)
                        first = fetch(camp_port, cookie, "/rooms/1/messages")
                        if args.sample_dir:
                            (args.sample_dir / "campfire-initial.html").write_bytes(first[2])
                        result = measure(binary, camp_port, cookie, csrf, args.clients, args.seconds, count, args.sockets, args.sample_dir / "campfire-invalid.html" if args.sample_dir else None, (args.sample_dir or temp) / "campfire-stream.html" if args.sockets else None, (args.sample_dir or temp) / "campfire-events.json" if args.sockets else None)
                        check_final_page(fetch(camp_port, cookie, "/rooms/1/messages")[2], count)
                        return first[2], result
                    finally:
                        stop_server(camp)

            if args.rustfire_first:
                (rust_body, rust_result), (camp_body, camp_result) = run_rustfire(), run_campfire()
            else:
                (camp_body, camp_result), (rust_body, rust_result) = run_campfire(), run_rustfire()
        finally:
            redis.terminate()
            redis.wait(timeout=10)
            redis_log.close()
        check_message_markup(camp_body, rust_body, 40)
        if args.sockets:
            check_stream_events((args.sample_dir or temp) / "campfire-events.json", (args.sample_dir or temp) / "rustfire-events.json", count)
        saved_messages(rust_db, count, False)
        saved_messages(camp_db, count, True)
        print("PASS paired mixed message reads and writes, response checks, and saved rows")
        print(json.dumps({"clients": args.clients, "seconds": args.seconds, "write_rate": args.write_rate, "sockets": args.sockets, "campfire_workers": args.campfire_workers, "rustfire_first": args.rustfire_first, "rustfire": rust_result, "campfire": camp_result}, sort_keys=True))


if __name__ == "__main__":
    main()
