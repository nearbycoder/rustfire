"""Compare mixed message traffic across multiple rooms and authenticated users.

Both apps use disposable, aligned SQLite fixtures. Each room has 40 seeded
messages, one scheduled writer, and readers spread across all user sessions.
"""

import argparse
from datetime import datetime, timedelta, timezone
import http.client
import http.cookiejar
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
import urllib.request

from direct_lookup import ROOT, free_port, start_server, stop_server
from message_markup import check_message_markup
from paired_banned_content import BUNDLE, REPOSITORY, REVISION, RUBY, start_redis
from paired_direct_lookup import seed_campfire, seed_rustfire, wait_for_server
from paired_message_cache import fetch, resource_snapshot


def seed_fixture(rust_db, camp_db, rooms, users):
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    stamp = base.isoformat().replace("+00:00", "Z")
    with sqlite3.connect(camp_db) as camp, sqlite3.connect(rust_db) as rust:
        digest = camp.execute("SELECT password_digest FROM users WHERE id=1").fetchone()[0]
        for uid in range(1, users + 1):
            email = "benchmark@example.invalid" if uid == 1 else f"benchmark-{uid}@example.invalid"
            camp.execute("UPDATE users SET email_address=?,password_digest=? WHERE id=?", (email, digest, uid))
            name, updated = camp.execute("SELECT name,updated_at FROM users WHERE id=?", (uid,)).fetchone()
            rust.execute("UPDATE users SET name=?,updated_at=? WHERE id=?", (name, updated, uid))
            if uid > 1:
                rust.execute(
                    "INSERT INTO sessions(user_id,token,csrf_token,created_at,last_active_at) VALUES(?,?,?,?,?)",
                    (uid, f"multi-session-{uid}", f"multi-csrf-{uid}", stamp, stamp),
                )
            if uid > 2:
                camp.execute(
                    "INSERT INTO memberships(room_id,user_id,involvement,created_at,updated_at) VALUES(1,?,'mentions',?,?)",
                    (uid, stamp, stamp),
                )
                rust.execute(
                    "INSERT INTO memberships(room_id,user_id,involvement,created_at) VALUES(1,?,'mentions',?)",
                    (uid, stamp),
                )
        room_name = camp.execute("SELECT name FROM rooms WHERE id=1").fetchone()[0]
        rust.execute("UPDATE rooms SET name=? WHERE id=1", (room_name,))
        for rid in range(2, rooms + 1):
            name = f"Scale Room {rid}"
            camp.execute("INSERT INTO rooms(id,name,type,creator_id,created_at,updated_at) VALUES(?,?,'Rooms::Open',1,?,?)", (rid, name, stamp, stamp))
            rust.execute("INSERT INTO rooms(id,name,type,creator_id,created_at,updated_at) VALUES(?,?,'Rooms::Open',1,?,?)", (rid, name, stamp, stamp))
            for uid in range(1, users + 1):
                camp.execute("INSERT INTO memberships(room_id,user_id,involvement,created_at,updated_at) VALUES(?,?,'mentions',?,?)", (rid, uid, stamp, stamp))
                rust.execute("INSERT INTO memberships(room_id,user_id,involvement,created_at) VALUES(?,?,'mentions',?)", (rid, uid, stamp))
        for rid in range(1, rooms + 1):
            for local in range(1, 41):
                mid = (rid - 1) * 40 + local
                created = base + timedelta(seconds=mid * 60, microseconds=mid * 123)
                camp_time = created.strftime("%Y-%m-%d %H:%M:%S.%f")
                rust_time = created.isoformat().replace("+00:00", "Z")
                nanos = int(created.timestamp()) * 1_000_000_000 + created.microsecond * 1000
                client_id = f"multi-seed-{rid}-{local}"
                body = f"room {rid} seed message {local}"
                camp.execute("INSERT INTO messages(id,room_id,creator_id,client_message_id,created_at,updated_at) VALUES(?,?,?,?,?,?)", (mid, rid, 1, client_id, camp_time, camp_time))
                camp.execute("INSERT INTO action_text_rich_texts(name,body,record_type,record_id,created_at,updated_at) VALUES('body',?,'Message',?,?,?)", (body, mid, camp_time, camp_time))
                rust.execute("INSERT INTO messages(id,room_id,creator_id,body,client_message_id,created_at,created_at_ns,updated_at,updated_at_ns) VALUES(?,?,?,?,?,?,?,?,?)", (mid, rid, 1, body, client_id, rust_time, nanos, rust_time, nanos))
        camp.execute("DELETE FROM sqlite_sequence WHERE name='messages'")
        camp.execute("INSERT INTO sqlite_sequence(name,seq) VALUES('messages',?)", (rooms * 40,))


def campfire_login(port, uid):
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    origin = f"http://127.0.0.1:{port}"
    with opener.open(origin + "/session/new", timeout=20) as response:
        page = response.read().decode()
    token = re.search(r'<meta name="csrf-token" content="([^"]+)', page)
    assert token, "Campfire sign-in CSRF token missing"
    email = "benchmark@example.invalid" if uid == 1 else f"benchmark-{uid}@example.invalid"
    fields = urllib.parse.urlencode({"email_address": email, "password": "benchmark-password", "authenticity_token": token.group(1)}).encode()
    with opener.open(origin + "/session", data=fields, timeout=20) as response:
        assert response.status == 200, (uid, response.status, response.url)
        response.read()
    with opener.open(origin + "/rooms/1", timeout=20) as response:
        room = response.read().decode()
        assert response.status == 200
    token = re.search(r'<meta name="csrf-token" content="([^"]+)', room)
    assert token, "Campfire room CSRF token missing"
    cookie = "; ".join(f"{item.name}={item.value}" for item in jar)
    return cookie, token.group(1)


def writer(port, rid, uid, cookie, csrf, count, seconds, result):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    samples = []
    started = time.monotonic()
    span = max(0.1, seconds - 0.5)
    try:
        for index in range(1, count + 1):
            delay = started + (index - 1) * span / count - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            client_id = f"multi-{rid}-{index}"
            fields = urllib.parse.urlencode({
                "message[body]": f"room {rid} live message {index}",
                "message[client_message_id]": client_id,
                "authenticity_token": csrf,
            })
            begun = time.monotonic()
            connection.request("POST", f"/rooms/{rid}/messages", fields, {
                "Cookie": cookie,
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "text/vnd.turbo-stream.html, text/html",
            })
            response = connection.getresponse()
            body = response.read()
            if response.status != 200 or response.getheader("Content-Type", "").split(";")[0] != "text/vnd.turbo-stream.html" or not re.search(rb"\bid=['\"]message_" + client_id.encode() + rb"['\"]", body):
                raise AssertionError((rid, index, response.status, response.getheader("Content-Type"), body[:200]))
            samples.append((time.monotonic() - begun) * 1000)
        result.update({"room": rid, "user": uid, "writes": len(samples), "latencies": samples, "elapsed_s": time.monotonic() - started})
    except Exception as error:
        result["error"] = repr(error)
    finally:
        connection.close()


def measure(binary, port, identities, rooms, users, clients, seconds, count, directory, label, sockets_per_room, resource_pids=()):
    targets = [{"path": f"/rooms/{rid}/messages", "cookie": identities[uid][0]} for rid in range(1, rooms + 1) for uid in range(1, users + 1)]
    targets_file = directory / f"{label}-targets.json"
    targets_file.write_text(json.dumps(targets))
    command = [str(binary), "--base", f"http://127.0.0.1:{port}", "--targets-file", str(targets_file), "--expected-status", "200", "--expected-content-type", "text/html", "--expected-message-count", "40", "--accept", "text/html", "--clients", str(clients), "--seconds", str(seconds), "--signal-start"]
    captures = []
    threads = []
    writes = []
    reader = None
    resource_stop = threading.Event()
    resource_samples = []
    sampler = None

    def sample_resources():
        while not resource_stop.wait(0.5):
            resource_samples.append(resource_snapshot(resource_pids))

    try:
        if sockets_per_room:
            for rid in range(1, rooms + 1):
                uid = (rid - 1) % users + 1
                events_file = directory / f"{label}-room-{rid}-events.json"
                capture_command = ["node", "bench/capture_message_appends.mjs", "--base", f"http://127.0.0.1:{port}", "--cookie", identities[uid][0], "--room", str(rid), "--client-prefix", f"multi-{rid}", "--first-id", "0", "--sockets", str(sockets_per_room), "--messages", str(count), "--timeout", str(round((seconds + 60) * 1000)), "--events-file", str(events_file)]
                capture = subprocess.Popen(capture_command, cwd=ROOT, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                captures.append((rid, capture))
                ready, _, _ = select.select([capture.stdout], [], [], 60)
                marker = capture.stdout.readline().strip() if ready else ""
                if marker != "READY":
                    capture.kill()
                    stdout, stderr = capture.communicate()
                    raise AssertionError(f"Room {rid} socket capture did not start: {marker}\n{stdout}\n{stderr}")
        reader = subprocess.Popen(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        ready, _, _ = select.select([reader.stderr], [], [], max(90, seconds + 60))
        marker = reader.stderr.readline().strip() if ready else ""
        assert marker == "MEASURE_START", f"Reader did not start: {marker}"
        if resource_pids:
            resource_samples.append(resource_snapshot(resource_pids))
            sampler = threading.Thread(target=sample_resources, daemon=True)
            sampler.start()
        for rid in range(1, rooms + 1):
            uid = (rid - 1) % users + 1
            cookie, csrf = identities[uid]
            result = {}
            thread = threading.Thread(target=writer, args=(port, rid, uid, cookie, csrf, count, seconds, result))
            writes.append(result)
            threads.append(thread)
            thread.start()
        stdout, stderr = reader.communicate(timeout=max(90, seconds + 60))
        for thread in threads:
            thread.join(timeout=45)
        if sampler:
            resource_stop.set()
            sampler.join()
            resource_samples.append(resource_snapshot(resource_pids))
        assert all(not thread.is_alive() for thread in threads), "Writer did not finish"
        assert all("error" not in item and item.get("writes") == count for item in writes), writes
        assert all(item["elapsed_s"] <= seconds for item in writes), ("Writes ran past the measured read interval", writes)
        report = json.loads(stdout.strip().splitlines()[-1])
        assert reader.returncode == 0 and report["errors"] == 0, (report, stderr)
        latencies = sorted(sample for item in writes for sample in item["latencies"])
        output = {"reads": report, "writes": {"count": len(latencies), "p95_ms": latencies[int(.95 * (len(latencies) - 1))], "max_elapsed_s": max(item["elapsed_s"] for item in writes), "per_room": [{"room": item["room"], "user": item["user"], "count": item["writes"]} for item in writes]}}
        if captures:
            deliveries = []
            for rid, capture in captures:
                stdout, stderr = capture.communicate(timeout=max(90, seconds + 75))
                delivery = json.loads(stdout.strip().splitlines()[-1])
                assert capture.returncode == 0 and delivery["missed"] == delivery["unexpected"] == delivery["closed_early"] == 0, (rid, delivery, stderr)
                deliveries.append({"room": rid, **delivery})
            output["sockets"] = {"per_room": deliveries, "expected": sum(item["expected"] for item in deliveries), "received": sum(item["received"] for item in deliveries)}
        if resource_samples:
            output["resources"] = {
                "server_cpu_seconds": round(resource_samples[-1][1] - resource_samples[0][1], 3),
                "server_peak_pss_mib": round(max(item[0] for item in resource_samples) / 1048576, 2),
                "server_processes_peak": max(item[2] for item in resource_samples),
            }
        return output
    finally:
        if sampler and sampler.is_alive():
            resource_stop.set()
            sampler.join()
        if reader and reader.poll() is None:
            reader.kill()
            reader.communicate()
        for _, capture in captures:
            if capture.poll() is None:
                capture.kill()
                capture.communicate()
        for thread in threads:
            thread.join(timeout=1)


def check_saved(database, rooms, users, count, rails):
    with sqlite3.connect(database) as db:
        for rid in range(1, rooms + 1):
            if rails:
                rows = db.execute("SELECT m.client_message_id,m.creator_id,t.body FROM messages m JOIN action_text_rich_texts t ON t.record_type='Message' AND t.record_id=m.id AND t.name='body' WHERE m.room_id=? AND m.client_message_id LIKE 'multi-%' ORDER BY m.id", (rid,)).fetchall()
            else:
                rows = db.execute("SELECT client_message_id,creator_id,body FROM messages WHERE room_id=? AND client_message_id LIKE 'multi-%' ORDER BY id", (rid,)).fetchall()
            live = [row for row in rows if row[0].startswith(f"multi-{rid}-")]
            assert len(live) == count, (rid, len(live), count)
            for index, (client_id, creator, body) in enumerate(live, 1):
                assert client_id == f"multi-{rid}-{index}" and creator == (rid - 1) % users + 1 and f"room {rid} live message {index}" in body, (rid, index, client_id, creator, body)


def check_final_pages(port, cookie, database, rooms, rails):
    with sqlite3.connect(database) as db:
        for rid in range(1, rooms + 1):
            status, _, body = fetch(port, cookie, f"/rooms/{rid}/messages")
            assert status == 200
            actual = [int(value) for value in re.findall(rb"\bdata-message-id=['\"](\d+)['\"]", body)]
            order = "created_at" if rails else "created_at_ns"
            newest = [row[0] for row in db.execute(f"SELECT id FROM messages WHERE room_id=? ORDER BY {order} DESC,id DESC LIMIT 40", (rid,))]
            assert actual == list(reversed(newest)), (rid, actual, newest)


def check_socket_events(database, directory, label, rooms, count):
    with sqlite3.connect(database) as db:
        for rid in range(1, rooms + 1):
            events = json.loads((directory / f"{label}-room-{rid}-events.json").read_text())
            assert len(events) == count, (label, rid, len(events), count)
            for index, event in enumerate(events, 1):
                assert isinstance(event, str)
                matched = re.search(rb"\bdata-message-id=['\"](\d+)['\"]", event.encode())
                assert matched, (label, rid, index)
                expected = db.execute("SELECT id FROM messages WHERE room_id=? AND client_message_id=?", (rid, f"multi-{rid}-{index}")).fetchone()
                assert expected and int(matched.group(1)) == expected[0], (label, rid, index, matched.group(1), expected)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rooms", type=int, default=4)
    parser.add_argument("--users", type=int, default=4)
    parser.add_argument("--clients", type=int, default=32)
    parser.add_argument("--seconds", type=float, default=10)
    parser.add_argument("--write-rate", type=float, default=5, help="scheduled writes per second per room")
    parser.add_argument("--campfire-workers", type=int, default=22)
    parser.add_argument("--sockets-per-room", type=int, default=0, help="signed message-stream subscribers per room")
    parser.add_argument("--resources", action="store_true", help="sample server CPU time and peak PSS during measured reads and writes (requires psutil)")
    parser.add_argument("--rustfire-first", action="store_true")
    args = parser.parse_args()
    if not (2 <= args.rooms <= 10 and 2 <= args.users <= 10 and args.clients >= args.rooms * args.users and args.seconds >= 2 and args.write_rate > 0 and args.campfire_workers > 0 and args.sockets_per_room >= 0):
        parser.error("use 2–10 rooms/users, at least one reader per room/user pair, seconds >= 2, and positive write rate/workers")
    count = round(args.seconds * args.write_rate)
    if not 1 <= count <= 1000:
        parser.error("writes per room must be 1–1000")
    if args.resources:
        try:
            import psutil  # noqa: F401
        except ImportError:
            parser.error("--resources requires the psutil Python package")
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPOSITORY, text=True).strip() == REVISION
    with tempfile.TemporaryDirectory(prefix="paired-message-multi-") as scratch:
        temp = pathlib.Path(scratch)
        binary = temp / "checked_get"
        subprocess.run(["go", "build", "-o", str(binary), "bench/checked_get.go"], cwd=ROOT, check=True)
        rust_db, camp_db = temp / "rust.sqlite3", temp / "camp.sqlite3"
        rust_port, camp_port, redis_port = free_port(), free_port(), free_port()
        seed_rustfire(rust_db, rust_port, [])
        camp_env = seed_campfire(REPOSITORY, RUBY, BUNDLE, REPOSITORY / "storage/db/production.sqlite3", camp_db, [], camp_port, temp)
        camp_env["REDIS_URL"] = f"redis://127.0.0.1:{redis_port}"
        camp_env["WEB_CONCURRENCY"] = str(args.campfire_workers)
        seed_fixture(rust_db, camp_db, args.rooms, args.users)
        redis, redis_log = start_redis(temp, redis_port)
        try:
            def run_rustfire():
                process = start_server(rust_db, rust_port, {"RUSTFIRE_CAMPFIRE_SECRET_KEY_BASE": camp_env["SECRET_KEY_BASE"], "RUSTFIRE_PUBLIC_URL": "http://127.0.0.1"})
                try:
                    identities = {uid: ("session_token=benchmark-session", "benchmark-csrf") if uid == 1 else (f"session_token=multi-session-{uid}", f"multi-csrf-{uid}") for uid in range(1, args.users + 1)}
                    pages = [fetch(rust_port, identities[1][0], f"/rooms/{rid}/messages")[2] for rid in range(1, args.rooms + 1)]
                    result = measure(binary, rust_port, identities, args.rooms, args.users, args.clients, args.seconds, count, temp, "rustfire", args.sockets_per_room, (process.pid,) if args.resources else ())
                    check_final_pages(rust_port, identities[1][0], rust_db, args.rooms, False)
                    if args.sockets_per_room:
                        check_socket_events(rust_db, temp, "rustfire", args.rooms, count)
                    return pages, result
                finally:
                    stop_server(process)

            def run_campfire():
                with open(temp / "puma.log", "w+") as log:
                    process = subprocess.Popen([str(RUBY), str(RUBY.parent / "bundle"), "exec", "puma", "-C", "config/puma.rb"], cwd=REPOSITORY, env=camp_env, stdout=log, stderr=log)
                    try:
                        wait_for_server(camp_port, process)
                        identities = {uid: campfire_login(camp_port, uid) for uid in range(1, args.users + 1)}
                        pages = [fetch(camp_port, identities[1][0], f"/rooms/{rid}/messages")[2] for rid in range(1, args.rooms + 1)]
                        result = measure(binary, camp_port, identities, args.rooms, args.users, args.clients, args.seconds, count, temp, "campfire", args.sockets_per_room, (process.pid, redis.pid) if args.resources else ())
                        check_final_pages(camp_port, identities[1][0], camp_db, args.rooms, True)
                        if args.sockets_per_room:
                            check_socket_events(camp_db, temp, "campfire", args.rooms, count)
                        return pages, result
                    except Exception:
                        log.flush()
                        log.seek(0)
                        print(log.read()[-3000:])
                        raise
                    finally:
                        stop_server(process)

            if args.rustfire_first:
                (rust_pages, rust_result), (camp_pages, camp_result) = run_rustfire(), run_campfire()
            else:
                (camp_pages, camp_result), (rust_pages, rust_result) = run_campfire(), run_rustfire()
        finally:
            redis.terminate()
            redis.wait(timeout=10)
            redis_log.close()
        for camp_page, rust_page in zip(camp_pages, rust_pages):
            check_message_markup(camp_page, rust_page, 40)
        check_saved(rust_db, args.rooms, args.users, count, False)
        check_saved(camp_db, args.rooms, args.users, count, True)
        print("PASS paired multi-room, multi-user mixed message workload")
        print(json.dumps({"rooms": args.rooms, "users": args.users, "clients": args.clients, "seconds": args.seconds, "write_rate_per_room": args.write_rate, "sockets_per_room": args.sockets_per_room, "campfire_workers": args.campfire_workers, "rustfire_first": args.rustfire_first, "rustfire": rust_result, "campfire": camp_result}, sort_keys=True))


if __name__ == "__main__":
    main()
