"""Compare clicked profile sign-out and device push cleanup in Chromium."""

import json
import pathlib
import shutil
import sqlite3
import subprocess
import tempfile
import time
import uuid

from direct_lookup import free_port, start_server, stop_server
from paired_banned_content import BUNDLE, REPOSITORY, REVISION, RUBY, start_redis
from paired_direct_lookup import seed_campfire, seed_rustfire, wait_for_server
from paired_reply_browser import browser


ENDPOINT = "https://fcm.googleapis.com/fcm/send/current-device"


def push_rows(database):
    with sqlite3.connect(database) as db:
        return db.execute("SELECT user_id,endpoint FROM push_subscriptions ORDER BY user_id,endpoint").fetchall()


def check_browser(session, port, database):
    browser(session, "open", f"http://127.0.0.1:{port}/session/new")
    browser(session, "fill", 'input[name="email_address"]', "benchmark@example.invalid")
    browser(session, "fill", 'input[name="password"]', "benchmark-password")
    browser(session, "click", 'button[name="log_in"]')
    browser(session, "wait", "--url", "**/rooms/*")
    browser(session, "open", f"http://127.0.0.1:{port}/users/me/profile")
    browser(session, "wait", 'form[data-controller="sessions"] button[data-action="sessions#logout:prevent"]')
    browser(session, "wait", "--load", "networkidle")
    browser(session, "eval", f"""(() => {{
      const endpoint={json.dumps(ENDPOINT)};
      Object.defineProperty(navigator.serviceWorker,'getRegistration',{{configurable:true,value:async origin=>{{
        sessionStorage.setItem('logout-registration-origin',origin);
        return {{pushManager:{{getSubscription:async()=>({{
          endpoint,unsubscribe:async()=>{{sessionStorage.setItem('logout-unsubscribed','true');return true}}
        }})}}}};
      }}}});
    }})()""")
    browser(session, "click", 'form[data-controller="sessions"] button[data-action="sessions#logout:prevent"]')
    browser(session, "wait", "--url", "**/session/new")
    state = json.loads(browser(session, "eval", "({origin:sessionStorage.getItem('logout-registration-origin'),unsubscribed:sessionStorage.getItem('logout-unsubscribed')})"))
    assert state == {"origin": f"http://127.0.0.1:{port}", "unsubscribed": "true"}, state
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        rows = push_rows(database)
        if rows == [(1, "https://fcm.googleapis.com/fcm/send/other-device"), (2, ENDPOINT)]:
            break
        time.sleep(0.1)
    assert rows == [(1, "https://fcm.googleapis.com/fcm/send/other-device"), (2, ENDPOINT)], rows
    assert not browser(session, "errors").strip(), browser(session, "errors")


def main():
    assert shutil.which("agent-browser"), "agent-browser CLI is required"
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPOSITORY, text=True).strip() == REVISION
    sessions = [f"logout-rust-{uuid.uuid4().hex[:8]}", f"logout-camp-{uuid.uuid4().hex[:8]}"]
    with tempfile.TemporaryDirectory(prefix="paired-logout-browser-") as scratch:
        temp = pathlib.Path(scratch)
        rust_db, camp_db = temp / "rust.sqlite3", temp / "camp.sqlite3"
        rust_port, camp_port, redis_port = free_port(), free_port(), free_port()
        seed_rustfire(rust_db, rust_port, [])
        environment = seed_campfire(REPOSITORY, RUBY, BUNDLE, REPOSITORY / "storage/db/production.sqlite3", camp_db, [], camp_port, temp)
        environment["REDIS_URL"] = f"redis://127.0.0.1:{redis_port}"
        with sqlite3.connect(camp_db) as db:
            digest = db.execute("SELECT password_digest FROM users WHERE id=1").fetchone()[0]
        with sqlite3.connect(rust_db) as db:
            db.execute("UPDATE users SET email_address='benchmark@example.invalid',password_digest=? WHERE id=1", [digest])
        for database in (rust_db, camp_db):
            with sqlite3.connect(database) as db:
                db.execute("DELETE FROM push_subscriptions")
                db.executemany("INSERT INTO push_subscriptions(user_id,endpoint,p256dh_key,auth_key,created_at,updated_at) VALUES(?1,?2,'key','auth','2026-01-01 00:00:00','2026-01-01 00:00:00')", [
                    (1, ENDPOINT),
                    (1, "https://fcm.googleapis.com/fcm/send/other-device"),
                    (2, ENDPOINT),
                ])
        redis, redis_log = start_redis(temp, redis_port)
        try:
            rust = start_server(rust_db, rust_port, {"RUSTFIRE_CAMPFIRE_SECRET_KEY_BASE": environment["SECRET_KEY_BASE"]})
            try:
                with open(temp / "puma.log", "w+") as log:
                    camp = subprocess.Popen([str(RUBY), str(RUBY.parent / "bundle"), "exec", "puma", "-C", "config/puma.rb"], cwd=REPOSITORY, env=environment, stdout=log, stderr=log)
                    try:
                        wait_for_server(camp_port, camp)
                        check_browser(sessions[0], rust_port, rust_db)
                        check_browser(sessions[1], camp_port, camp_db)
                    finally:
                        stop_server(camp)
            finally:
                stop_server(rust)
        finally:
            for session in sessions:
                subprocess.run(["agent-browser", "--session", session, "close"], capture_output=True, timeout=15)
            redis.terminate()
            redis.wait(timeout=10)
            redis_log.close()
    print("PASS clicked profile sign-out unsubscribes this browser and removes only its user's endpoint")


if __name__ == "__main__":
    main()
