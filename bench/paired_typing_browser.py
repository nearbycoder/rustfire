"""Compare live typing indicators for two other users in paired Chromium rooms."""

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


NAMES = {1: "Viewer", 2: "Zed", 3: "Amy"}


def prepare_users(rust_db, camp_db):
    with sqlite3.connect(camp_db) as camp:
        password = camp.execute("SELECT password_digest FROM users WHERE id=1").fetchone()[0]
        account = camp.execute("SELECT name,updated_at FROM accounts WHERE id=1").fetchone()
        room_name = camp.execute("SELECT name FROM rooms WHERE id=1").fetchone()[0]
        for uid, name in NAMES.items():
            camp.execute("UPDATE users SET name=?,email_address=?,password_digest=? WHERE id=?",
                         (name, f"typing-{uid}@example.invalid", password, uid))
        camp.execute("INSERT OR IGNORE INTO memberships(room_id,user_id,involvement,created_at,updated_at) VALUES(1,3,'everything','2026-01-01','2026-01-01')")
    with sqlite3.connect(rust_db) as rust:
        rust.execute("UPDATE accounts SET name=?,updated_at=? WHERE id=1", account)
        rust.execute("UPDATE rooms SET name=? WHERE id=1", (room_name,))
        for uid, name in NAMES.items():
            rust.execute("UPDATE users SET name=?,email_address=?,password_digest=? WHERE id=?",
                         (name, f"typing-{uid}@example.invalid", password, uid))
        rust.execute("INSERT OR IGNORE INTO memberships(room_id,user_id,involvement,created_at) VALUES(1,3,'everything','2026-01-01')")


def login(session, port, uid):
    browser(session, "open", f"http://127.0.0.1:{port}/session/new")
    browser(session, "fill", 'input[name="email_address"]', f"typing-{uid}@example.invalid")
    browser(session, "fill", 'input[name="password"]', "benchmark-password")
    browser(session, "click", 'button[name="log_in"]')
    browser(session, "wait", "--url", "**/rooms/1")
    browser(session, "wait", "#composer trix-editor")
    browser(session, "wait", "--load", "networkidle")
    observed = json.loads(browser(session, "eval", "document.querySelector('meta[name=current-user-id]')?.content"))
    assert observed == str(uid), (uid, observed)


def indicator(session):
    return json.loads(browser(session, "eval", """(() => {
      const indicator=document.querySelector('[data-typing-notifications-target="indicator"]');
      return {text:indicator.querySelector('[data-typing-notifications-target="author"]').textContent,
              active:indicator.classList.contains('typing-indicator--active')};
    })()"""))


def wait_indicator(session, expected, seconds=8):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        result = indicator(session)
        if result == {"text": expected, "active": bool(expected)}:
            return result
        time.sleep(0.2)
    raise AssertionError((expected, result, browser(session, "errors")))


def change_editor(session, text):
    browser(session, "fill", "#composer trix-editor", text)
    browser(session, "eval", "document.querySelector('#composer trix-editor').dispatchEvent(new Event('trix-change',{bubbles:true}))")
    document_text = json.loads(browser(session, "eval", "document.querySelector('#composer trix-editor').editor?.getDocument().toString().trim()"))
    assert document_text == text, (text, document_text)


def check(prefix, port):
    sessions = {uid: f"typing-{prefix}-{uid}-{uuid.uuid4().hex[:8]}" for uid in NAMES}
    try:
        for uid, session in sessions.items():
            login(session, port, uid)
        viewer, zed, amy = (sessions[uid] for uid in (1, 2, 3))
        states = [wait_indicator(viewer, "")]
        change_editor(zed, "Zed typing")
        states.append(wait_indicator(viewer, "Zed"))
        time.sleep(3)
        wait_indicator(viewer, "Zed")
        change_editor(amy, "Amy typing")
        states.append(wait_indicator(viewer, "Amy, Zed"))
        change_editor(zed, "")
        states.append(wait_indicator(viewer, "Amy"))
        change_editor(amy, "")
        states.append(wait_indicator(viewer, ""))
        # Send once on a separate cable subscription, with no editor blur or stop event.
        browser(zed, "eval", "window.__idleTypingSocket=new WebSocket(`${location.protocol==='https:'?'wss':'ws'}://${location.host}/cable`,'actioncable-v1-json');window.__idleTypingSocket.addEventListener('open',()=>window.__idleTypingSocket.send(JSON.stringify({command:'subscribe',identifier:JSON.stringify({channel:'TypingNotificationsChannel',room_id:1})})));window.__idleTypingSocket.addEventListener('message',event=>{const frame=JSON.parse(event.data);if(frame.type==='confirm_subscription')window.__idleTypingSocket.send(JSON.stringify({command:'message',identifier:frame.identifier,data:JSON.stringify({action:'start'})}))})")
        states.append(wait_indicator(viewer, "Zed"))
        time.sleep(3)
        wait_indicator(viewer, "Zed")
        states.append(wait_indicator(viewer, "", seconds=5))
        for session in sessions.values():
            assert not browser(session, "errors").strip(), browser(session, "errors")
        return states
    finally:
        for session in sessions.values():
            subprocess.run(["agent-browser", "--session", session, "close"], capture_output=True, timeout=15)


def main():
    assert shutil.which("agent-browser"), "agent-browser CLI is required"
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPOSITORY, text=True).strip() == REVISION
    with tempfile.TemporaryDirectory(prefix="paired-typing-browser-") as scratch:
        temp = pathlib.Path(scratch)
        rust_db, camp_db = temp / "rust.sqlite3", temp / "camp.sqlite3"
        rust_port, camp_port, redis_port = free_port(), free_port(), free_port()
        seed_rustfire(rust_db, rust_port, [])
        environment = seed_campfire(REPOSITORY, RUBY, BUNDLE, REPOSITORY / "storage/db/production.sqlite3", camp_db, [], camp_port, temp)
        environment["REDIS_URL"] = f"redis://127.0.0.1:{redis_port}"
        prepare_users(rust_db, camp_db)
        redis, redis_log = start_redis(temp, redis_port)
        try:
            rust = start_server(rust_db, rust_port, {"RUSTFIRE_CAMPFIRE_SECRET_KEY_BASE": environment["SECRET_KEY_BASE"]})
            try:
                with open(temp / "puma.log", "w+") as log:
                    camp = subprocess.Popen([str(RUBY), str(RUBY.parent / "bundle"), "exec", "puma", "-C", "config/puma.rb"],
                                            cwd=REPOSITORY, env=environment, stdout=log, stderr=log)
                    try:
                        wait_for_server(camp_port, camp)
                        rust_result = check("rust", rust_port)
                        camp_result = check("camp", camp_port)
                        assert rust_result == camp_result, (rust_result, camp_result)
                    finally:
                        stop_server(camp)
                        cleanup_campfire_uploads(REPOSITORY / "storage/db/production.sqlite3", camp_db, REPOSITORY)
            finally:
                stop_server(rust)
        finally:
            redis.terminate()
            redis.wait(timeout=10)
            redis_log.close()
    print("PASS sorted typing names, start/stop, and idle expiration match Campfire in Chromium")


if __name__ == "__main__":
    main()
