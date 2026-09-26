"""Compare Campfire's composer keyboard shortcuts in paired Chromium sessions."""

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


def rows(database, rails):
    with sqlite3.connect(database) as db:
        if rails:
            return db.execute("""SELECT m.client_message_id, COALESCE(t.body,'') FROM messages m
                LEFT JOIN action_text_rich_texts t ON t.record_type='Message' AND t.record_id=m.id
                    AND t.name='body' ORDER BY m.id""").fetchall()
        return db.execute("SELECT client_message_id, body FROM messages ORDER BY id").fetchall()


def await_rows(database, rails, count):
    for _ in range(100):
        saved = rows(database, rails)
        if len(saved) == count:
            return saved
        time.sleep(0.1)
    raise AssertionError((count, saved))


def state(session):
    return json.loads(browser(session, "eval", """(() => ({
      toolbar:document.querySelector('#composer').classList.contains('composer--rich-text'),
      empty:document.querySelector('#composer trix-editor').matches(':empty'),
      editorText:document.querySelector('#composer trix-editor').textContent,
      editFocused:!!document.querySelector('.messages .composer--edit trix-editor:focus')
    }))()"""))


def check(session, port, database, rails):
    browser(session, "set", "viewport", "1440", "900")
    browser(session, "open", f"http://127.0.0.1:{port}/session/new")
    browser(session, "fill", 'input[name="email_address"]', "benchmark@example.invalid")
    browser(session, "fill", 'input[name="password"]', "benchmark-password")
    browser(session, "click", 'button[name="log_in"]')
    browser(session, "wait", "--url", "**/rooms/1")
    browser(session, "wait", "#composer trix-editor")
    browser(session, "wait", "--load", "networkidle")

    browser(session, "fill", "#composer trix-editor", "Plain Enter")
    browser(session, "press", "Enter")
    first = await_rows(database, rails, 1)
    browser(session, "wait", f"#message_{first[0][0]}[data-message-id]")
    plain = state(session)
    assert plain["empty"] and not plain["toolbar"], plain

    browser(session, "fill", "#composer trix-editor", "Shift Enter")
    browser(session, "press", "Shift+Enter")
    time.sleep(0.25)
    shifted = state(session)
    assert len(rows(database, rails)) == 1 and "Shift Enter" in shifted["editorText"], shifted
    browser(session, "press", "Enter")
    second = await_rows(database, rails, 2)
    browser(session, "wait", f"#message_{second[-1][0]}[data-message-id]")

    # The headless browser advertises no fine pointer, which hides this desktop button.
    browser(session, "eval", "document.querySelector('#composer .composer__rich-text-btn').click()")
    opened = state(session)
    assert opened["toolbar"], opened
    browser(session, "fill", "#composer trix-editor", "Rich Enter")
    filled = state(session)
    assert filled["toolbar"], filled
    browser(session, "press", "Enter")
    time.sleep(0.25)
    rich = state(session)
    assert len(rows(database, rails)) == 2 and rich["toolbar"] and "Rich Enter" in rich["editorText"], rich
    browser(session, "press", "Control+Enter")
    third = await_rows(database, rails, 3)
    browser(session, "wait", f"#message_{third[-1][0]}[data-message-id]")
    modified = state(session)
    assert modified["empty"] and not modified["toolbar"], modified

    browser(session, "focus", "#composer trix-editor")
    browser(session, "press", "ArrowUp")
    browser(session, "wait", f"#message_{third[-1][0]} .composer--edit")
    edit = state(session)
    assert edit["editFocused"], edit
    return {"sent": 3, "plain": plain["empty"], "shiftHeld": "Shift Enter" in shifted["editorText"],
            "richHeld": rich["toolbar"], "modifiedSent": modified["empty"], "upEdited": edit["editFocused"]}


def main():
    assert shutil.which("agent-browser"), "agent-browser CLI is required"
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPOSITORY, text=True).strip() == REVISION
    sessions = [f"keyboard-rust-{uuid.uuid4().hex[:8]}", f"keyboard-camp-{uuid.uuid4().hex[:8]}"]
    with tempfile.TemporaryDirectory(prefix="paired-keyboard-browser-") as scratch:
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
            rust = start_server(rust_db, rust_port, {"RUSTFIRE_CAMPFIRE_SECRET_KEY_BASE": environment["SECRET_KEY_BASE"]})
            try:
                with open(temp / "puma.log", "w+") as log:
                    camp = subprocess.Popen([str(RUBY), str(RUBY.parent / "bundle"), "exec", "puma", "-C", "config/puma.rb"],
                                            cwd=REPOSITORY, env=environment, stdout=log, stderr=log)
                    try:
                        wait_for_server(camp_port, camp)
                        rust_result = check(sessions[0], rust_port, rust_db, False)
                        camp_result = check(sessions[1], camp_port, camp_db, True)
                        assert rust_result == camp_result, (rust_result, camp_result)
                    finally:
                        for session in sessions:
                            subprocess.run(["agent-browser", "--session", session, "close"], capture_output=True, timeout=15)
                        stop_server(camp)
                        cleanup_campfire_uploads(REPOSITORY / "storage/db/production.sqlite3", camp_db, REPOSITORY)
            finally:
                stop_server(rust)
        finally:
            redis.terminate()
            redis.wait(timeout=10)
            redis_log.close()
    print("PASS plain Enter, Shift+Enter, rich Enter, Ctrl+Enter, and Up edit match Campfire")


if __name__ == "__main__":
    main()
