"""Compare account user administration with the pinned Campfire checkout.

Run after ``cargo build --release`` with the pinned Ruby bundle and Redis.
"""

import argparse
import html
import pathlib
import re
import sqlite3
import subprocess
import tempfile
import urllib.parse

from direct_lookup import free_port, start_server, stop_server
from paired_bot_admin import request
from paired_direct_lookup import login_campfire, seed_campfire, seed_rustfire, wait_for_server


def role_and_status(database):
    with sqlite3.connect(database) as db:
        return db.execute("SELECT role,status FROM users WHERE id=2").fetchone()


def membership_state(database):
    with sqlite3.connect(database) as db:
        return db.execute("SELECT room_id FROM memberships WHERE user_id=2 ORDER BY room_id").fetchall()


def workflow(port, cookie, csrf, database):
    result = {}
    status, _, body = request(port, "GET", "/account/edit", cookie, csrf)
    assert status == 200
    page = body.decode()
    frame = re.search(r"<turbo-frame\b[^>]*\bid=['\"]account_users['\"][^>]*>(.*?)</turbo-frame>", page, re.S)
    assert frame, "Account user frame is absent"
    frame = frame.group(1)
    with sqlite3.connect(database) as db:
        admin = html.escape(db.execute("SELECT name FROM users WHERE id=1").fetchone()[0])
        member = html.escape(db.execute("SELECT name FROM users WHERE id=2").fetchone()[0])
    divider = re.search(r"<hr\s+class=['\"]separator full-width['\"]", frame)
    assert divider and frame.index(f"<strong>{admin}</strong>") < divider.start() < frame.index(f"<strong>{member}</strong>")
    result["roster_grouping"] = True

    for key, role in (("promote", "administrator"), ("invalid_role", "invalid")):
        body = urllib.parse.urlencode({"user[role]": role}).encode()
        status, location, payload = request(port, "PUT", "/account/users/2", cookie, csrf, body, "application/x-www-form-urlencoded")
        assert status == 302, (key, status, payload[:300])
        result[key] = status, urllib.parse.urlsplit(location).path, role_and_status(database)

    status, location, payload = request(port, "DELETE", "/account/users/2", cookie, csrf)
    assert status == 302, (status, payload[:300])
    result["deactivate"] = status, urllib.parse.urlsplit(location).path, role_and_status(database), membership_state(database)
    status, _, _ = request(port, "DELETE", "/account/users/2", cookie, csrf)
    result["repeat_delete"] = status
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--campfire-repo", type=pathlib.Path, default=pathlib.Path("/tmp/once-campfire-reference"))
    parser.add_argument("--ruby", type=pathlib.Path, default=pathlib.Path("/tmp/rustfire-baseline/local/bin/ruby"))
    parser.add_argument("--bundle-path", type=pathlib.Path, default=pathlib.Path("/tmp/rustfire-baseline/bundle"))
    args = parser.parse_args()
    repository, ruby, bundle_path = args.campfire_repo.resolve(), args.ruby.resolve(), args.bundle_path.resolve()
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repository, text=True).strip()
    assert revision == "91d294f4a09f9bbe37f9548959bfcb43645678fb", revision
    with tempfile.TemporaryDirectory(prefix="paired-account-users-") as temporary:
        temp = pathlib.Path(temporary)
        rust_db, camp_db = temp / "rustfire.sqlite3", temp / "campfire.sqlite3"
        rust_port, camp_port = free_port(), free_port()
        direct_rooms = [(2,)]
        seed_rustfire(rust_db, rust_port, direct_rooms)
        env = seed_campfire(repository, ruby, bundle_path, repository / "storage/db/production.sqlite3", camp_db, direct_rooms, camp_port, temp)
        rust_process = start_server(rust_db, rust_port)
        try:
            rust = workflow(rust_port, "session_token=benchmark-session", "benchmark-csrf", rust_db)
        finally:
            stop_server(rust_process)
        log = open(temp / "puma.log", "w+")
        camp_process = subprocess.Popen([str(ruby), str(ruby.parent / "bundle"), "exec", "puma", "-C", "config/puma.rb"], cwd=repository, env=env, stdout=log, stderr=log)
        try:
            wait_for_server(camp_port, camp_process)
            cookie, csrf = login_campfire(camp_port)
            camp = workflow(camp_port, cookie, csrf, camp_db)
        except Exception:
            log.flush()
            log.seek(0)
            print(log.read()[-3000:])
            raise
        finally:
            stop_server(camp_process)
            log.close()
        for key in rust:
            assert rust[key] == camp[key], (key, rust[key], camp[key])
            print(f"{key}: rustfire={rust[key]!r} campfire={camp[key]!r}")


if __name__ == "__main__":
    main()
