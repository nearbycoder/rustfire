"""Measure checked room reads during repeated, equivalent JPEG uploads."""

import argparse
import hashlib
import http.client
import json
import pathlib
import select
import sqlite3
import subprocess
import tempfile
import threading
import time

from direct_lookup import ROOT, free_port, start_server, stop_server
from message_markup import check_message_markup
from paired_banned_content import BUNDLE, REPOSITORY, REVISION, RUBY, start_redis
from paired_bot_admin import cleanup_campfire_uploads
from paired_direct_lookup import login_campfire, seed_campfire, seed_rustfire, wait_for_server
from paired_message_cache import fetch, seed_additional_messages, set_distinct_creation_times
from paired_message_mix import check_final_page
from paired_room_refresh import seed_messages
from paired_room_shell import section


def writer(port, cookie, csrf, image, count, seconds, result):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=120)
    samples = []
    started = time.monotonic()
    span = max(0.1, seconds - 0.5)
    try:
        for index in range(1, count + 1):
            delay = started + (index - 1) * span / count - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            client_id = f"image-mixed-{index}"
            boundary = f"rustfire-image-mixed-{index}"
            payload = (
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"authenticity_token\"\r\n\r\n{csrf}\r\n"
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"message[client_message_id]\"\r\n\r\n{client_id}\r\n"
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"message[attachment]\"; filename=\"black_hole.jpg\"\r\nContent-Type: image/jpeg\r\n\r\n"
            ).encode() + image + f"\r\n--{boundary}--\r\n".encode()
            begun = time.monotonic()
            connection.request("POST", "/rooms/1/messages", payload, {
                "Cookie": cookie,
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "Accept": "text/vnd.turbo-stream.html, text/html",
                "X-CSRF-Token": csrf,
            })
            response = connection.getresponse()
            body = response.read()
            if response.status != 200 or not response.getheader("Content-Type", "").startswith("text/vnd.turbo-stream.html") or f"message_{client_id}".encode() not in body:
                raise AssertionError((index, response.status, response.getheader("Content-Type"), body[:150]))
            samples.append((time.monotonic() - begun) * 1000)
        result.update({"count": len(samples), "p95_ms": sorted(samples)[int(.95 * (len(samples) - 1))], "max_ms": max(samples), "elapsed_s": time.monotonic() - started})
    except Exception as error:
        result["error"] = repr(error)
    finally:
        connection.close()


def measure(binary, port, cookie, csrf, image, clients, seconds, count, sockets=0, events_file=None):
    capture = None
    if sockets:
        capture_command = ["node", "bench/capture_message_appends.mjs", "--base", f"http://127.0.0.1:{port}",
                           "--cookie", cookie, "--room", "1", "--client-prefix", "image-mixed", "--first-id", "41",
                           "--sockets", str(sockets), "--messages", str(count),
                           "--timeout", str(round((seconds + 60) * 1000)), "--events-file", str(events_file)]
        capture = subprocess.Popen(capture_command, cwd=ROOT, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        ready, _, _ = select.select([capture.stdout], [], [], 60)
        marker = capture.stdout.readline().strip() if ready else ""
        if marker != "READY":
            capture.kill()
            stdout, stderr = capture.communicate()
            raise AssertionError(f"Image socket capture did not start: {marker}\n{stdout}\n{stderr}")
    command = [str(binary), "--base", f"http://127.0.0.1:{port}", "--path", "/rooms/1/messages", "--cookie", cookie,
               "--expected-status", "200", "--expected-content-type", "text/html", "--expected-message-count", "40",
               "--accept", "text/html", "--clients", str(clients), "--seconds", str(seconds), "--signal-start"]
    reader = subprocess.Popen(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    thread = None
    writes = {}
    try:
        ready, _, _ = select.select([reader.stderr], [], [], max(90, seconds + 60))
        marker = reader.stderr.readline().strip() if ready else ""
        assert marker == "MEASURE_START", f"Reader did not start: {marker}"
        thread = threading.Thread(target=writer, args=(port, cookie, csrf, image, count, seconds, writes))
        thread.start()
        stdout, stderr = reader.communicate(timeout=max(90, seconds + 60))
        thread.join(timeout=max(90, seconds + 60))
        assert not thread.is_alive() and "error" not in writes and writes.get("count") == count, (writes, stderr)
        report = json.loads(stdout.strip().splitlines()[-1])
        assert reader.returncode == 0 and report["errors"] == 0, (report, stderr)
        writes["deadline_met"] = writes["elapsed_s"] <= seconds
        result = {"reads": report, "writes": writes}
        if capture:
            stdout, stderr = capture.communicate(timeout=max(90, seconds + 75))
            delivery = json.loads(stdout.strip().splitlines()[-1])
            assert capture.returncode == 0 and delivery["missed"] == delivery["unexpected"] == delivery["closed_early"] == 0, (delivery, stderr)
            result["sockets"] = delivery
        return result
    finally:
        if reader.poll() is None:
            reader.kill()
            reader.communicate()
        if thread:
            thread.join(timeout=1)
        if capture and capture.poll() is None:
            capture.kill()
            capture.communicate()


def check_uploads(database, upload_directory, count, image, rails):
    with sqlite3.connect(database) as db:
        if rails:
            rows = db.execute("""SELECT m.id,m.client_message_id,b.filename,b.key,b.byte_size
                FROM messages m JOIN active_storage_attachments a ON a.record_type='Message' AND a.record_id=m.id AND a.name='attachment'
                JOIN active_storage_blobs b ON b.id=a.blob_id WHERE m.id>40 ORDER BY m.id""").fetchall()
        else:
            rows = db.execute("""SELECT m.id,m.client_message_id,a.filename,a.stored_name,NULL
                FROM messages m JOIN attachments a ON a.message_id=m.id WHERE m.id>40 ORDER BY m.id""").fetchall()
    assert len(rows) == count, (len(rows), count)
    digest = hashlib.sha256(image).digest()
    for index, (message_id, client_id, filename, key, size) in enumerate(rows, 1):
        assert message_id == 40 + index and client_id == f"image-mixed-{index}" and filename == "black_hole.jpg", rows[index - 1]
        if size is not None:
            assert size == len(image), (index, size, len(image))
        path = upload_directory / key[:2] / key[2:4] / key if rails else upload_directory / key
        assert hashlib.sha256(path.read_bytes()).digest() == digest, (index, path)


def message_tokens(body):
    return section(b'<div id="message-area">' + body + b'</div>', "message-area",
                   normalize_times=True, ignore_csrf_inputs=True, normalize_blob_paths=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--clients", type=int, default=32)
    parser.add_argument("--seconds", type=float, default=10)
    parser.add_argument("--write-rate", type=float, default=1)
    parser.add_argument("--campfire-workers", type=int, default=22)
    parser.add_argument("--sockets", type=int, default=0)
    parser.add_argument("--rustfire-first", action="store_true")
    args = parser.parse_args()
    if args.clients < 1 or args.seconds < 2 or args.write_rate <= 0 or args.campfire_workers < 1 or args.sockets < 0:
        parser.error("clients, seconds, write rate, and workers must be positive; seconds >= 2; sockets >= 0")
    count = round(args.seconds * args.write_rate)
    if not 1 <= count <= 100:
        parser.error("use 1–100 JPEG writes per trial")
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPOSITORY, text=True).strip() == REVISION
    image = (REPOSITORY / "test/fixtures/files/black_hole.jpg").read_bytes()
    with tempfile.TemporaryDirectory(prefix="paired-image-mix-") as scratch:
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
                process = start_server(rust_db, rust_port, {"RUSTFIRE_CAMPFIRE_SECRET_KEY_BASE": camp_env["SECRET_KEY_BASE"],
                    "RUSTFIRE_PUBLIC_URL": "http://127.0.0.1", "RUSTFIRE_UPLOAD_DIR": str(temp / "rust-uploads")})
                try:
                    first = fetch(rust_port, "session_token=benchmark-session", "/rooms/1/messages")[2]
                    result = measure(binary, rust_port, "session_token=benchmark-session", "benchmark-csrf", image,
                                     args.clients, args.seconds, count, args.sockets, temp / "rust-events.json" if args.sockets else None)
                    final = fetch(rust_port, "session_token=benchmark-session", "/rooms/1/messages")[2]
                    check_final_page(final, count)
                    check_uploads(rust_db, temp / "rust-uploads", count, image, False)
                    return first, final, result
                finally:
                    stop_server(process)

            def run_campfire():
                with open(temp / "puma.log", "w+") as log:
                    process = subprocess.Popen([str(RUBY), str(RUBY.parent / "bundle"), "exec", "puma", "-C", "config/puma.rb"], cwd=REPOSITORY, env=camp_env, stdout=log, stderr=log)
                    try:
                        wait_for_server(camp_port, process)
                        cookie, csrf = login_campfire(camp_port)
                        first = fetch(camp_port, cookie, "/rooms/1/messages")[2]
                        result = measure(binary, camp_port, cookie, csrf, image, args.clients, args.seconds, count,
                                         args.sockets, temp / "camp-events.json" if args.sockets else None)
                        final = fetch(camp_port, cookie, "/rooms/1/messages")[2]
                        check_final_page(final, count)
                        check_uploads(camp_db, REPOSITORY / "storage/files", count, image, True)
                        return first, final, result
                    except Exception:
                        log.flush()
                        log.seek(0)
                        print(log.read()[-3000:])
                        raise
                    finally:
                        stop_server(process)
                        cleanup_campfire_uploads(REPOSITORY / "storage/db/production.sqlite3", camp_db, REPOSITORY)

            if args.rustfire_first:
                (rust_initial, rust_final, rust_result), (camp_initial, camp_final, camp_result) = run_rustfire(), run_campfire()
            else:
                (camp_initial, camp_final, camp_result), (rust_initial, rust_final, rust_result) = run_campfire(), run_rustfire()
        finally:
            redis.terminate()
            redis.wait(timeout=10)
            redis_log.close()
        check_message_markup(camp_initial, rust_initial, 40)
        camp_tokens = message_tokens(camp_final)
        rust_tokens = message_tokens(rust_final)
        assert camp_tokens == rust_tokens, next(
            ((index, left, right) for index, (left, right) in enumerate(zip(camp_tokens, rust_tokens)) if left != right),
            (len(camp_tokens), len(rust_tokens)),
        )
        if args.sockets:
            for label in ("camp", "rust"):
                events = json.loads((temp / f"{label}-events.json").read_text())
                assert len(events) == count and all(isinstance(event, str) and f"message_image-mixed-{index}" in event for index, event in enumerate(events, 1)), label
            camp_events = json.loads((temp / "camp-events.json").read_text())
            rust_events = json.loads((temp / "rust-events.json").read_text())
            for index, (camp_event, rust_event) in enumerate(zip(camp_events, rust_events), 1):
                left, right = message_tokens(camp_event.encode()), message_tokens(rust_event.encode())
                assert left == right, (index, next(((pos, a, b) for pos, (a, b) in enumerate(zip(left, right)) if a != b), (len(left), len(right))))
        passed = camp_result["writes"]["deadline_met"] and rust_result["writes"]["deadline_met"]
        print("PASS paired JPEG upload/read mix" if passed else "FAIL JPEG writer exceeded read interval")
        print(json.dumps({"clients": args.clients, "seconds": args.seconds, "write_rate": args.write_rate,
            "writes": count, "image_bytes": len(image), "parsed_page_tokens": len(camp_tokens), "sockets": args.sockets,
            "campfire_workers": args.campfire_workers, "rustfire_first": args.rustfire_first,
            "rustfire": rust_result, "campfire": camp_result}, sort_keys=True))
        if not passed:
            raise SystemExit(1)


if __name__ == "__main__":
    main()
