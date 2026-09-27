"""Compare first-run setup with a fresh pinned Campfire account."""

import pathlib
import re
import shutil
import sqlite3
import subprocess
import tempfile
import urllib.error
import urllib.parse
import urllib.request

from direct_lookup import free_port, start_server, stop_server
from paired_banned_content import start_redis
from paired_direct_lookup import campfire_env, wait_for_server
from paired_join import browser
from paired_turbo_fanout import MessageTagSequence


SOURCE = pathlib.Path("/tmp/once-campfire-reference")
RUBY = pathlib.Path("/tmp/rustfire-baseline/local/bin/ruby")
BUNDLE = pathlib.Path("/tmp/rustfire-baseline/bundle")
REVISION = "91d294f4a09f9bbe37f9548959bfcb43645678fb"


def fresh_campfire_database(path):
    with sqlite3.connect(SOURCE / "storage/db/production.sqlite3") as source, sqlite3.connect(path) as target:
        source.backup(target)
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA foreign_keys=OFF")
        for table in ("action_text_rich_texts", "message_search_index", "messages", "sessions", "memberships", "rooms", "users", "accounts"):
            db.execute(f"DELETE FROM {table}")
        db.commit()
        assert not db.execute("PRAGMA foreign_key_check").fetchall()


def multipart(fields, image):
    boundary = "paired-first-run"
    parts = []
    for name, value in fields.items():
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
    parts.extend((f'--{boundary}\r\nContent-Disposition: form-data; name="user[avatar]"; filename="avatar.png"\r\nContent-Type: image/png\r\n\r\n'.encode(), image, b'\r\n', f'--{boundary}--\r\n'.encode()))
    return f"multipart/form-data; boundary={boundary}", b"".join(parts)


def fetch(opener, port, path, body=None, content_type=None):
    headers = {"Accept": "text/html"}
    if content_type:
        headers["Content-Type"] = content_type
    request = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=body, headers=headers)
    try:
        response = opener.open(request)
    except urllib.error.HTTPError as error:
        response = error
    with response:
        return response.status, urllib.parse.urlsplit(response.headers.get("Location", "")).path, response.read().decode()


def form_structure(page):
    form = re.search(r'<form class="center max-width".*?</form>', page, re.S)
    assert form, "first-run signup form missing"
    parser = MessageTagSequence()
    parser.feed(form.group())
    return parser.tags, parser.attribute_keys


def page_shell(page):
    styles = re.findall(r'<link[^>]+rel=[\'\"]stylesheet[\'\"][^>]+href=[\'\"]([^\'\"]+)', page)
    body = re.search(r'<body[^>]+class=[\'\"]([^\'\"]*)', page)
    landmarks = tuple(bool(re.search(rf'<[^>]+id=[\'\"]{name}[\'\"]', page)) for name in ("nav", "main-content", "footer", "sidebar", "app-logo"))
    return styles, body.group(1) if body else None, landmarks


def check(port, database, campfire, image):
    opener = browser()
    assert fetch(opener, port, "/")[:2] == (302, "/session/new")
    assert fetch(opener, port, "/session/new")[:2] == (302, "/first_run")
    status, _, page = fetch(opener, port, "/first_run")
    assert status == 200
    assert 'enctype="multipart/form-data"' in page
    for field in ("user[name]", "user[email_address]", "user[password]", "user[avatar]"):
        assert f'name="{field}"' in page, field
    token = re.search(r'name=[\'\"]authenticity_token[\'\"] value=[\'\"]([^\'\"]+)', page)
    assert token and token.group(1)
    fields = {"authenticity_token": token.group(1), "user[name]": "First Admin", "user[email_address]": "first-admin@example.invalid", "user[password]": "initial-password"}
    content_type, body = multipart(fields, image)
    status, location, _ = fetch(opener, port, "/first_run", body, content_type)
    assert (status, location) == (302, "/"), (status, location)
    with sqlite3.connect(database) as db:
        account = db.execute("SELECT name FROM accounts").fetchall()
        join_code = db.execute("SELECT join_code FROM accounts").fetchone()[0]
        users = db.execute("SELECT id,name,email_address,role FROM users").fetchall()
        rooms = db.execute("SELECT id,name,type,creator_id FROM rooms").fetchall()
        memberships = db.execute("SELECT room_id,user_id FROM memberships").fetchall()
        avatar_count = db.execute("SELECT count(*) FROM active_storage_attachments WHERE record_type='User' AND name='avatar'").fetchone()[0] if campfire else db.execute("SELECT count(*) FROM avatars").fetchone()[0]
    assert account == [("Campfire",)], account
    assert re.fullmatch(r"[A-Za-z0-9]{4}(?:-[A-Za-z0-9]{4}){2}", join_code), join_code
    assert len(users) == len(rooms) == len(memberships) == avatar_count == 1
    uid, name, email, role = users[0]
    rid, room_name, kind, creator = rooms[0]
    assert (name, email, role) == ("First Admin", "first-admin@example.invalid", 1)
    assert (room_name, kind, creator, memberships) == ("All Talk", "Rooms::Open", uid, [(rid, uid)])
    assert fetch(opener, port, "/")[:2] == (302, f"/rooms/{rid}")
    status, _, room_page = fetch(opener, port, f"/rooms/{rid}")
    assert status == 200 and "All Talk" in room_page
    assert fetch(opener, port, "/first_run")[:2] == (302, "/")
    assert fetch(browser(), port, "/")[:2] == (302, "/session/new")
    return (account, [(name, email, role)], [(room_name, kind)], avatar_count), form_structure(page), page_shell(page)


def main():
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=SOURCE, text=True).strip() == REVISION
    with tempfile.TemporaryDirectory(prefix="paired-first-run-") as scratch:
        temp = pathlib.Path(scratch)
        source_root = temp / "source"
        shutil.copytree(SOURCE, source_root, ignore=shutil.ignore_patterns(".git", "storage", "tmp", "log"))
        (source_root / "storage/files").mkdir(parents=True)
        (source_root / "tmp").mkdir()
        rust_db, source_db = temp / "rust.sqlite3", temp / "camp.sqlite3"
        rust_port, source_port, redis_port = free_port(), free_port(), free_port()
        fresh_campfire_database(source_db)
        source_env = campfire_env(source_root, RUBY, BUNDLE, source_db, source_port, temp)
        source_env["REDIS_URL"] = f"redis://127.0.0.1:{redis_port}"
        source_env["WEB_CONCURRENCY"] = "1"
        image = pathlib.Path("static/icons/app-icon-192.png").read_bytes()
        redis, redis_log = start_redis(temp, redis_port)
        try:
            rust = start_server(rust_db, rust_port, {"RUSTFIRE_CAMPFIRE_SECRET_KEY_BASE": source_env["SECRET_KEY_BASE"], "RUSTFIRE_UPLOAD_DIR": str(temp / "uploads")})
            try:
                rust_result = check(rust_port, rust_db, False, image)
            finally:
                stop_server(rust)
            with open(temp / "puma.log", "w+") as log:
                source = subprocess.Popen([str(RUBY), str(RUBY.parent / "bundle"), "exec", "puma", "-C", "config/puma.rb"], cwd=source_root, env=source_env, stdout=log, stderr=log)
                try:
                    wait_for_server(source_port, source)
                    source_result = check(source_port, source_db, True, image)
                except Exception:
                    log.flush()
                    log.seek(0)
                    print(log.read()[-3000:])
                    raise
                finally:
                    stop_server(source)
            assert rust_result[0] == source_result[0], (rust_result[0], source_result[0])
            for rust_part, source_part in zip(rust_result[1], source_result[1]):
                mismatches = [(index, rust_item, source_item) for index, (rust_item, source_item) in enumerate(zip(rust_part, source_part)) if rust_item != source_item]
                assert not mismatches and len(rust_part) == len(source_part), (len(rust_part), len(source_part), mismatches[:12])
            assert rust_result[2] == source_result[2], (rust_result[2], source_result[2])
        finally:
            redis.terminate()
            redis.wait(timeout=10)
            redis_log.close()
    print("PASS paired first-run account, administrator, room, avatar, and repeat redirect")


if __name__ == "__main__":
    main()
