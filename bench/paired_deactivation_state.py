"""Compare all persisted user-deactivation side effects with pinned Campfire."""

import pathlib
import re
import sqlite3
import subprocess
import tempfile
from urllib.parse import urlsplit

from direct_lookup import free_port, start_server, stop_server
from paired_banned_content import BUNDLE, REPOSITORY, REVISION, RUBY, isolated_campfire, start_redis
from paired_bot_admin import request
from paired_direct_lookup import login_campfire, seed_campfire, seed_rustfire, wait_for_server


def seed_state(database, campfire):
    stamp = "2026-01-01 00:00:00.000000" if campfire else "2026-01-01T00:00:00Z"
    with sqlite3.connect(database) as db:
        db.execute("UPDATE users SET email_address='member@example.invalid' WHERE id=2")
        db.execute("DELETE FROM sessions WHERE user_id=2")
        db.execute("INSERT INTO rooms(id,name,type,creator_id,created_at,updated_at) VALUES(3,'Private','Rooms::Closed',1,?1,?1)", [stamp])
        if campfire:
            db.executemany("INSERT INTO memberships(room_id,user_id,involvement,created_at,updated_at) VALUES(3,?1,'mentions',?2,?2)", [(1, stamp), (2, stamp), (3, stamp)])
            db.executemany("INSERT INTO sessions(user_id,token,created_at,updated_at,last_active_at) VALUES(?1,?2,?3,?3,?3)",
                           [(2, "member-session-1", stamp), (2, "member-session-2", stamp), (3, "other-session", stamp)])
        else:
            db.executemany("INSERT INTO memberships(room_id,user_id,involvement,created_at) VALUES(3,?1,'mentions',?2)", [(1, stamp), (2, stamp), (3, stamp)])
            db.executemany("INSERT INTO sessions(user_id,token,csrf_token,created_at,last_active_at) VALUES(?1,?2,'csrf',?3,?3)",
                           [(2, "member-session-1", stamp), (2, "member-session-2", stamp), (3, "other-session", stamp)])
        db.executemany("INSERT INTO searches(user_id,query,created_at,updated_at) VALUES(?1,?2,?3,?3)",
                       [(2, "member search one", stamp), (2, "member search two", stamp), (3, "other search", stamp)])
        db.executemany("INSERT INTO push_subscriptions(user_id,endpoint,p256dh_key,auth_key,created_at,updated_at) VALUES(?1,?2,'key','auth',?3,?3)",
                       [(2, "https://fcm.googleapis.com/fcm/send/member-one", stamp),
                        (2, "https://fcm.googleapis.com/fcm/send/member-two", stamp),
                        (3, "https://fcm.googleapis.com/fcm/send/other", stamp)])


def snapshot(database):
    with sqlite3.connect(database) as db:
        user = db.execute("SELECT status,email_address FROM users WHERE id=2").fetchone()
        rows = {}
        for table, columns in (("memberships", "room_id,user_id"),
                               ("sessions", "user_id,token"),
                               ("searches", "user_id,query"),
                               ("push_subscriptions", "user_id,endpoint")):
            rows[table] = db.execute(f"SELECT {columns} FROM {table} WHERE user_id IN (2,3) ORDER BY user_id,1,2").fetchall()
        return user, rows


def exercise(port, cookie, csrf, database):
    before = snapshot(database)
    status, location, body = request(port, "DELETE", "/account/users/2", cookie, csrf)
    assert status == 302, (status, body[:300])
    after = snapshot(database)
    email = after[0][1]
    assert re.fullmatch(r"member-deactivated-[0-9a-f-]{36}@example\.invalid", email), email
    normalized = ((after[0][0], "member-deactivated-UUID@example.invalid"), after[1])
    return before, (status, urlsplit(location).path), normalized


def main():
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPOSITORY, text=True).strip() == REVISION
    with tempfile.TemporaryDirectory(prefix="paired-deactivation-state-") as scratch:
        temp = pathlib.Path(scratch)
        rust_db, camp_db = temp / "rust.sqlite3", temp / "camp.sqlite3"
        rust_port, camp_port, redis_port = free_port(), free_port(), free_port()
        seed_rustfire(rust_db, rust_port, [(2,)])
        checkout = isolated_campfire(temp, redis_port)
        env = seed_campfire(checkout, RUBY, BUNDLE, REPOSITORY / "storage/db/production.sqlite3", camp_db, [(2,)], camp_port, temp)
        env["REDIS_URL"] = f"redis://127.0.0.1:{redis_port}"
        env["WEB_CONCURRENCY"] = "1"
        seed_state(rust_db, False)
        seed_state(camp_db, True)
        redis, redis_log = start_redis(temp, redis_port)
        try:
            rust = start_server(rust_db, rust_port)
            try:
                rust_result = exercise(rust_port, "session_token=benchmark-session", "benchmark-csrf", rust_db)
            finally:
                stop_server(rust)
            with open(temp / "puma.log", "w+") as log:
                camp = subprocess.Popen([str(RUBY), str(RUBY.parent / "bundle"), "exec", "puma", "-C", "config/puma.rb"],
                                        cwd=checkout, env=env, stdout=log, stderr=log)
                try:
                    wait_for_server(camp_port, camp)
                    cookie, csrf = login_campfire(camp_port)
                    camp_result = exercise(camp_port, cookie, csrf, camp_db)
                finally:
                    stop_server(camp)
        finally:
            redis.terminate()
            redis.wait(timeout=10)
            redis_log.close()
    assert rust_result == camp_result, (rust_result, camp_result)
    print("PASS paired deactivation status, email, sessions, searches, push subscriptions, and room memberships")


if __name__ == "__main__":
    main()
