"""Compare Campfire and Rustfire sign-in forms, rejection, and sign-out."""

import pathlib
import re
import sqlite3
import subprocess
import tempfile
import urllib.error
import urllib.parse
import urllib.request

from direct_lookup import free_port, start_server, stop_server
from paired_banned_content import start_redis
from paired_direct_lookup import seed_campfire, seed_rustfire, wait_for_server
from paired_join import browser
from paired_room_shell import HeadMeta, section
from paired_turbo_fanout import MessageTagSequence


SOURCE = pathlib.Path("/tmp/once-campfire-reference")
RUBY = pathlib.Path("/tmp/rustfire-baseline/local/bin/ruby")
BUNDLE = pathlib.Path("/tmp/rustfire-baseline/bundle")
REVISION = "91d294f4a09f9bbe37f9548959bfcb43645678fb"


def request(opener, port, path, method="GET", fields=None, csrf=None):
    headers = {"Accept": "text/html"}
    if csrf:
        headers["X-CSRF-Token"] = csrf
    body = urllib.parse.urlencode(fields).encode() if fields is not None else None
    if body is not None:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=body, headers=headers, method=method)
    try:
        response = opener.open(req)
    except urllib.error.HTTPError as error:
        response = error
    with response:
        return response.status, urllib.parse.urlsplit(response.headers.get("Location", "")).path, response.read().decode()


def token(page):
    found = re.search(r'<meta name=[\'\"]csrf-token[\'\"] content=[\'\"]([^\'\"]+)', page)
    assert found
    return found.group(1)


def form_shape(page):
    form = re.search(r'<form class="flex flex-column gap".*?</form>', page, re.S)
    assert form, "source-shaped sign-in form missing"
    parser = MessageTagSequence()
    parser.feed(form.group())
    return parser.tags, parser.attribute_keys


def icon_links(page):
    parser = HeadMeta()
    parser.feed(page)
    assert set(parser.links) == {"icon", "apple-touch-icon"}, parser.links
    return parser.links


def session_count(database):
    with sqlite3.connect(database) as db:
        return db.execute("SELECT count(*) FROM sessions WHERE user_id=1").fetchone()[0]


def push_rows(database):
    with sqlite3.connect(database) as db:
        return db.execute("SELECT user_id,endpoint FROM push_subscriptions ORDER BY user_id,endpoint").fetchall()


def check(port, database):
    opener = browser()
    starting_sessions = session_count(database)
    status, _, page = request(opener, port, "/session/new?email_address=benchmark%40example.invalid")
    assert status == 200
    assert 'value="benchmark@example.invalid"' in page and "Benchmark" in page
    assert "name='authenticity_token'" in page or 'name="authenticity_token"' in page
    assert "user[email_address]" not in page and 'name="email_address"' in page
    original_shape = form_shape(page)
    original_icons = icon_links(page)

    status, _, rejected = request(opener, port, "/session", "POST", {"email_address": "benchmark@example.invalid", "password": "wrong-password"}, token(page))
    assert status == 401 and "shake" in rejected and "Too many requests or unauthorized." in rejected
    assert form_shape(rejected) == original_shape
    status, _, alert_asset = request(opener, port, "/assets/alert-b937985b.svg")
    assert status == 200 and alert_asset.encode() == pathlib.Path("static/assets/alert-b937985b.svg").read_bytes()

    status, location, _ = request(opener, port, "/session", "POST", {"email_address": "benchmark@example.invalid", "password": "benchmark-password"}, token(rejected))
    assert (status, location) == (302, "/"), (status, location)
    assert session_count(database) == starting_sessions + 1
    status, _, room = request(opener, port, "/rooms/1")
    assert status == 200
    status, location, _ = request(opener, port, "/session", "DELETE", {"push_subscription_endpoint": "https://fcm.googleapis.com/fcm/send/current-device"}, token(room))
    assert (status, location) == (302, "/"), (status, location)
    assert session_count(database) == starting_sessions
    assert push_rows(database) == [
        (1, "https://fcm.googleapis.com/fcm/send/other-device"),
        (2, "https://fcm.googleapis.com/fcm/send/current-device"),
    ], push_rows(database)
    assert request(opener, port, "/")[:2] == (302, "/session/new")
    assert request(opener, port, "/rooms/1")[:2] == (302, "/session/new")
    status, _, sign_in_page = request(opener, port, "/session/new")
    assert status == 200
    assert request(opener, port, "/session", "POST", {"email_address": "benchmark@example.invalid", "password": "benchmark-password"}, token(sign_in_page))[:2] == (302, "/rooms/1")
    rate_opener = browser()
    status, _, rate_page = request(rate_opener, port, "/session/new")
    assert status == 200
    for _ in range(7):
        status, _, rate_page = request(rate_opener, port, "/session", "POST", {"email_address": "benchmark@example.invalid", "password": "wrong-password"}, token(rate_page))
        assert status == 401, status
    status, _, limited = request(rate_opener, port, "/session", "POST", {"email_address": "benchmark@example.invalid", "password": "wrong-password"}, token(rate_page))
    assert status == 429 and "Too many requests or unauthorized." in limited, (status, limited[:250])
    assert section(limited.encode(), "main-content") == section(rejected.encode(), "main-content")
    return original_shape, original_icons, page, rejected, limited


def main():
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=SOURCE, text=True).strip() == REVISION
    with tempfile.TemporaryDirectory(prefix="paired-session-") as scratch:
        temp = pathlib.Path(scratch)
        rust_db, camp_db = temp / "rust.sqlite3", temp / "camp.sqlite3"
        rust_port, camp_port, redis_port = free_port(), free_port(), free_port()
        seed_rustfire(rust_db, rust_port, [])
        camp_env = seed_campfire(SOURCE, RUBY, BUNDLE, SOURCE / "storage/db/production.sqlite3", camp_db, [], camp_port, temp)
        camp_env["REDIS_URL"] = f"redis://127.0.0.1:{redis_port}"
        camp_env["WEB_CONCURRENCY"] = "1"
        with sqlite3.connect(camp_db) as db:
            db.execute("UPDATE accounts SET name='Benchmark' WHERE id=1")
            account_updated_at = db.execute("SELECT updated_at FROM accounts WHERE id=1").fetchone()[0]
            digest = db.execute("SELECT password_digest FROM users WHERE id=1").fetchone()[0]
        with sqlite3.connect(rust_db) as db:
            db.execute("UPDATE accounts SET name='Benchmark',updated_at=? WHERE id=1", [account_updated_at])
            db.execute("UPDATE users SET name='Test Admin',email_address='benchmark@example.invalid',password_digest=? WHERE id=1", [digest])
        for database in (rust_db, camp_db):
            with sqlite3.connect(database) as db:
                db.executemany("INSERT INTO push_subscriptions(user_id,endpoint,p256dh_key,auth_key,created_at,updated_at) VALUES(?1,?2,'key','auth','2026-01-01 00:00:00','2026-01-01 00:00:00')", [
                    (1, "https://fcm.googleapis.com/fcm/send/current-device"),
                    (1, "https://fcm.googleapis.com/fcm/send/other-device"),
                    (2, "https://fcm.googleapis.com/fcm/send/current-device"),
                ])
        redis, redis_log = start_redis(temp, redis_port)
        try:
            rust = start_server(rust_db, rust_port, {"RUSTFIRE_CAMPFIRE_SECRET_KEY_BASE": camp_env["SECRET_KEY_BASE"]})
            try:
                rust_shape = check(rust_port, rust_db)
            finally:
                stop_server(rust)
            with open(temp / "puma.log", "w+") as log:
                camp = subprocess.Popen([str(RUBY), str(RUBY.parent / "bundle"), "exec", "puma", "-C", "config/puma.rb"], cwd=SOURCE, env=camp_env, stdout=log, stderr=log)
                try:
                    wait_for_server(camp_port, camp)
                    camp_shape = check(camp_port, camp_db)
                except Exception:
                    log.flush()
                    log.seek(0)
                    print(log.read()[-3000:])
                    raise
                finally:
                    stop_server(camp)
            for rust_part, camp_part in zip(rust_shape[0], camp_shape[0]):
                mismatches = [(i, left, right) for i, (left, right) in enumerate(zip(rust_part, camp_part)) if left != right]
                assert not mismatches and len(rust_part) == len(camp_part), (len(rust_part), len(camp_part), mismatches[:12])
            assert rust_shape[1] == camp_shape[1], (rust_shape[1], camp_shape[1])
            for target in ("nav", "main-content", "footer", "sidebar"):
                rust_tokens = section(rust_shape[2].encode(), target)
                camp_tokens = section(camp_shape[2].encode(), target)
                differences = [(index, left, right) for index, (left, right) in enumerate(zip(rust_tokens, camp_tokens)) if left != right]
                assert not differences and len(rust_tokens) == len(camp_tokens), (target, len(rust_tokens), len(camp_tokens), differences[:8])
                rust_rejected = section(rust_shape[3].encode(), target)
                camp_rejected = section(camp_shape[3].encode(), target)
                differences = [(index, left, right) for index, (left, right) in enumerate(zip(rust_rejected, camp_rejected)) if left != right]
                assert not differences and len(rust_rejected) == len(camp_rejected), ("rejected", target, len(rust_rejected), len(camp_rejected), differences[:8])
                assert section(rust_shape[4].encode(), target) == section(camp_shape[4].encode(), target), ("rate-limited", target)
            for page in (rust_shape[3], camp_shape[3]):
                assert page.index('class="flash"') < page.index('id="main-content"'), "rejection flash must precede main"
            rust_flash = section(rust_shape[3].replace('<div class="flash"', '<div id="session-flash" class="flash"', 1).encode(), "session-flash")
            # The pinned layout emits a stray </span> after the flash icon. Browsers discard it.
            camp_rejected = re.sub(r'(<div class="flash__inner shadow"[^>]*>\s*<img[^>]*>)\s*</span>', r'\1', camp_shape[3], count=1)
            assert camp_rejected != camp_shape[3]
            camp_flash = section(camp_rejected.replace('<div class="flash"', '<div id="session-flash" class="flash"', 1).encode(), "session-flash")
            assert rust_flash == camp_flash, (rust_flash, camp_flash)
        finally:
            redis.terminate()
            redis.wait(timeout=10)
            redis_log.close()
    print("PASS paired sign-in page sections, rejection and rate-limit flashes, device push removal on sign-out, and return to a requested room")


if __name__ == "__main__":
    main()
