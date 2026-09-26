"""Compare clicked message replies in Chromium against the pinned Campfire app.

Requires the release Rustfire build and the agent-browser CLI. The fixture posts
one message with text and a link preview, then one with only the preview.
"""

import json
import pathlib
import re
import shutil
import sqlite3
import subprocess
import tempfile
import time
import uuid

from direct_lookup import free_port, start_server, stop_server
from paired_banned_content import BUNDLE, REPOSITORY, REVISION, RUBY, start_redis
from paired_direct_lookup import login_campfire, seed_campfire, seed_rustfire, wait_for_server
from paired_link_preview import CASES, post


def browser(session, *arguments):
    result = subprocess.run(
        ["agent-browser", "--session", session, *arguments],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode:
        raise RuntimeError(f"agent-browser {arguments!r}: {result.stdout}\n{result.stderr}")
    return result.stdout


def reply_result(session, port, database, message_id):
    page = f"http://127.0.0.1:{port}"
    browser(session, "open", f"{page}/session/new")
    assert 'button "Go"' in browser(session, "snapshot", "-i")
    browser(session, "fill", 'input[name="email_address"]', "benchmark@example.invalid")
    browser(session, "fill", 'input[name="password"]', "benchmark-password")
    browser(session, "click", 'button[name="log_in"]')
    browser(session, "wait", "--url", "**/rooms/1")
    browser(session, "wait", "#composer trix-editor")
    browser(session, "wait", "--load", "networkidle")
    snapshot = browser(session, "snapshot", "-i")
    assert 'textbox "Write a message"' in snapshot, snapshot

    results = {}
    for name in ("preview-only", "with-text"):
        client_id = f"paired-link-preview-{name}"
        browser(session, "click", f"#message_{client_id} details summary")
        snapshot = browser(session, "snapshot", "-i", "-C")
        assert 'button "Reply"' in snapshot, snapshot
        browser(session, "click", f'#message_{client_id} [data-action~="reply#reply"]')
        result = json.loads(browser(session, "eval", """(() => {
          const editor = document.querySelector('#composer trix-editor');
          const quote = editor?.querySelector('blockquote');
          const cite = editor?.querySelector('cite');
          const link = cite?.querySelector('a');
          const preview = document.querySelector('#message_paired-link-preview-%s [data-reply-target="body"] .og-embed a');
          return {html: editor?.innerHTML, quote: quote?.textContent,
                  cite: cite?.textContent, href: link?.href,
                  focused: document.activeElement === editor, previewTarget: preview?.target};
        })()""" % name))
        assert result["focused"] and result["previewTarget"] == "_blank", result
        assert result["quote"].replace("\xa0", " ") == ("https://example.com/page" if name == "preview-only" else "Link "), result
        assert result["cite"] == "Test Admin #", result
        assert result["href"] == f"{page}/rooms/1/@{message_id[name]}", result
        results[name] = re.sub(
            r"http://127\.0\.0\.1:\d+/rooms/1/@\d+", "<original-message>",
            result["html"],
        )
        if name == "preview-only":
            browser(session, "click", '#composer button[type="submit"]')
            client_id = None
            for _ in range(100):
                with sqlite3.connect(database) as db:
                    row = db.execute("SELECT client_message_id FROM messages WHERE client_message_id NOT IN ('paired-link-preview-with-text','paired-link-preview-preview-only') ORDER BY id DESC LIMIT 1").fetchone()
                if row:
                    client_id = row[0]
                    break
                time.sleep(0.1)
            assert client_id, "Browser reply was not saved"
            browser(session, "wait", f"#message_{client_id} [data-reply-target=body]")
            posted = json.loads(browser(session, "eval", """(() => {
              const body = document.querySelector('#message_%s [data-reply-target="body"]');
              const quote = body?.querySelector('blockquote');
              const cite = body?.querySelector('cite');
              return {quote:quote?.textContent,cite:cite?.textContent,
                      href:cite?.querySelector('a')?.href};
            })()""" % client_id))
            assert posted == {"quote": "https://example.com/page", "cite": "Test Admin #", "href": f"{page}/rooms/1/@{message_id[name]}"}, posted
    return results


def main():
    assert shutil.which("agent-browser"), "agent-browser CLI is required"
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPOSITORY, text=True).strip() == REVISION
    sessions = [f"reply-rust-{uuid.uuid4().hex[:8]}", f"reply-camp-{uuid.uuid4().hex[:8]}"]
    with tempfile.TemporaryDirectory(prefix="paired-reply-browser-") as scratch:
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
                    camp = subprocess.Popen(
                        [str(RUBY), str(RUBY.parent / "bundle"), "exec", "puma", "-C", "config/puma.rb"],
                        cwd=REPOSITORY, env=environment, stdout=log, stderr=log,
                    )
                    try:
                        wait_for_server(camp_port, camp)
                        camp_cookie, camp_csrf = login_campfire(camp_port)
                        for case in CASES[:2]:
                            assert post(rust_port, "session_token=benchmark-session", "benchmark-csrf", case) == post(camp_port, camp_cookie, camp_csrf, case)
                        with sqlite3.connect(rust_db) as rust_db_conn, sqlite3.connect(camp_db) as camp_db_conn:
                            ids = {
                                "rust": {name: rust_db_conn.execute("SELECT id FROM messages WHERE client_message_id=?", [f"paired-link-preview-{name}"]).fetchone()[0] for name in ("preview-only", "with-text")},
                                "camp": {name: camp_db_conn.execute("SELECT id FROM messages WHERE client_message_id=?", [f"paired-link-preview-{name}"]).fetchone()[0] for name in ("preview-only", "with-text")},
                            }
                        rust_result = reply_result(sessions[0], rust_port, rust_db, ids["rust"])
                        camp_result = reply_result(sessions[1], camp_port, camp_db, ids["camp"])
                        assert rust_result == camp_result, (rust_result, camp_result)
                    finally:
                        for session in sessions:
                            subprocess.run(["agent-browser", "--session", session, "close"], capture_output=True, timeout=15)
                        stop_server(camp)
            finally:
                stop_server(rust)
        finally:
            redis.terminate()
            redis.wait(timeout=10)
            redis_log.close()
    print("PASS clicked preview-only and text-plus-preview replies match Campfire in Chromium")


if __name__ == "__main__":
    main()
