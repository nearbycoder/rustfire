"""Compare sign-out endpoint selection with pinned Campfire."""

import pathlib
import sqlite3
import subprocess
import tempfile
import urllib.parse

from direct_lookup import free_port, start_server, stop_server
from paired_banned_content import BUNDLE, REPOSITORY, REVISION, RUBY, isolated_campfire, start_redis
from paired_bot_admin import request
from paired_direct_lookup import login_campfire, seed_campfire, seed_rustfire, wait_for_server


FIRST = "https://fcm.googleapis.com/fcm/send/first-device"
SECOND = "https://fcm.googleapis.com/fcm/send/second-device"
OTHER = "https://fcm.googleapis.com/fcm/send/other-user"
CASES = (
    ("body endpoint", "DELETE", (("push_subscription_endpoint", FIRST),), ()),
    ("query endpoint", "DELETE", (), (("push_subscription_endpoint", SECOND),)),
    ("query overrides body", "DELETE", (("push_subscription_endpoint", FIRST),), (("push_subscription_endpoint", SECOND),)),
    ("blank query overrides body", "DELETE", (("push_subscription_endpoint", FIRST),), (("push_subscription_endpoint", ""),)),
    ("duplicate body endpoint", "DELETE", (("push_subscription_endpoint", FIRST), ("push_subscription_endpoint", SECOND)), ()),
    ("duplicate query endpoint", "DELETE", (), (("push_subscription_endpoint", FIRST), ("push_subscription_endpoint", SECOND))),
    ("POST override with query", "POST", (("_method", "delete"), ("push_subscription_endpoint", FIRST)), (("push_subscription_endpoint", SECOND),)),
)


def reset(database, rustfire):
    with sqlite3.connect(database) as db:
        db.execute("DELETE FROM push_subscriptions")
        db.executemany(
            "INSERT INTO push_subscriptions(user_id,endpoint,p256dh_key,auth_key,created_at,updated_at) VALUES(?1,?2,'key','auth','2026-01-01 00:00:00','2026-01-01 00:00:00')",
            ((1, FIRST), (1, SECOND), (2, OTHER)),
        )
        if rustfire:
            db.execute("INSERT OR IGNORE INTO sessions(user_id,token,csrf_token,created_at,last_active_at) VALUES(1,'benchmark-session','benchmark-csrf','2026-01-01T00:00:00Z','2026-01-01T00:00:00Z')")


def result(port, database, cookie, csrf, method, body, query):
    path = "/session" + ("?" + urllib.parse.urlencode(query) if query else "")
    status, location, response = request(port, method, path, cookie, csrf, urllib.parse.urlencode(body).encode(),
                                         "application/x-www-form-urlencoded")
    with sqlite3.connect(database) as db:
        remaining = db.execute("SELECT user_id,endpoint FROM push_subscriptions ORDER BY user_id,endpoint").fetchall()
    return status, urllib.parse.urlsplit(location).path if location else None, response, remaining


def main():
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPOSITORY, text=True).strip() == REVISION
    with tempfile.TemporaryDirectory(prefix="paired-logout-parameters-") as scratch:
        temp = pathlib.Path(scratch)
        rust_db, camp_db = temp / "rust.sqlite3", temp / "camp.sqlite3"
        rust_port, camp_port, redis_port = free_port(), free_port(), free_port()
        seed_rustfire(rust_db, rust_port, [])
        camp_env = seed_campfire(REPOSITORY, RUBY, BUNDLE, REPOSITORY / "storage/db/production.sqlite3", camp_db, [], camp_port, temp)
        checkout = isolated_campfire(temp, redis_port)
        camp_env["REDIS_URL"] = f"redis://127.0.0.1:{redis_port}"
        redis, redis_log = start_redis(temp, redis_port)
        try:
            rust = start_server(rust_db, rust_port)
            with open(temp / "puma.log", "w+") as log:
                camp = subprocess.Popen([str(RUBY), str(RUBY.parent / "bundle"), "exec", "puma", "-C", "config/puma.rb"],
                                        cwd=checkout, env=camp_env, stdout=log, stderr=log)
                try:
                    wait_for_server(camp_port, camp)
                    results = []
                    for label, method, body, query in CASES:
                        reset(rust_db, True)
                        reset(camp_db, False)
                        cookie, csrf = login_campfire(camp_port)
                        actual = result(rust_port, rust_db, "session_token=benchmark-session", "benchmark-csrf", method, body, query)
                        expected = result(camp_port, camp_db, cookie, csrf, method, body, query)
                        results.append((label, actual, expected))
                except Exception:
                    log.flush()
                    log.seek(0)
                    print(log.read()[-2000:])
                    raise
                finally:
                    stop_server(camp)
                    stop_server(rust)
        finally:
            redis.terminate()
            redis.wait(timeout=10)
            redis_log.close()
    mismatches = [(label, actual, expected) for label, actual, expected in results if actual != expected]
    for label, actual, expected in mismatches:
        print(f"{label}: Rustfire={actual}, Campfire={expected}")
    assert not mismatches, f"{len(mismatches)} sign-out endpoint cases differ"
    print(f"PASS {len(CASES)} paired sign-out endpoint cases match Campfire")


if __name__ == "__main__":
    main()
