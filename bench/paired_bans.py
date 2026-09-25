"""Compare ban, repeated ban, and unban behavior with pinned Campfire.

Run after ``cargo build --release`` with the pinned Ruby bundle and Redis.
The workflow uses disposable account databases and public fixture IP addresses.
"""

import http.client
import pathlib
import sqlite3
import subprocess
import tempfile
import urllib.parse

from direct_lookup import free_port, start_server, stop_server
from paired_bot_admin import request
from paired_direct_lookup import login_campfire, seed_campfire, seed_rustfire, wait_for_server


REPOSITORY = pathlib.Path("/tmp/once-campfire-reference")
RUBY = pathlib.Path("/tmp/rustfire-baseline/local/bin/ruby")
BUNDLE = pathlib.Path("/tmp/rustfire-baseline/bundle")
REVISION = "91d294f4a09f9bbe37f9548959bfcb43645678fb"
ADDRESSES = ("203.0.113.1", "203.0.113.2")


def seed_sessions(database, campfire):
    stamp = "2026-01-01T00:00:00Z"
    with sqlite3.connect(database) as db:
        db.execute("DELETE FROM sessions WHERE user_id=2")
        if campfire:
            db.executemany(
                "INSERT INTO sessions(user_id,token,ip_address,user_agent,created_at,updated_at,last_active_at) VALUES(2,?1,?2,'Paired ban probe',?3,?3,?3)",
                [(f"paired-ban-{index}", address, stamp) for index, address in enumerate(ADDRESSES)],
            )
        else:
            db.executemany(
                "INSERT INTO sessions(user_id,token,csrf_token,ip_address,created_at,last_active_at) VALUES(2,?1,?2,?3,?4,?4)",
                [(f"paired-ban-{index}", f"paired-csrf-{index}", address, stamp) for index, address in enumerate(ADDRESSES)],
            )


def state(database):
    with sqlite3.connect(database) as db:
        status = db.execute("SELECT status FROM users WHERE id=2").fetchone()[0]
        sessions = db.execute("SELECT count(*) FROM sessions WHERE user_id=2").fetchone()[0]
        bans = tuple(row[0] for row in db.execute("SELECT ip_address FROM bans WHERE user_id=2 ORDER BY ip_address"))
    return status, sessions, bans


def mutation(port, method, cookie, csrf):
    status, location, body = request(port, method, "/users/2/ban", cookie, csrf)
    return status, urllib.parse.urlsplit(location).path if location else None, body[:160]


def forwarded_status(port, method):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
    try:
        connection.request(method, "/session/new" if method == "GET" else "/session", headers={"X-Forwarded-For": ADDRESSES[0]})
        response = connection.getresponse()
        response.read()
        return response.status
    finally:
        connection.close()


def workflow(port, cookie, csrf, database, campfire):
    assert state(database) == (0, 2, ())
    result = {}
    for name, method in (("ban", "POST"), ("repeat_ban", "POST"), ("unban", "DELETE"), ("repeat_unban", "DELETE")):
        status, location, body = mutation(port, method, cookie, csrf)
        result[name] = (status, location, state(database))
        if name == "ban":
            result["banned_ip"] = (forwarded_status(port, "GET"), forwarded_status(port, "POST"))
        elif name == "unban":
            result["unbanned_ip"] = forwarded_status(port, "POST") != 429
        if status >= 500:
            print(name, "server error:", body)
    with sqlite3.connect(database) as db:
        db.execute("UPDATE users SET role=0 WHERE id=1")
    for name, method in (("member_ban", "POST"), ("member_unban", "DELETE")):
        status, location, _ = mutation(port, method, cookie, csrf)
        result[name] = (status, location, state(database))
    with sqlite3.connect(database) as db:
        db.execute("UPDATE users SET role=1 WHERE id=1")
        if campfire:
            db.execute(
                "INSERT INTO sessions(user_id,token,ip_address,user_agent,created_at,updated_at,last_active_at) VALUES(2,'paired-private-session','127.0.0.1','Paired ban probe','2026-01-01','2026-01-01','2026-01-01')"
            )
        else:
            db.execute(
                "INSERT INTO sessions(user_id,token,csrf_token,ip_address,created_at,last_active_at) VALUES(2,'paired-private-session','paired-private-csrf','127.0.0.1','2026-01-01','2026-01-01')"
            )
    status, location, _ = mutation(port, "POST", cookie, csrf)
    result["private_session_ban"] = (status, location, state(database))
    return result


def main():
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPOSITORY, text=True).strip()
    assert revision == REVISION, revision
    with tempfile.TemporaryDirectory(prefix="paired-bans-") as temporary:
        temp = pathlib.Path(temporary)
        rust_db, camp_db = temp / "rustfire.sqlite3", temp / "campfire.sqlite3"
        rust_port, camp_port = free_port(), free_port()
        seed_rustfire(rust_db, rust_port, [])
        env = seed_campfire(REPOSITORY, RUBY, BUNDLE, REPOSITORY / "storage/db/production.sqlite3", camp_db, [], camp_port, temp)
        seed_sessions(rust_db, False)
        seed_sessions(camp_db, True)
        rust_process = start_server(rust_db, rust_port, {"RUSTFIRE_TRUSTED_PROXY_IPS": "127.0.0.1"})
        try:
            rust = workflow(rust_port, "session_token=benchmark-session", "benchmark-csrf", rust_db, False)
        finally:
            stop_server(rust_process)
        log = open(temp / "puma.log", "w+")
        camp_process = subprocess.Popen([str(RUBY), str(RUBY.parent / "bundle"), "exec", "puma", "-C", "config/puma.rb"], cwd=REPOSITORY, env=env, stdout=log, stderr=log)
        try:
            wait_for_server(camp_port, camp_process)
            cookie, csrf = login_campfire(camp_port)
            camp = workflow(camp_port, cookie, csrf, camp_db, True)
        except Exception:
            log.flush()
            log.seek(0)
            print(log.read()[-3000:])
            raise
        finally:
            stop_server(camp_process)
            log.close()
        mismatches = []
        for name in rust:
            print(f"{name}: rustfire={rust[name]!r} campfire={camp[name]!r}")
            if rust[name] != camp[name]:
                mismatches.append(name)
        assert not mismatches, mismatches
        print("PASS paired ban, repeated ban, unban, and authorization workflow")


if __name__ == "__main__":
    main()
