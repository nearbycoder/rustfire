"""Compare queued files and a text-plus-two-file composer send in Chromium."""

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
from paired_bot_admin import cleanup_campfire_uploads
from paired_direct_lookup import seed_campfire, seed_rustfire, wait_for_server
from paired_reply_browser import browser


def preview(session):
    return json.loads(browser(session, "eval", """(() => [...document.querySelectorAll('#composer [data-composer-target="fileList"] > *')].map((card, index) => ({
      tag:card.tagName.toLowerCase(),classes:card.className,action:card.dataset.action,
      fileIndex:card.dataset.composerIndexParam,style:card.getAttribute('style'),
      thumbTag:card.firstElementChild?.tagName.toLowerCase(),
      thumbClasses:card.firstElementChild?.className,
      thumbRole:card.firstElementChild?.getAttribute('role'),
      thumbBlob:card.firstElementChild?.getAttribute('src')?.startsWith('blob:'),
      captionClasses:card.lastElementChild?.className,
      name:card.lastElementChild?.textContent,
      captionParts:[...card.lastElementChild?.children||[]].map(node=>({classes:node.className,text:node.textContent}))
    })))()"""))


def saved(database, rails):
    with sqlite3.connect(database) as db:
        if rails:
            rows = db.execute("""SELECT m.id,m.client_message_id,COALESCE(t.body,''),b.filename
                FROM messages m LEFT JOIN action_text_rich_texts t ON t.record_type='Message' AND t.record_id=m.id AND t.name='body'
                LEFT JOIN active_storage_attachments a ON a.record_type='Message' AND a.record_id=m.id AND a.name='attachment'
                LEFT JOIN active_storage_blobs b ON b.id=a.blob_id ORDER BY m.id""").fetchall()
        else:
            rows = db.execute("""SELECT m.id,m.client_message_id,m.body,a.filename
                FROM messages m LEFT JOIN attachments a ON a.message_id=m.id ORDER BY m.id""").fetchall()
    return rows


def verify_uploads(database, upload_directory, rails):
    with sqlite3.connect(database) as db:
        if rails:
            files = db.execute("""SELECT b.filename,b.key FROM active_storage_attachments a
                JOIN active_storage_blobs b ON b.id=a.blob_id
                WHERE a.record_type='Message' AND a.name='attachment'""").fetchall()
        else:
            files = db.execute("SELECT filename,stored_name FROM attachments").fetchall()
    assert sorted(name for name, _ in files) == ["earth.png", "moon.jpg"], files
    for name, key in files:
        path = upload_directory / key[:2] / key[2:4] / key if rails else upload_directory / key
        assert path.read_bytes() == (REPOSITORY / "test/fixtures/files" / name).read_bytes(), name


def check_app(session, port, database, rails):
    base = f"http://127.0.0.1:{port}"
    browser(session, "open", f"{base}/session/new")
    browser(session, "fill", 'input[name="email_address"]', "benchmark@example.invalid")
    browser(session, "fill", 'input[name="password"]', "benchmark-password")
    browser(session, "click", 'button[name="log_in"]')
    browser(session, "wait", "--url", "**/rooms/1")
    browser(session, "wait", "#composer trix-editor")
    browser(session, "wait", "--load", "networkidle")
    files = [str(REPOSITORY / "test/fixtures/files/earth.png"), str(REPOSITORY / "test/fixtures/files/moon.jpg")]
    browser(session, "upload", '#composer input[type="file"]', *files)
    both = preview(session)
    assert [item["name"] for item in both] == ["earth.png", "moon.jpg"], both
    browser(session, "click", '#composer [data-composer-target="fileList"] > button:first-child')
    one = preview(session)
    assert [item["name"] for item in one] == ["moon.jpg"], one
    browser(session, "upload", '#composer input[type="file"]', files[0])
    assert [item["name"] for item in preview(session)] == ["earth.png", "moon.jpg"]
    browser(session, "eval", "window.__composerAlerts=[];window.alert=message=>window.__composerAlerts.push(String(message))")
    browser(session, "eval", """(() => {
      const open=XMLHttpRequest.prototype.open, send=XMLHttpRequest.prototype.send;
      XMLHttpRequest.prototype.open=function(method,url,...rest){this.__composerUpload=String(url).includes('/rooms/1/messages');return open.call(this,method,url,...rest)};
      XMLHttpRequest.prototype.send=function(body){if(this.__composerUpload)setTimeout(()=>send.call(this,body),1500);else send.call(this,body)};
    })()""")
    browser(session, "fill", "#composer trix-editor", "Text with two files")
    browser(session, "click", '#composer button[type="submit"]')
    browser(session, "wait", '.messages .message__pending-upload')
    pending = json.loads(browser(session, "eval", """(() => ({
      uploadPending:[...document.querySelectorAll('.messages .message__pending-upload')].some(node=>node.textContent.includes('earth.png')),
      fileListEmpty:document.querySelector('#composer [data-composer-target="fileList"]')?.children.length===0,
      failed:!!document.querySelector('.messages .message--failed')
    }))()"""))
    assert pending == {"uploadPending": True, "fileListEmpty": True, "failed": False}, pending
    rows = []
    for _ in range(150):
        rows = saved(database, rails)
        if len(rows) == 3 and sum(bool(row[3]) for row in rows) == 2:
            break
        time.sleep(0.1)
    assert len(rows) == 3 and sum(bool(row[3]) for row in rows) == 2, (rows, browser(session, "eval", "JSON.stringify({alerts:window.__composerAlerts,messages:[...document.querySelectorAll('.messages .message')].map(node=>({id:node.id,classes:node.className,text:node.textContent.slice(-100)}))})"))
    for _, client_id, _, _ in rows:
        browser(session, "wait", f'#message_{client_id}[data-message-id]')
    assert preview(session) == [], preview(session)
    result = {"both": both, "one": one, "pending": pending,
              "saved": sorted((re.sub(r"<[^>]+>", "", body), filename or "") for _, _, body, filename in rows)}
    return result


def main():
    assert shutil.which("agent-browser"), "agent-browser CLI is required"
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPOSITORY, text=True).strip() == REVISION
    sessions = [f"composer-rust-{uuid.uuid4().hex[:8]}", f"composer-camp-{uuid.uuid4().hex[:8]}"]
    with tempfile.TemporaryDirectory(prefix="paired-composer-browser-") as scratch:
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
            rust = start_server(rust_db, rust_port, {"RUSTFIRE_CAMPFIRE_SECRET_KEY_BASE": environment["SECRET_KEY_BASE"], "RUSTFIRE_UPLOAD_DIR": str(temp / "uploads")})
            try:
                with open(temp / "puma.log", "w+") as log:
                    camp = subprocess.Popen(
                        [str(RUBY), str(RUBY.parent / "bundle"), "exec", "puma", "-C", "config/puma.rb"],
                        cwd=REPOSITORY, env=environment, stdout=log, stderr=log,
                    )
                    try:
                        wait_for_server(camp_port, camp)
                        rust_result = check_app(sessions[0], rust_port, rust_db, False)
                        camp_result = check_app(sessions[1], camp_port, camp_db, True)
                        assert rust_result == camp_result, (rust_result, camp_result)
                        verify_uploads(rust_db, temp / "uploads", False)
                        verify_uploads(camp_db, REPOSITORY / "storage/files", True)
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
    print("PASS queued file previews, removal, and text-plus-two-file browser send match Campfire")


if __name__ == "__main__":
    main()
