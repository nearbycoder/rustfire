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
from paired_message_mix import StreamWithoutCsrfInputs
from paired_turbo_fanout import check_message_times, stable_message_attributes


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


def message_body(rid, index, rich_writes):
    text = f"room {rid} live message {index}"
    if not rich_writes:
        return text
    variants = [
        f"<div><strong>{text}</strong> <a class='sample-link' href='https://example.com/{rid}/{index}'>link</a></div>",
        f"<div>{text} <span class='sample'>safe</span> <a href='javascript:alert(1)'>unsafe</a></div>",
        f"<div>{text}<ul><li>one</li><li><code>two</code></li></ul></div>",
        f"<div>{text} <time datetime='2024-01-01'>date</time></div>",
        f"<div>{text} <img src='https://example.com/untrusted.svg'>after image</div>",
    ]
    return variants[(index - 1) % len(variants)]


def writer(port, rid, uid, cookie, csrf, count, seconds, result, rich_writes):
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
                "message[body]": message_body(rid, index, rich_writes),
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
        result.update({"room": rid, "user": uid, "writes": len(samples), "latencies": samples, "elapsed_s": time.monotonic() - started, "error": repr(error)})
    finally:
        connection.close()


def event_file(directory, label, room_id, user_id):
    return directory / f"{label}-room-{room_id}-user-{user_id}-events.json"


def socket_groups(rooms, users, sockets_per_room, socket_users_per_room):
    if not sockets_per_room:
        return []
    group_size = sockets_per_room // socket_users_per_room
    return [
        (rid, (rid - 1 + offset) % users + 1, group_size)
        for rid in range(1, rooms + 1)
        for offset in range(socket_users_per_room)
    ]


def measure(binary, port, identities, rooms, users, clients, seconds, count, directory, event_dir, label, sockets_per_room, socket_users_per_room=1, browser_channels=False, resource_pids=(), rich_writes=False):
    targets = [{"path": f"/rooms/{rid}/messages", "cookie": identities[uid][0]} for rid in range(1, rooms + 1) for uid in range(1, users + 1)]
    targets_file = directory / f"{label}-targets.json"
    targets_file.write_text(json.dumps(targets))
    command = [str(binary), "--base", f"http://127.0.0.1:{port}", "--targets-file", str(targets_file), "--expected-status", "200", "--expected-content-type", "text/html", "--expected-message-count", "40", "--accept", "text/html", "--clients", str(clients), "--seconds", str(seconds), "--signal-start", "--invalid-sample", str(event_dir / f"{label}-invalid-read.html")]
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
            for rid, uid, group_size in socket_groups(rooms, users, sockets_per_room, socket_users_per_room):
                events_file = event_file(event_dir, label, rid, uid)
                capture_command = ["node", "bench/capture_message_appends.mjs", "--base", f"http://127.0.0.1:{port}", "--cookie", identities[uid][0], "--room", str(rid), "--client-prefix", f"multi-{rid}", "--first-id", "0", "--sockets", str(group_size), "--messages", str(count), "--timeout", str(round((seconds + 60) * 1000)), "--await-start", "1", "--events-file", str(events_file)]
                if browser_channels:
                    capture_command.extend(("--browser-channels", "1"))
                capture = subprocess.Popen(capture_command, cwd=ROOT, text=True, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                captures.append((rid, uid, capture))
                ready, _, _ = select.select([capture.stdout], [], [], 60)
                marker = capture.stdout.readline().strip() if ready else ""
                if marker != "READY":
                    capture.kill()
                    stdout, stderr = capture.communicate()
                    raise AssertionError(f"Room {rid} user {uid} socket capture did not start: {marker}\n{stdout}\n{stderr}")
        reader = subprocess.Popen(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        ready, _, _ = select.select([reader.stderr], [], [], max(90, seconds + 60))
        marker = reader.stderr.readline().strip() if ready else ""
        assert marker == "MEASURE_START", f"Reader did not start: {marker}"
        for _, _, capture in captures:
            capture.stdin.write("START\n")
            capture.stdin.flush()
            capture.stdin.close()
            capture.stdin = None
        if resource_pids:
            resource_samples.append(resource_snapshot(resource_pids))
            sampler = threading.Thread(target=sample_resources, daemon=True)
            sampler.start()
        for rid in range(1, rooms + 1):
            uid = (rid - 1) % users + 1
            cookie, csrf = identities[uid]
            result = {}
            thread = threading.Thread(target=writer, args=(port, rid, uid, cookie, csrf, count, seconds, result, rich_writes))
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
        writers_complete = all(not thread.is_alive() for thread in threads) and all("error" not in item and item.get("writes") == count for item in writes)
        deadline_met = writers_complete and all(item["elapsed_s"] <= seconds for item in writes)
        report = json.loads(stdout.strip().splitlines()[-1])
        (event_dir / f"{label}-reader.json").write_text(json.dumps({"report": report, "stderr": stderr, "reader_exit_code": reader.returncode}, indent=2))
        reads_complete = reader.returncode == 0 and report["errors"] == 0
        latencies = sorted(sample for item in writes for sample in item.get("latencies", []))
        output = {"reads": report, "reads_complete": reads_complete, "reader_exit_code": reader.returncode, "writes": {"count": len(latencies), "p95_ms": latencies[int(.95 * (len(latencies) - 1))] if latencies else None, "max_elapsed_s": max((item.get("elapsed_s", 0) for item in writes), default=0), "complete": writers_complete, "deadline_met": deadline_met, "per_room": [{"room": item.get("room", rid), "user": item.get("user"), "count": item.get("writes", 0), "elapsed_s": round(item.get("elapsed_s", 0), 3), **({"error": item["error"]} if "error" in item else {})} for rid, item in enumerate(writes, 1)]}}
        if captures:
            deliveries = []
            for rid, uid, capture in captures:
                try:
                    capture_stdout, _ = capture.communicate(timeout=max(90, seconds + 75))
                    delivery = json.loads(capture_stdout.strip().splitlines()[-1])
                    deliveries.append({"room": rid, "user": uid, "capture_exit_code": capture.returncode, **delivery})
                except (subprocess.TimeoutExpired, IndexError, json.JSONDecodeError) as error:
                    if capture.poll() is None:
                        capture.kill()
                        capture.communicate()
                    expected = sockets_per_room // socket_users_per_room * count
                    deliveries.append({"room": rid, "user": uid, "capture_exit_code": capture.returncode, "expected": expected, "received": None, "missed": None, "unexpected": None, "closed_early": None, "error": repr(error)})
            complete = all(item["capture_exit_code"] == 0 and item.get("missed") == item.get("unexpected") == item.get("closed_early") == 0 for item in deliveries)
            output["sockets"] = {"per_room": deliveries, "expected": sum(item["expected"] for item in deliveries), "received": sum(item["received"] for item in deliveries if item["received"] is not None), "unreported_groups": sum(item["received"] is None for item in deliveries), "complete": complete}
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
        for _, _, capture in captures:
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


def check_socket_events(database, directory, label, groups, count):
    with sqlite3.connect(database) as db:
        for rid, uid, _ in groups:
            events = json.loads(event_file(directory, label, rid, uid).read_text())
            assert len(events) == count, (label, rid, uid, len(events), count)
            for index, event in enumerate(events, 1):
                assert isinstance(event, str)
                matched = re.search(rb"\bdata-message-id=['\"](\d+)['\"]", event.encode())
                assert matched, (label, rid, uid, index)
                expected = db.execute("SELECT id FROM messages WHERE room_id=? AND client_message_id=?", (rid, f"multi-{rid}-{index}")).fetchone()
                assert expected and int(matched.group(1)) == expected[0], (label, rid, uid, index, matched.group(1), expected)


def stable_multi_stream_values(attributes, message_id, room_id):
    values = []
    for tag, attrs in stable_message_attributes(attributes):
        normalized = []
        for key, value in attrs:
            if key == "data-message-id":
                assert value == str(message_id), (key, value, message_id)
                value = "<message-id>"
            elif key == "data-copy-to-clipboard-content-value":
                url = urllib.parse.urlsplit(value)
                assert url.scheme in {"http", "https"} and url.netloc and url.path == f"/rooms/{room_id}/@{message_id}", value
                value = f"/rooms/{room_id}/@<message-id>"
            elif key in {"action", "href"}:
                value = re.sub(rf"(?<=/messages/){message_id}(?=/|$)", "<message-id>", value)
                value = re.sub(rf"(?<=/@){message_id}(?=/|$)", "<message-id>", value)
            normalized.append((key, value))
        values.append((tag, tuple(normalized)))
    return values


def check_paired_socket_markup(camp_db, rust_db, directory, groups, count):
    with sqlite3.connect(camp_db) as camp, sqlite3.connect(rust_db) as rust:
        for rid, uid, _ in groups:
            camp_file = event_file(directory, "campfire", rid, uid)
            rust_file = event_file(directory, "rustfire", rid, uid)
            camp_events = json.loads(camp_file.read_text())
            rust_events = json.loads(rust_file.read_text())
            assert len(camp_events) == len(rust_events) == count, rid
            for index, (camp_html, rust_html) in enumerate(zip(camp_events, rust_events), 1):
                camp_id = camp.execute("SELECT id FROM messages WHERE room_id=? AND client_message_id=?", (rid, f"multi-{rid}-{index}")).fetchone()[0]
                rust_id = rust.execute("SELECT id FROM messages WHERE room_id=? AND client_message_id=?", (rid, f"multi-{rid}-{index}")).fetchone()[0]
                camp_tags, rust_tags = StreamWithoutCsrfInputs(), StreamWithoutCsrfInputs()
                camp_tags.feed(camp_html)
                rust_tags.feed(rust_html)
                check_message_times(camp_tags.attributes, f"{camp_file} event {index}")
                check_message_times(rust_tags.attributes, f"{rust_file} event {index}")
                location = f"room {rid} user {uid} append {index}"
                assert camp_tags.tags == rust_tags.tags, f"{location} tag structure differs"
                assert camp_tags.attribute_keys == rust_tags.attribute_keys, f"{location} attribute keys differ"
                assert stable_multi_stream_values(camp_tags.attributes, camp_id, rid) == stable_multi_stream_values(rust_tags.attributes, rust_id, rid), f"{location} static attributes differ"
                assert camp_tags.text == rust_tags.text, f"{location} text differs"
    return len(groups) * count


def record_validation(result, name, check, *args):
    try:
        check(*args)
    except Exception as error:
        result.setdefault("validation_errors", []).append(f"{name}: {error!r}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rooms", type=int, default=4)
    parser.add_argument("--users", type=int, default=4)
    parser.add_argument("--clients", type=int, default=32)
    parser.add_argument("--seconds", type=float, default=10)
    parser.add_argument("--write-rate", type=float, default=5, help="scheduled writes per second per room")
    parser.add_argument("--campfire-workers", type=int, default=22)
    parser.add_argument("--sockets-per-room", type=int, default=0, help="signed message-stream subscribers per room")
    parser.add_argument("--socket-users-per-room", type=int, default=1, help="distinct authenticated socket users per room; must divide sockets per room")
    parser.add_argument("--browser-channels", action="store_true", help="also subscribe each socket to the page's presence, read, unread, typing, heartbeat, and signed sidebar channels")
    parser.add_argument("--rich-writes", action="store_true", help="cycle formatted, linked, listed, and filtered message bodies in each room")
    parser.add_argument("--resources", action="store_true", help="sample server CPU time and peak PSS during measured reads and writes (requires psutil)")
    parser.add_argument("--rustfire-first", action="store_true")
    parser.add_argument("--only-rustfire", action="store_true", help="run just Rustfire on the matched fixture to probe a load point")
    parser.add_argument("--only-campfire", action="store_true", help="run just Campfire on the matched fixture to diagnose a load point")
    parser.add_argument("--sample-dir", type=pathlib.Path, help="retain captured room append events from both apps")
    args = parser.parse_args()
    if args.only_rustfire and args.only_campfire:
        parser.error("choose at most one single-app mode")
    if not (2 <= args.rooms <= 10 and 2 <= args.users <= 10 and args.clients >= args.rooms * args.users and args.seconds >= 2 and args.write_rate > 0 and args.campfire_workers > 0 and args.sockets_per_room >= 0):
        parser.error("use 2–10 rooms/users, at least one reader per room/user pair, seconds >= 2, and positive write rate/workers")
    if args.browser_channels and not args.sockets_per_room:
        parser.error("--browser-channels requires --sockets-per-room")
    if not 1 <= args.socket_users_per_room <= args.users or args.sockets_per_room and args.sockets_per_room % args.socket_users_per_room:
        parser.error("--socket-users-per-room must be 1–users and divide --sockets-per-room")
    if args.socket_users_per_room > 1 and not args.sockets_per_room:
        parser.error("--socket-users-per-room requires --sockets-per-room")
    count = round(args.seconds * args.write_rate)
    groups = socket_groups(args.rooms, args.users, args.sockets_per_room, args.socket_users_per_room)
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
        event_dir = args.sample_dir.resolve() if args.sample_dir else temp
        event_dir.mkdir(parents=True, exist_ok=True)
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
                    result = measure(binary, rust_port, identities, args.rooms, args.users, args.clients, args.seconds, count, temp, event_dir, "rustfire", args.sockets_per_room, args.socket_users_per_room, args.browser_channels, (process.pid,) if args.resources else (), args.rich_writes)
                    record_validation(result, "final pages", check_final_pages, rust_port, identities[1][0], rust_db, args.rooms, False)
                    if args.sockets_per_room and result["sockets"]["complete"]:
                        record_validation(result, "socket events", check_socket_events, rust_db, event_dir, "rustfire", groups, count)
                    if args.sample_dir:
                        (event_dir / "rustfire-result.json").write_text(json.dumps(result, indent=2))
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
                        result = measure(binary, camp_port, identities, args.rooms, args.users, args.clients, args.seconds, count, temp, event_dir, "campfire", args.sockets_per_room, args.socket_users_per_room, args.browser_channels, (process.pid, redis.pid) if args.resources else (), args.rich_writes)
                        record_validation(result, "final pages", check_final_pages, camp_port, identities[1][0], camp_db, args.rooms, True)
                        if args.sockets_per_room and result["sockets"]["complete"]:
                            record_validation(result, "socket events", check_socket_events, camp_db, event_dir, "campfire", groups, count)
                        if args.sample_dir:
                            (event_dir / "campfire-result.json").write_text(json.dumps(result, indent=2))
                        return pages, result
                    except Exception:
                        log.flush()
                        log.seek(0)
                        print(log.read()[-3000:])
                        raise
                    finally:
                        stop_server(process)

            if args.only_rustfire:
                rust_pages, rust_result = run_rustfire()
            elif args.only_campfire:
                camp_pages, camp_result = run_campfire()
            elif args.rustfire_first:
                (rust_pages, rust_result), (camp_pages, camp_result) = run_rustfire(), run_campfire()
            else:
                (camp_pages, camp_result), (rust_pages, rust_result) = run_campfire(), run_rustfire()
        finally:
            redis.terminate()
            redis.wait(timeout=10)
            redis_log.close()
        if args.only_rustfire:
            record_validation(rust_result, "saved messages", check_saved, rust_db, args.rooms, args.users, count, False)
            if args.sample_dir:
                (event_dir / "rustfire-result.json").write_text(json.dumps(rust_result, indent=2))
            complete = rust_result["reads_complete"] and rust_result["writes"]["complete"] and not rust_result.get("validation_errors") and (not args.sockets_per_room or rust_result["sockets"]["complete"])
            deadline = rust_result["writes"]["deadline_met"]
            print("PASS Rustfire single-app load point" if complete and deadline else "FAIL Rustfire single-app load point")
            print(json.dumps({"rooms": args.rooms, "users": args.users, "clients": args.clients, "seconds": args.seconds, "write_rate_per_room": args.write_rate, "sockets_per_room": args.sockets_per_room, "socket_users_per_room": args.socket_users_per_room, "browser_channels": args.browser_channels, "rich_writes": args.rich_writes, "rustfire": rust_result}, sort_keys=True))
            if not complete or not deadline:
                raise SystemExit(1)
            return
        if args.only_campfire:
            record_validation(camp_result, "saved messages", check_saved, camp_db, args.rooms, args.users, count, True)
            if args.sample_dir:
                (event_dir / "campfire-result.json").write_text(json.dumps(camp_result, indent=2))
            complete = camp_result["reads_complete"] and camp_result["writes"]["complete"] and not camp_result.get("validation_errors") and (not args.sockets_per_room or camp_result["sockets"]["complete"])
            deadline = camp_result["writes"]["deadline_met"]
            print("PASS Campfire single-app load point" if complete and deadline else "FAIL Campfire single-app load point")
            print(json.dumps({"rooms": args.rooms, "users": args.users, "clients": args.clients, "seconds": args.seconds, "write_rate_per_room": args.write_rate, "sockets_per_room": args.sockets_per_room, "socket_users_per_room": args.socket_users_per_room, "browser_channels": args.browser_channels, "rich_writes": args.rich_writes, "campfire": camp_result}, sort_keys=True))
            if not complete or not deadline:
                raise SystemExit(1)
            return
        for camp_page, rust_page in zip(camp_pages, rust_pages):
            check_message_markup(camp_page, rust_page, 40)
        record_validation(rust_result, "saved messages", check_saved, rust_db, args.rooms, args.users, count, False)
        record_validation(camp_result, "saved messages", check_saved, camp_db, args.rooms, args.users, count, True)
        if args.sample_dir:
            (event_dir / "rustfire-result.json").write_text(json.dumps(rust_result, indent=2))
            (event_dir / "campfire-result.json").write_text(json.dumps(camp_result, indent=2))
        deliveries_complete = not args.sockets_per_room or rust_result["sockets"]["complete"] and camp_result["sockets"]["complete"]
        reads_complete = rust_result["reads_complete"] and camp_result["reads_complete"]
        checked_events = 0
        paired_validation_errors = []
        if args.sockets_per_room and deliveries_complete:
            try:
                checked_events = check_paired_socket_markup(camp_db, rust_db, event_dir, groups, count)
            except Exception as error:
                paired_validation_errors.append(f"paired socket markup: {error!r}")
        deadlines_met = rust_result["writes"]["deadline_met"] and camp_result["writes"]["deadline_met"]
        validation_complete = not paired_validation_errors and not rust_result.get("validation_errors") and not camp_result.get("validation_errors")
        passed = deadlines_met and deliveries_complete and reads_complete and validation_complete
        print("PASS paired multi-room, multi-user mixed message workload" if passed else "FAIL paired workload missed a reader, writer deadline, socket delivery, or validation")
        print(json.dumps({"rooms": args.rooms, "users": args.users, "clients": args.clients, "seconds": args.seconds, "write_rate_per_room": args.write_rate, "sockets_per_room": args.sockets_per_room, "socket_users_per_room": args.socket_users_per_room, "browser_channels": args.browser_channels, "rich_writes": args.rich_writes, "stream_markup_checked_events": checked_events, "paired_validation_errors": paired_validation_errors, "campfire_workers": args.campfire_workers, "rustfire_first": args.rustfire_first, "rustfire": rust_result, "campfire": camp_result}, sort_keys=True))
        if not passed:
            raise SystemExit(1)


if __name__ == "__main__":
    main()
