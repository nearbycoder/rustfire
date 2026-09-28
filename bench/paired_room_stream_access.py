"""Compare signed room-stream access before and after private membership removal.

The captured token is reused on a fresh socket after removal. Campfire must
still accept it for the administrator, but reject it for the removed member.
The stock Turbo channel must reject it for both users throughout.
"""

import json
import pathlib
import re
import sqlite3
import subprocess
import tempfile
import urllib.parse

from direct_lookup import ROOT, free_port, start_server, stop_server
from paired_banned_content import REPOSITORY, RUBY, BUNDLE, REVISION, isolated_campfire, start_redis
from paired_bot_admin import request
from paired_direct_lookup import login_campfire, seed_campfire, seed_rustfire, wait_for_server


def seed_private(database, campfire):
    stamp = "2026-01-01T00:00:00Z"
    with sqlite3.connect(database) as db:
        db.execute("INSERT INTO rooms(id,name,type,creator_id,created_at,updated_at) VALUES(2,'Private','Rooms::Closed',1,?1,?1)", [stamp])
        columns = "room_id,user_id,involvement,created_at,updated_at" if campfire else "room_id,user_id,involvement,created_at"
        values = "2,?1,'mentions',?2,?2" if campfire else "2,?1,'mentions',?2"
        db.executemany(f"INSERT INTO memberships({columns}) VALUES({values})", [(1, stamp), (2, stamp)])


def signed_name(port, cookie):
    status, _, page = request(port, "GET", "/rooms/2", cookie, "")
    assert status == 200, (status, page[:200])
    source = next((tag for tag in re.findall(rb"<turbo-cable-stream-source\b[^>]*>", page, re.I)
                   if re.search(rb'\bchannel=[\'\"]RoomMessagesChannel[\'\"]', tag, re.I)), None)
    match = re.search(rb'\bsigned-stream-name=[\'\"]([^\'\"]+)', source or b"", re.I)
    assert match, "Room page lacks a signed message stream"
    return match.group(1).decode()


def decisions(port, cookie, token):
    result = subprocess.run(
        ["node", "bench/probe_room_stream_access.mjs", "--base", f"http://127.0.0.1:{port}",
         "--cookie", cookie, "--signed-name", token],
        cwd=ROOT, capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, (result.stdout, result.stderr)
    return json.loads(result.stdout.strip())


def run(port, database, admin_cookie, admin_csrf, member_cookie):
    token = signed_name(port, member_cookie)
    initial = decisions(port, member_cookie, token)
    assert initial == {
        "HeartbeatChannel": "confirm_subscription",
        "RoomMessagesChannel": "confirm_subscription",
        "Turbo::StreamsChannel": "reject_subscription",
        "UnsignedRoomMessagesChannel": "reject_subscription",
    }, initial
    body = urllib.parse.urlencode([("room[name]", "Private"), ("user_ids[]", "1")]).encode()
    status, location, response = request(port, "PATCH", "/rooms/closeds/2", admin_cookie, admin_csrf,
                                         body, "application/x-www-form-urlencoded")
    assert status == 302 and urllib.parse.urlsplit(location).path == "/rooms/2", (status, location, response[:200])
    with sqlite3.connect(database) as db:
        room = db.execute("SELECT type FROM rooms WHERE id=2").fetchone()[0]
        members = [row[0] for row in db.execute("SELECT user_id FROM memberships WHERE room_id=2 ORDER BY user_id")]
    assert room == "Rooms::Closed" and members == [1], (room, members)
    removed = decisions(port, member_cookie, token)
    retained = decisions(port, admin_cookie, token)
    assert removed == {
        "HeartbeatChannel": "confirm_subscription",
        "RoomMessagesChannel": "reject_subscription",
        "Turbo::StreamsChannel": "reject_subscription",
        "UnsignedRoomMessagesChannel": "reject_subscription",
    }, removed
    assert retained == initial, retained
    return {"before": initial, "removed_member": removed, "remaining_member": retained}


def main():
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPOSITORY, text=True).strip()
    assert revision == REVISION, revision
    with tempfile.TemporaryDirectory(prefix="paired-room-stream-access-") as temporary:
        temp = pathlib.Path(temporary)
        rust_db, camp_db = temp / "rustfire.sqlite3", temp / "campfire.sqlite3"
        rust_port, camp_port, redis_port = free_port(), free_port(), free_port()
        checkout = isolated_campfire(temp, redis_port)
        seed_rustfire(rust_db, rust_port, [])
        seed_private(rust_db, False)
        with sqlite3.connect(rust_db) as db:
            db.execute("INSERT INTO sessions(user_id,token,csrf_token,created_at,last_active_at) VALUES(2,'paired-member-session','paired-member-csrf','2026-01-01T00:00:00Z','2026-01-01T00:00:00Z')")
        camp_env = seed_campfire(checkout, RUBY, BUNDLE, REPOSITORY / "storage/db/production.sqlite3", camp_db, [], camp_port, temp)
        camp_env["WEB_CONCURRENCY"] = "1"
        camp_env["REDIS_URL"] = f"redis://127.0.0.1:{redis_port}"
        seed_private(camp_db, True)
        with sqlite3.connect(camp_db) as db:
            digest = db.execute("SELECT password_digest FROM users WHERE id=1").fetchone()[0]
            db.execute("UPDATE users SET email_address='member@example.invalid',password_digest=? WHERE id=2", [digest])

        rust = start_server(rust_db, rust_port)
        try:
            observed_rust = run(rust_port, rust_db, "session_token=benchmark-session", "benchmark-csrf",
                                "session_token=paired-member-session")
        finally:
            stop_server(rust)

        redis, redis_log = start_redis(temp, redis_port)
        log = open(temp / "puma.log", "w+")
        camp = None
        try:
            camp = subprocess.Popen([str(RUBY), str(RUBY.parent / "bundle"), "exec", "puma", "-C", "config/puma.rb"],
                                    cwd=checkout, env=camp_env, stdout=log, stderr=log)
            wait_for_server(camp_port, camp)
            member_cookie, _ = login_campfire(camp_port, "member@example.invalid")
            admin_cookie, admin_csrf = login_campfire(camp_port)
            observed_camp = run(camp_port, camp_db, admin_cookie, admin_csrf, member_cookie)
        finally:
            if camp is not None:
                stop_server(camp)
            redis.terminate()
            redis.wait(timeout=10)
            redis_log.close()
            log.close()
    assert observed_rust == observed_camp, (observed_rust, observed_camp)
    print(json.dumps({"revision": revision, "rustfire": observed_rust, "campfire": observed_camp}, sort_keys=True))
    print("PASS signed room stream membership and Turbo bypass protection after reconnect")


if __name__ == "__main__":
    main()
