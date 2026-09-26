"""Compare push registration when an endpoint is reused with different keys."""

import http.client
import pathlib
import sqlite3
import subprocess
import tempfile
import urllib.parse

from direct_lookup import free_port, start_server, stop_server
from paired_direct_lookup import login_campfire, seed_campfire, seed_rustfire, wait_for_server
from paired_push_subscriptions_page import BUNDLE, REPOSITORY, REVISION, RUBY


ENDPOINT = "https://fcm.googleapis.com/fcm/send/rustfire-parity"
CREATED = "2026-01-01 00:00:00"


def seed_legacy(rust_db, camp_db):
    with sqlite3.connect(rust_db) as db:
        db.execute("DROP TABLE push_subscriptions")
        db.execute("""CREATE TABLE push_subscriptions(id INTEGER PRIMARY KEY,user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            endpoint TEXT NOT NULL,p256dh_key TEXT NOT NULL,auth_key TEXT NOT NULL,user_agent TEXT,
            created_at TEXT NOT NULL,updated_at TEXT NOT NULL,UNIQUE(user_id,endpoint))""")
        db.execute("INSERT INTO push_subscriptions VALUES(1,1,?,'legacy-p','legacy-a','Legacy Agent',?,?)", (ENDPOINT, CREATED, CREATED))
    with sqlite3.connect(camp_db) as db:
        db.execute("DELETE FROM push_subscriptions")
        db.execute("INSERT INTO push_subscriptions(id,user_id,endpoint,p256dh_key,auth_key,user_agent,created_at,updated_at) VALUES(1,1,?,'legacy-p','legacy-a','Legacy Agent',?,?)", (ENDPOINT, CREATED, CREATED))


def post(port, cookie, csrf, p256dh, auth, agent):
    body = urllib.parse.urlencode({
        "push_subscription[endpoint]": ENDPOINT,
        "push_subscription[p256dh_key]": p256dh,
        "push_subscription[auth_key]": auth,
    })
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    try:
        connection.request("POST", "/users/me/push_subscriptions", body, {
            "Cookie": cookie,
            "X-CSRF-Token": csrf,
            "User-Agent": agent,
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "text/html",
        })
        response = connection.getresponse()
        payload = response.read()
        assert response.status == 200, (port, response.status, payload[:300])
    finally:
        connection.close()


def rows(database):
    with sqlite3.connect(database) as db:
        return db.execute("SELECT id,user_id,endpoint,p256dh_key,auth_key,user_agent,created_at,updated_at FROM push_subscriptions ORDER BY id").fetchall()


def check_pair(rust_db, camp_db, stage, count):
    rust_rows, camp_rows = rows(rust_db), rows(camp_db)
    assert len(rust_rows) == len(camp_rows) == count, (stage, rust_rows, camp_rows)
    assert [row[:6] for row in rust_rows] == [row[:6] for row in camp_rows], (stage, rust_rows, camp_rows)
    return rust_rows, camp_rows


def main():
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPOSITORY, text=True).strip() == REVISION
    with tempfile.TemporaryDirectory(prefix="paired-push-registration-") as directory:
        temp = pathlib.Path(directory)
        rust_db, camp_db = temp / "rust.sqlite3", temp / "camp.sqlite3"
        rust_port, camp_port = free_port(), free_port()
        seed_rustfire(rust_db, rust_port, [])
        env = seed_campfire(REPOSITORY, RUBY, BUNDLE, REPOSITORY / "storage/db/production.sqlite3", camp_db, [], camp_port, temp)
        seed_legacy(rust_db, camp_db)
        rust = start_server(rust_db, rust_port, {"RUSTFIRE_CAMPFIRE_SECRET_KEY_BASE": env["SECRET_KEY_BASE"], "RUSTFIRE_DISABLE_PUSH": "1"})
        env["WEB_CONCURRENCY"] = "1"
        with open(temp / "puma.log", "w+") as log:
            camp = subprocess.Popen([str(RUBY), str(RUBY.parent / "bundle"), "exec", "puma", "-C", "config/puma.rb"], cwd=REPOSITORY, env=env, stdout=log, stderr=log)
            try:
                wait_for_server(camp_port, camp)
                camp_cookie, camp_csrf = login_campfire(camp_port)
                identities = ((rust_port, "session_token=benchmark-session", "benchmark-csrf"), (camp_port, camp_cookie, camp_csrf))
                check_pair(rust_db, camp_db, "migrated legacy row", 1)
                with sqlite3.connect(rust_db) as db:
                    assert db.execute("PRAGMA foreign_key_check").fetchall() == []
                    assert not any(row[2] for row in db.execute("PRAGMA index_list('push_subscriptions')")), "old endpoint uniqueness remains"
                for port, cookie, csrf in identities:
                    post(port, cookie, csrf, "legacy-p", "legacy-a", "Touch Agent")
                rust_rows, camp_rows = check_pair(rust_db, camp_db, "exact registration touch", 1)
                assert rust_rows[0][5] == camp_rows[0][5] == "Legacy Agent"
                assert rust_rows[0][6] == camp_rows[0][6] == CREATED
                assert rust_rows[0][7] != CREATED and camp_rows[0][7] != CREATED
                for port, cookie, csrf in identities:
                    post(port, cookie, csrf, "new-p", "new-a", "New Agent")
                rust_rows, camp_rows = check_pair(rust_db, camp_db, "same endpoint, new keys", 2)
                assert rust_rows[0][3:6] == ("legacy-p", "legacy-a", "Legacy Agent")
                assert rust_rows[1][3:6] == ("new-p", "new-a", "New Agent")
                for port, cookie, csrf in identities:
                    post(port, cookie, csrf, "legacy-p", "legacy-a", "Later Agent")
                check_pair(rust_db, camp_db, "repeat exact registration", 2)
            except Exception:
                log.flush()
                log.seek(0)
                print(log.read()[-2000:])
                raise
            finally:
                stop_server(camp)
                stop_server(rust)
    print("PASS push endpoint/key identity, exact-match touch, and legacy Rustfire migration match Campfire")


if __name__ == "__main__":
    main()
