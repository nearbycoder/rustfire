"""Compare room tab visibility, offline state, and missed-message catch-up."""

from datetime import datetime, timezone
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
from paired_bot_admin import cleanup_campfire_uploads
from paired_direct_lookup import seed_campfire, seed_rustfire, wait_for_server
from paired_reply_browser import browser


def field_disabled(session):
    return browser(session, "eval", "document.querySelector('#composer [data-composer-target=fields]').disabled").strip() == "true"


def wait_for(session, expected, seconds=20):
    for _ in range(seconds * 4):
        if field_disabled(session) == expected:
            return
        time.sleep(0.25)
    raise AssertionError((expected, field_disabled(session), browser(session, "eval", "window.__connectionEvents"), browser(session, "errors")))


def seed_missed(database, rails):
    now = datetime.now(timezone.utc)
    stamp = now.strftime("%Y-%m-%d %H:%M:%S.%f") if rails else now.isoformat()
    with sqlite3.connect(database) as db:
        message_id = db.execute("SELECT COALESCE(MAX(id),0)+1 FROM messages").fetchone()[0]
        if rails:
            db.execute("INSERT INTO messages(id,room_id,creator_id,client_message_id,created_at,updated_at) VALUES(?1,1,1,'browser-missed',?2,?2)", (message_id, stamp))
            db.execute("INSERT INTO action_text_rich_texts(name,body,record_type,record_id,created_at,updated_at) VALUES('body','Missed while offline','Message',?1,?2,?2)", (message_id, stamp))
        else:
            db.execute("INSERT INTO messages(id,room_id,creator_id,client_message_id,body,created_at,updated_at) VALUES(?1,1,1,'browser-missed','Missed while offline',?2,?2)", (message_id, stamp))


def membership(database):
    with sqlite3.connect(database) as db:
        return db.execute("SELECT connections,unread_at FROM memberships WHERE room_id=1 AND user_id=1").fetchone()


def wait_connections(database, expected, seconds=15):
    for _ in range(seconds * 4):
        result = membership(database)
        if result[0] == expected:
            return result
        time.sleep(0.25)
    raise AssertionError((expected, result))


def set_unread(database, rails):
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f" if rails else "%Y-%m-%dT%H:%M:%S.%fZ")
    with sqlite3.connect(database) as db:
        db.execute("UPDATE memberships SET unread_at=? WHERE room_id=1 AND user_id=1", (stamp,))


def wait_post(database):
    for _ in range(100):
        with sqlite3.connect(database) as db:
            row = db.execute("SELECT client_message_id FROM messages ORDER BY id DESC LIMIT 1").fetchone()
        if row:
            return row[0]
        time.sleep(0.1)
    raise AssertionError("Browser message was not saved")


def check(session, port, database, rails, holder, restart):
    browser(session, "open", f"http://127.0.0.1:{port}/session/new")
    browser(session, "fill", 'input[name="email_address"]', "benchmark@example.invalid")
    browser(session, "fill", 'input[name="password"]', "benchmark-password")
    browser(session, "click", 'button[name="log_in"]')
    browser(session, "wait", "--url", "**/rooms/1")
    browser(session, "wait", "#composer trix-editor")
    browser(session, "wait", "--load", "networkidle")
    browser(session, "eval", "window.__connectionEvents=[];window.addEventListener('refresh-room:offline',()=>window.__connectionEvents.push('offline'));window.addEventListener('refresh-room:online',()=>window.__connectionEvents.push('online'))")
    wait_for(session, False)
    wait_connections(database, 1)
    assert browser(session, "eval", "!!document.querySelector('#message_browser-missed')").strip() == "false"

    browser(session, "fill", "#composer trix-editor", "Current room message")
    browser(session, "press", "Enter")
    posted_id = wait_post(database)
    browser(session, "wait", f"#message_{posted_id}[data-message-id]")
    time.sleep(0.5)
    assert browser(session, "eval", "document.querySelector('#sidebar a[href=\"/rooms/1\"]')?.classList.contains('unread')").strip() == "false"

    set_unread(database, rails)
    browser(session, "tab", "new", "about:blank")
    time.sleep(1)
    browser(session, "tab", "close")
    browser(session, "tab", "t1")
    time.sleep(6)
    assert membership(database)[0] == 1 and membership(database)[1] is not None, membership(database)

    browser(session, "tab", "new", "about:blank")
    wait_connections(database, 0)
    set_unread(database, rails)
    browser(session, "tab", "close")
    browser(session, "tab", "t1")
    wait_connections(database, 1)
    assert membership(database)[1] is None, membership(database)

    stop_server(holder["process"])
    holder["process"] = None
    wait_for(session, True, 15)
    seed_missed(database, rails)
    holder["process"] = restart()
    wait_for(session, False, 25)
    browser(session, "wait", "#message_browser-missed[data-message-id]")
    assert "Missed while offline" in browser(session, "get", "text", "#message_browser-missed")
    assert json.loads(browser(session, "eval", "window.__connectionEvents")) == ["offline", "online"]
    assert not browser(session, "errors").strip(), browser(session, "errors")


def main():
    assert shutil.which("agent-browser"), "agent-browser CLI is required"
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPOSITORY, text=True).strip() == REVISION
    sessions = [f"connection-rust-{uuid.uuid4().hex[:8]}", f"connection-camp-{uuid.uuid4().hex[:8]}"]
    with tempfile.TemporaryDirectory(prefix="paired-connection-browser-") as scratch:
        temp = pathlib.Path(scratch)
        rust_db, camp_db = temp / "rust.sqlite3", temp / "camp.sqlite3"
        rust_port, camp_port, redis_port = free_port(), free_port(), free_port()
        seed_rustfire(rust_db, rust_port, [])
        environment = seed_campfire(REPOSITORY, RUBY, BUNDLE, REPOSITORY / "storage/db/production.sqlite3", camp_db, [], camp_port, temp)
        environment["REDIS_URL"] = f"redis://127.0.0.1:{redis_port}"
        with sqlite3.connect(camp_db) as camp:
            password, name, updated_at = camp.execute("SELECT password_digest,name,updated_at FROM users WHERE id=1").fetchone()
            account = camp.execute("SELECT name,updated_at FROM accounts WHERE id=1").fetchone()
            room_name = camp.execute("SELECT name FROM rooms WHERE id=1").fetchone()[0]
        with sqlite3.connect(rust_db) as rust:
            rust.execute("UPDATE users SET email_address='benchmark@example.invalid',password_digest=?,name=?,updated_at=? WHERE id=1", (password, name, updated_at))
            rust.execute("UPDATE accounts SET name=?,updated_at=? WHERE id=1", account)
            rust.execute("UPDATE rooms SET name=? WHERE id=1", [room_name])
        redis, redis_log = start_redis(temp, redis_port)
        try:
            def start_rust():
                return start_server(rust_db, rust_port, {"RUSTFIRE_CAMPFIRE_SECRET_KEY_BASE": environment["SECRET_KEY_BASE"]})
            rust = {"process": start_rust()}
            try:
                with open(temp / "puma.log", "w+") as log:
                    def start_camp():
                        pathlib.Path(environment["PIDFILE"]).unlink(missing_ok=True)
                        process = subprocess.Popen([str(RUBY), str(RUBY.parent / "bundle"), "exec", "puma", "-C", "config/puma.rb"],
                                                   cwd=REPOSITORY, env=environment, stdout=log, stderr=log)
                        wait_for_server(camp_port, process)
                        return process
                    camp = {"process": start_camp()}
                    try:
                        check(sessions[0], rust_port, rust_db, False, rust, start_rust)
                        check(sessions[1], camp_port, camp_db, True, camp, start_camp)
                    finally:
                        for session in sessions:
                            subprocess.run(["agent-browser", "--session", session, "close"], capture_output=True, timeout=15)
                        if camp["process"] is not None:
                            stop_server(camp["process"])
                        cleanup_campfire_uploads(REPOSITORY / "storage/db/production.sqlite3", camp_db, REPOSITORY)
            finally:
                if rust["process"] is not None:
                    stop_server(rust["process"])
        finally:
            redis.terminate()
            redis.wait(timeout=10)
            redis_log.close()
    print("PASS current-room unread, brief and long tab visibility, offline composer, and reconnect match Campfire")


if __name__ == "__main__":
    main()
