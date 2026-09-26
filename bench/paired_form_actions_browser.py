"""Compare keyboard form actions and avatar autosubmit with pinned Campfire."""

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


AVATAR = REPOSITORY / "test/fixtures/files/moon.jpg"


def seed_message(rust_db, camp_db):
    with sqlite3.connect(camp_db) as camp:
        password, name = camp.execute("SELECT password_digest,name FROM users WHERE id=1").fetchone()
        account = camp.execute("SELECT name,updated_at FROM accounts WHERE id=1").fetchone()
        room_name = camp.execute("SELECT name FROM rooms WHERE id=1").fetchone()[0]
        stamp = "2026-01-01 13:45:12.123456"
        camp.execute("INSERT INTO messages(id,room_id,creator_id,client_message_id,created_at,updated_at) VALUES(1,1,1,'form-actions',?,?)", (stamp, stamp))
        camp.execute("INSERT INTO action_text_rich_texts(name,body,record_type,record_id,created_at,updated_at) VALUES('body','Original form message','Message',1,?,?)", (stamp, stamp))
    with sqlite3.connect(rust_db) as rust:
        rust.execute("UPDATE users SET email_address='benchmark@example.invalid',password_digest=?,name=? WHERE id=1", (password, name))
        rust.execute("UPDATE accounts SET name=?,updated_at=? WHERE id=1", account)
        rust.execute("UPDATE rooms SET name=? WHERE id=1", [room_name])
        stamp = "2026-01-01T13:45:12.123456Z"
        rust.execute("INSERT INTO messages(id,room_id,creator_id,body,client_message_id,created_at,updated_at) VALUES(1,1,1,'Original form message','form-actions',?,?)", (stamp, stamp))


def wait_database(database, statement, expected, seconds=8):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        with sqlite3.connect(database) as db:
            value = db.execute(statement).fetchone()[0]
        if value == expected:
            return
        time.sleep(0.1)
    raise AssertionError((database, statement, expected, value))


def check_browser(session, port, database, campfire):
    browser(session, "open", f"http://127.0.0.1:{port}/session/new")
    assert 'button "Go"' in browser(session, "snapshot", "-i")
    browser(session, "fill", 'input[name="email_address"]', "benchmark@example.invalid")
    browser(session, "fill", 'input[name="password"]', "benchmark-password")
    browser(session, "click", 'button[name="log_in"]')
    browser(session, "wait", "--url", "**/rooms/1")
    browser(session, "snapshot", "-i")
    browser(session, "wait", "#message_form-actions .message__body-content")

    def open_editor():
        browser(session, "scrollintoview", "#message_form-actions .message__actions details summary")
        browser(session, "click", "#message_form-actions .message__actions details summary")
        browser(session, "snapshot", "-i")
        browser(session, "scrollintoview", "#message_form-actions .message__edit-btn")
        browser(session, "eval", "document.querySelector('#message_form-actions .message__edit-btn').click()")
        browser(session, "wait", '#message_form-actions form[data-action*="form#cancel"] trix-editor')
        browser(session, "snapshot", "-i")

    open_editor()
    browser(session, "fill", '#message_form-actions form[data-action*="form#cancel"] trix-editor', "Discard this change")
    browser(session, "press", "Escape")
    browser(session, "wait", '#message_form-actions .message__body-content .trix-content')
    with sqlite3.connect(database) as db:
        body = db.execute("SELECT body FROM action_text_rich_texts WHERE record_type='Message' AND record_id=1" if campfire else "SELECT body FROM messages WHERE id=1").fetchone()[0]
    assert "Original form message" in body and "Discard this change" not in body, body

    open_editor()
    browser(session, "eval", "(() => { const editor=document.querySelector('#message_form-actions form[data-action*=\"form#cancel\"] trix-editor'); editor.editor.setSelectedRange([0,editor.editor.getDocument().toString().length]); editor.editor.insertString('Saved by keyboard'); editor.focus(); })()")
    browser(session, "press", "Control+Enter")
    statement = "SELECT body FROM action_text_rich_texts WHERE record_type='Message' AND record_id=1" if campfire else "SELECT body FROM messages WHERE id=1"
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        with sqlite3.connect(database) as db:
            body = db.execute(statement).fetchone()[0]
        if "Saved by keyboard" in body:
            break
        time.sleep(0.1)
    assert "Saved by keyboard" in body, body
    browser(session, "wait", '#message_form-actions .message__body-content .trix-content')

    open_editor()
    browser(session, "eval", "window.__confirmationTexts=[];window.confirm=message=>{window.__confirmationTexts.push(message);return false}")
    browser(session, "eval", "document.querySelector('#message_form-actions button[data-turbo-confirm]').click()")
    confirmations = json.loads(browser(session, "eval", "window.__confirmationTexts"))
    assert confirmations == ["Are you sure you want to delete this message?"], confirmations
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT COUNT(*) FROM messages WHERE id=1").fetchone() == (1,)
    browser(session, "press", "Escape")

    browser(session, "open", f"http://127.0.0.1:{port}/users/me/profile")
    browser(session, "snapshot", "-i")
    browser(session, "wait", '.avatar__form form input[type="file"]')
    browser(session, "upload", '.avatar__form form:first-of-type input[type="file"]', str(AVATAR))
    wait_database(database, "SELECT COUNT(*) FROM active_storage_attachments WHERE record_type='User' AND record_id=1 AND name='avatar'" if campfire else "SELECT COUNT(*) FROM avatars WHERE user_id=1", 1)
    browser(session, "wait", "--load", "networkidle")

    browser(session, "open", f"http://127.0.0.1:{port}/account/custom_styles/edit")
    browser(session, "snapshot", "-i")
    browser(session, "fill", "#account_custom_styles", "body { color: #123456; }")
    browser(session, "press", "Control+Enter")
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        with sqlite3.connect(database) as db:
            css = db.execute("SELECT custom_styles FROM accounts WHERE id=1").fetchone()[0]
        if css == "body { color: #123456; }":
            break
        time.sleep(0.1)
    assert css == "body { color: #123456; }", css

    browser(session, "open", f"http://127.0.0.1:{port}/rooms/1")
    browser(session, "wait", "#message_form-actions .message__body-content")
    open_editor()
    browser(session, "eval", "window.__confirmationTexts=[];window.confirm=message=>{window.__confirmationTexts.push(message);return true}")
    browser(session, "eval", "document.querySelector('#message_form-actions button[data-turbo-confirm]').click()")
    wait_database(database, "SELECT COUNT(*) FROM messages WHERE id=1", 0)
    confirmations = json.loads(browser(session, "eval", "window.__confirmationTexts"))
    assert confirmations == ["Are you sure you want to delete this message?"], confirmations
    assert not browser(session, "errors").strip(), browser(session, "errors")


def main():
    assert shutil.which("agent-browser"), "agent-browser CLI is required"
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPOSITORY, text=True).strip() == REVISION
    assert AVATAR.is_file()
    sessions = [f"form-actions-rust-{uuid.uuid4().hex[:8]}", f"form-actions-camp-{uuid.uuid4().hex[:8]}"]
    with tempfile.TemporaryDirectory(prefix="paired-form-actions-browser-") as scratch:
        temp = pathlib.Path(scratch)
        rust_db, camp_db = temp / "rust.sqlite3", temp / "camp.sqlite3"
        rust_port, camp_port, redis_port = free_port(), free_port(), free_port()
        seed_rustfire(rust_db, rust_port, [])
        environment = seed_campfire(REPOSITORY, RUBY, BUNDLE, REPOSITORY / "storage/db/production.sqlite3", camp_db, [], camp_port, temp)
        environment["REDIS_URL"] = f"redis://127.0.0.1:{redis_port}"
        seed_message(rust_db, camp_db)
        redis, redis_log = start_redis(temp, redis_port)
        try:
            rust = start_server(rust_db, rust_port, {"RUSTFIRE_CAMPFIRE_SECRET_KEY_BASE": environment["SECRET_KEY_BASE"], "RUSTFIRE_UPLOAD_DIR": str(temp / "uploads")})
            try:
                with open(temp / "puma.log", "w+") as log:
                    camp = subprocess.Popen([str(RUBY), str(RUBY.parent / "bundle"), "exec", "puma", "-C", "config/puma.rb"], cwd=REPOSITORY, env=environment, stdout=log, stderr=log)
                    try:
                        wait_for_server(camp_port, camp)
                        check_browser(sessions[0], rust_port, rust_db, False)
                        check_browser(sessions[1], camp_port, camp_db, True)
                    finally:
                        stop_server(camp)
                        cleanup_campfire_uploads(REPOSITORY / "storage/db/production.sqlite3", camp_db, REPOSITORY)
            finally:
                stop_server(rust)
        finally:
            for session in sessions:
                subprocess.run(["agent-browser", "--session", session, "close"], capture_output=True, timeout=15)
            redis.terminate()
            redis.wait(timeout=10)
            redis_log.close()
    print("PASS message edit shortcuts, confirmed/cancelled delete, avatar autosubmit, and custom CSS shortcut match Campfire in Chromium")


if __name__ == "__main__":
    main()
