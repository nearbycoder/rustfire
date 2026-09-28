"""Compare per-member unread broadcasts and saved unread state with Campfire.

Run after `cargo build --release`. Both apps use disposable databases.
"""

import base64
from datetime import datetime, timedelta, timezone
import http.client
import http.cookiejar
import json
import os
import pathlib
import re
import socket
import sqlite3
import struct
import subprocess
import tempfile
import time
import urllib.parse
import urllib.request

from direct_lookup import free_port, start_server, stop_server
from paired_banned_content import REPOSITORY, RUBY, BUNDLE, REVISION, start_redis
from paired_direct_lookup import login_campfire, seed_campfire, seed_rustfire, wait_for_server


class Cable:
    def __init__(self, port, cookie):
        self.socket = socket.create_connection(("127.0.0.1", port), timeout=5)
        self.pending = bytearray()
        self.events = []
        key = base64.b64encode(os.urandom(16)).decode()
        handshake = (
            f"GET /cable HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nOrigin: http://127.0.0.1:{port}\r\n"
            "Upgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Version: 13\r\n"
            f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Protocol: actioncable-v1-json\r\nCookie: {cookie}\r\n\r\n"
        )
        self.socket.sendall(handshake.encode())
        while b"\r\n\r\n" not in self.pending:
            chunk = self.socket.recv(4096)
            if not chunk:
                raise RuntimeError("Socket closed during handshake")
            self.pending.extend(chunk)
        head, _, remaining = self.pending.partition(b"\r\n\r\n")
        self.pending = bytearray(remaining)
        if not head.startswith(b"HTTP/1.1 101"):
            raise RuntimeError(head.decode(errors="replace").splitlines()[0])
        self.wait_for(lambda event: event.get("type") == "welcome")

    def close(self):
        self.socket.close()

    def send(self, command):
        payload = json.dumps(command, separators=(",", ":")).encode()
        mask = os.urandom(4)
        if len(payload) < 126:
            header = bytes([0x81, 0x80 | len(payload)])
        else:
            header = bytes([0x81, 0x80 | 126]) + struct.pack("!H", len(payload))
        masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        self.socket.sendall(header + mask + masked)

    def _read(self, count, deadline):
        while len(self.pending) < count:
            self.socket.settimeout(max(0.001, deadline - time.monotonic()))
            chunk = self.socket.recv(4096)
            if not chunk:
                raise RuntimeError("Action Cable socket closed")
            self.pending.extend(chunk)
        value = bytes(self.pending[:count])
        del self.pending[:count]
        return value

    def receive(self, timeout=5):
        deadline = time.monotonic() + timeout
        while True:
            try:
                first, second = self._read(2, deadline)
                size = second & 127
                if size == 126:
                    size = struct.unpack("!H", self._read(2, deadline))[0]
                elif size == 127:
                    size = struct.unpack("!Q", self._read(8, deadline))[0]
                payload = self._read(size, deadline)
            except socket.timeout:
                return None
            if first & 15 != 1:
                continue
            event = json.loads(payload)
            if event.get("type") != "ping":
                return event

    def wait_for(self, predicate, timeout=5):
        for index, event in enumerate(self.events):
            if predicate(event):
                return self.events.pop(index)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            event = self.receive(deadline - time.monotonic())
            if event is None:
                break
            if predicate(event):
                return event
            self.events.append(event)
        raise AssertionError(f"Expected Action Cable event missing; queued={self.events!r}")

    def subscribe_unread(self):
        identifier = json.dumps({"channel": "UnreadRoomsChannel"}, separators=(",", ":"))
        self.send({"command": "subscribe", "identifier": identifier})
        self.wait_for(lambda event: event.get("identifier") == identifier and event.get("type") == "confirm_subscription")
        self.identifier = identifier

    def unread(self):
        event = self.wait_for(lambda frame: frame.get("identifier") == self.identifier and frame.get("message", {}).get("roomId") == 1)
        return event["message"]

    def assert_no_unread(self):
        if any(event.get("identifier") == self.identifier for event in self.events):
            raise AssertionError(self.events)
        event = self.receive(0.25)
        if event is not None and event.get("identifier") == self.identifier:
            raise AssertionError(f"Nonmember received unread event: {event!r}")


def login_without_room(port, email):
    cookies = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cookies))
    with opener.open(f"http://127.0.0.1:{port}/session/new") as response:
        page = response.read().decode()
    csrf = re.search(r'<meta name="csrf-token" content="([^"]+)', page).group(1)
    data = urllib.parse.urlencode({"email_address": email, "password": "benchmark-password", "authenticity_token": csrf}).encode()
    with opener.open(f"http://127.0.0.1:{port}/session", data=data) as response:
        assert response.status == 200
        response.read()
    return "; ".join(f"{item.name}={item.value}" for item in cookies)


def seed_sessions(rust_db, camp_db):
    stamp = "2026-01-01T00:00:00Z"
    with sqlite3.connect(rust_db) as db:
        db.executemany(
            "INSERT INTO sessions(user_id,token,csrf_token,created_at,last_active_at) VALUES(?1,?2,?3,?4,?4)",
            [(2, "member-session", "member-csrf", stamp), (3, "outsider-session", "outsider-csrf", stamp)],
        )
    with sqlite3.connect(camp_db) as db:
        db.execute("UPDATE users SET email_address='member@example.test',password_digest=(SELECT password_digest FROM users WHERE id=1) WHERE id=2")
        db.execute("UPDATE users SET email_address='outsider@example.test',password_digest=(SELECT password_digest FROM users WHERE id=1) WHERE id=3")


def post(port, cookie, csrf, index):
    data = urllib.parse.urlencode({
        "message[body]": f"paired unread {index}",
        "message[client_message_id]": f"paired-unread-{index}",
        "authenticity_token": csrf,
    })
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
    try:
        connection.request("POST", "/rooms/1/messages", data, {
            "Cookie": cookie, "X-CSRF-Token": csrf,
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "text/vnd.turbo-stream.html, text/html",
        })
        response = connection.getresponse()
        body = response.read()
        assert response.status in (200, 201), (response.status, body[:300])
    finally:
        connection.close()


def member_state(database):
    with sqlite3.connect(database) as db:
        return db.execute("SELECT unread_at,updated_at FROM memberships WHERE room_id=1 AND user_id=2").fetchone()


def set_member(database, campfire, involvement, connection):
    current = datetime.now(timezone.utc) - (timedelta(minutes=2) if connection == "stale" else timedelta())
    connected_at = current.strftime("%Y-%m-%d %H:%M:%S.%f") if campfire else current.isoformat().replace("+00:00", "Z")
    with sqlite3.connect(database) as db:
        db.execute(
            "UPDATE memberships SET involvement=?,connections=?,connected_at=?,unread_at=NULL WHERE room_id=1 AND user_id=2",
            (involvement, 0 if connection == "none" else 1, None if connection == "none" else connected_at),
        )


def set_room_kind(database, kind):
    with sqlite3.connect(database) as db:
        db.execute("UPDATE rooms SET type=?,name=? WHERE id=1", (kind, None if kind == "Rooms::Direct" else "All Talk"))


def exercise(port, database, cookies, csrf, campfire):
    sockets = [Cable(port, cookie) for cookie in cookies]
    try:
        for client in sockets:
            client.subscribe_unread()
        results = []
        open_cases = (
            ("mentions", "fresh", False),
            ("invisible", "none", False),
            ("mentions", "none", True),
            ("nothing", "none", True),
            ("everything", "none", True),
            ("everything", "fresh", False),
            ("nothing", "fresh", False),
            ("mentions", "stale", True),
        )
        other_cases = (("everything", "none", True), ("invisible", "none", False), ("mentions", "fresh", False))
        index = 0
        for kind, cases in (("Rooms::Open", open_cases), ("Rooms::Closed", other_cases), ("Rooms::Direct", other_cases)):
            set_room_kind(database, kind)
            for involvement, connection, expected_unread in cases:
                index += 1
                set_member(database, campfire, involvement, connection)
                before_updated_at = member_state(database)[1]
                post(port, cookies[0], csrf, index)
                author_event, member_event = sockets[0].unread(), sockets[1].unread()
                assert author_event == member_event == {"roomId": 1}, (author_event, member_event)
                sockets[2].assert_no_unread()
                unread_at, updated_at = member_state(database)
                saved = unread_at is not None
                touched = updated_at != before_updated_at
                assert saved == expected_unread, (kind, index, saved)
                assert touched == expected_unread, (kind, index, touched)
                results.append({"room_kind": kind, "member_connection": connection, "involvement": involvement, "unread_saved": saved, "membership_touched": touched, "member_events": 2, "outsider_events": 0})
        return results
    finally:
        for client in sockets:
            client.close()


def main():
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPOSITORY, text=True).strip()
    assert revision == REVISION, revision
    with tempfile.TemporaryDirectory(prefix="paired-unread-") as scratch:
        temp = pathlib.Path(scratch)
        rust_db, camp_db = temp / "rust.sqlite3", temp / "camp.sqlite3"
        rust_port, camp_port, redis_port = free_port(), free_port(), free_port()
        seed_rustfire(rust_db, rust_port, [])
        camp_env = seed_campfire(REPOSITORY, RUBY, BUNDLE, REPOSITORY / "storage/db/production.sqlite3", camp_db, [], camp_port, temp)
        camp_env["REDIS_URL"] = f"redis://127.0.0.1:{redis_port}"
        seed_sessions(rust_db, camp_db)
        redis, redis_log = start_redis(temp, redis_port)
        try:
            rust = start_server(rust_db, rust_port)
            try:
                rust_result = exercise(
                    rust_port, rust_db,
                    ["session_token=benchmark-session", "session_token=member-session", "session_token=outsider-session"],
                    "benchmark-csrf", False,
                )
            finally:
                stop_server(rust)
            with open(temp / "puma.log", "w+") as log:
                camp = subprocess.Popen(
                    [str(RUBY), str(RUBY.parent / "bundle"), "exec", "puma", "-C", "config/puma.rb"],
                    cwd=REPOSITORY, env=camp_env, stdout=log, stderr=log,
                )
                try:
                    wait_for_server(camp_port, camp)
                    author_cookie, csrf = login_campfire(camp_port)
                    member_cookie, _ = login_campfire(camp_port, "member@example.test")
                    outsider_cookie = login_without_room(camp_port, "outsider@example.test")
                    camp_result = exercise(camp_port, camp_db, [author_cookie, member_cookie, outsider_cookie], csrf, True)
                except Exception:
                    log.flush()
                    log.seek(0)
                    print(log.read()[-3000:])
                    raise
                finally:
                    stop_server(camp)
        finally:
            redis.terminate()
            redis.wait(timeout=10)
            redis_log.close()
        assert rust_result == camp_result, (rust_result, camp_result)
        print("PASS paired member-scoped unread events and saved unread state")
        print(json.dumps({"rustfire": rust_result, "campfire": camp_result}, sort_keys=True))


if __name__ == "__main__":
    main()
