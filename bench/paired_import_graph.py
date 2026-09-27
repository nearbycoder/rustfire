"""Compare an imported Campfire room graph, boosts, sidebar, and search page.

Requires the pinned Campfire checkout, bundled Ruby, Redis, and a release build.
All database changes happen in a disposable copy of the Campfire installation.
"""

import argparse
import base64
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import tempfile
import urllib.parse

from direct_lookup import free_port, start_server, stop_server
from paired_banned_content import start_redis
from paired_bot_admin import request
from paired_direct_lookup import login_campfire, seed_campfire, wait_for_server
from paired_room_shell import AGENT, assert_equal, section


SOURCE_REVISION = "91d294f4a09f9bbe37f9548959bfcb43645678fb"
STAMP = "2026-01-01 12:00:00.000000"
CASES = {
    "open": "/rooms/1",
    "private": "/rooms/2",
    "direct": "/rooms/3",
    "private_around": "/rooms/2/@2",
    "sidebar": "/users/me/sidebar",
    "search": "/searches?q=constellation",
}
MEMBER_CASES = {**CASES, "hidden": "/rooms/4"}
AFTER_POST_CASES = {name: CASES[name] for name in ("private", "sidebar", "search")}
AFTER_POST_CLIENT_ID = "import-graph-after-post"


def seed_graph(database):
    rooms = ((2, "Private Plans", "Rooms::Closed", 1, STAMP, STAMP),
        (3, None, "Rooms::Direct", 1, STAMP, STAMP),
        (4, "Hidden Archive", "Rooms::Closed", 2, STAMP, STAMP))
    memberships = ((2, 1, "everything", None, STAMP, STAMP),
        (2, 2, "everything", None, STAMP, STAMP),
        (3, 1, "everything", STAMP, STAMP, STAMP),
        (3, 2, "everything", None, STAMP, STAMP),
        (4, 2, "mentions", None, STAMP, STAMP))
    messages = ((1, 1, 1, "import-graph-1", "2026-01-01 12:01:01.000001"),
        (2, 2, 2, "import-graph-2", "2026-01-01 12:01:02.000002"),
        (3, 3, 1, "import-graph-3", "2026-01-01 12:01:03.000003"),
        (4, 4, 2, "import-graph-4", "2026-01-01 12:01:04.000004"),
        (5, 2, 1, "import-graph-5", "2026-01-01 12:01:05.000005"))
    bodies = ((1, "<div>constellation open</div>", "constellation open"),
        (2, "<div>constellation private <strong>bold</strong></div>", "constellation private bold"),
        (3, "<div>constellation direct</div>", "constellation direct"),
        (4, "<div>constellation hidden</div>", "constellation hidden"),
        (5, "<div>Private follow-up <em>italic</em></div>", "Private follow-up italic"))
    with sqlite3.connect(database) as db:
        db.execute("UPDATE users SET email_address='member@example.invalid',password_digest=(SELECT password_digest FROM users WHERE id=1) WHERE id=2")
        db.executemany("INSERT INTO rooms(id,name,type,creator_id,created_at,updated_at) VALUES(?,?,?,?,?,?)", rooms)
        db.executemany("INSERT INTO memberships(room_id,user_id,involvement,unread_at,created_at,updated_at) VALUES(?,?,?,?,?,?)", memberships)
        db.executemany("INSERT INTO messages(id,room_id,creator_id,client_message_id,created_at,updated_at) VALUES(?,?,?,?,?,?)",
            ((id, room, creator, client, stamp, stamp) for id, room, creator, client, stamp in messages))
        db.executemany("INSERT INTO action_text_rich_texts(name,body,record_type,record_id,created_at,updated_at) VALUES('body',?,'Message',?,?,?)",
            ((body, id, STAMP, STAMP) for id, body, _ in bodies))
        db.executemany("INSERT INTO message_search_index(rowid,body) VALUES(?,?)",
            ((id, plain) for id, _, plain in bodies))
        db.execute("INSERT INTO boosts(id,message_id,booster_id,content,created_at,updated_at) VALUES(1,2,1,'🎉',?,?)", (STAMP, STAMP))
        db.executemany("INSERT INTO searches(id,user_id,query,created_at,updated_at) VALUES(?,1,?,?,?)",
            ((id, query, STAMP, STAMP) for id, query in ((1, "constellation"), (2, "private"))))
        db.execute("INSERT INTO push_subscriptions(id,user_id,endpoint,p256dh_key,auth_key,user_agent,created_at,updated_at) VALUES(1,1,'https://push.example.test/import-graph','test-p256dh','test-auth','test',?,?)", (STAMP, STAMP))


def capture(port, cookie, csrf, cases, inaccessible_room=None):
    pages = {}
    for name, path in cases.items():
        headers = {"User-Agent": AGENT}
        if name == "sidebar":
            headers["Turbo-Frame"] = "user_sidebar"
        status, _, body = request(port, "GET", path, cookie, csrf, extra_headers=headers)
        assert status == 200, (name, path, status, body[:300])
        pages[name] = body
    location = None
    if inaccessible_room is not None:
        status, location, _ = request(port, "GET", f"/rooms/{inaccessible_room}", cookie, csrf, extra_headers={"User-Agent": AGENT})
        assert status == 302, (status, location)
    return pages, location


def compare(source, target, label, expected_search_ids):
    search_ids = re.findall(rb'data-message-id=["\'](\d+)', source["search"])
    assert search_ids == expected_search_ids, search_ids
    if label == "administrator":
        assert re.findall(rb'/searches\?q=(constellation|private)', source["search"])[:2] == [b"constellation", b"private"]
    assert "🎉".encode() in source["private"]
    for name in source:
        if name == "sidebar":
            options = dict(normalize_times=True, normalize_avatar_paths=True,
                normalize_blob_paths=True, normalize_text_origins=True, ignore_csrf_inputs=True)
            assert_equal(f"imported {label} sidebar frame", section(source[name], "user_sidebar", **options),
                section(target[name], "user_sidebar", **options))
            continue
        for part in ("head", "body"):
            options = dict(normalize_times=True, normalize_avatar_paths=True,
                normalize_blob_paths=True, normalize_text_origins=True, ignore_csrf_inputs=True)
            assert_equal(f"imported {label} {name} {part}", section(source[name], part, **options),
                section(target[name], part, **options))


def post_after_import(port, cookie, csrf):
    body = urllib.parse.urlencode({
        "message[body]": "constellation after import",
        "message[client_message_id]": AFTER_POST_CLIENT_ID,
        "authenticity_token": csrf,
    }).encode()
    status, location, response = request(port, "POST", "/rooms/2/messages", cookie, csrf,
        body=body, content_type="application/x-www-form-urlencoded",
        extra_headers={"Accept": "text/vnd.turbo-stream.html, text/html"})
    assert status == 200 and location is None and b"constellation after import" in response, (status, location, response[:300])
    return response


def post_state(database):
    with sqlite3.connect(database) as db:
        message = db.execute("SELECT id,room_id,creator_id,client_message_id FROM messages WHERE client_message_id=?",
            (AFTER_POST_CLIENT_ID,)).fetchone()
        assert message is not None and message[0] > 5 and message[1:] == (2, 2, AFTER_POST_CLIENT_ID), message
        memberships = db.execute("SELECT user_id,unread_at IS NOT NULL FROM memberships WHERE room_id=2 ORDER BY user_id").fetchall()
        assert memberships == [(1, 1), (2, 0)], memberships
        search_text = db.execute("SELECT body FROM message_search_index WHERE rowid=?", (message[0],)).fetchone()
        assert search_text == ("constellation after import",), search_text
        return message, memberships, search_text


def csrf_from_page(page):
    match = re.search(rb'<meta name=["\']csrf-token["\'] content=["\']([^"\']+)', page)
    assert match, "Rendered CSRF token missing"
    return match.group(1).decode()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--campfire-repo", type=Path, default=Path("/tmp/once-campfire-reference"))
    parser.add_argument("--ruby", type=Path, default=Path("/tmp/rustfire-baseline/local/bin/ruby"))
    parser.add_argument("--bundle-path", type=Path, default=Path("/tmp/rustfire-baseline/bundle"))
    parser.add_argument("--sample-dir", type=Path)
    args = parser.parse_args()
    repository, ruby, bundle_path = args.campfire_repo.resolve(), args.ruby.resolve(), args.bundle_path.resolve()
    revision = subprocess.check_output(("git", "rev-parse", "HEAD"), cwd=repository, text=True).strip()
    assert revision == SOURCE_REVISION, revision
    with tempfile.TemporaryDirectory(prefix="paired-import-graph-") as directory:
        temp = Path(directory)
        source_db, target_db, uploads = temp / "campfire.sqlite3", temp / "rustfire.sqlite3", temp / "uploads"
        camp_port, rust_port, redis_port = free_port(), free_port(), free_port()
        environment = seed_campfire(repository, ruby, bundle_path,
            repository / "storage/db/production.sqlite3", source_db, [], camp_port, temp)
        seed_graph(source_db)
        generator_x = bytes.fromhex("6b17d1f2e12c4247f8bce6e563a440f277037d812deb33a0f4a13945d898c296")
        generator_y = bytes.fromhex("4fe342e2fe1a7f9b8ee7eb4a7c0f9e162bce33576b315ececbb6406837bf51f5")
        vapid_public = base64.urlsafe_b64encode(b"\x04" + generator_x + generator_y).rstrip(b"=").decode()
        vapid_private = base64.urlsafe_b64encode(bytes(31) + b"\x01").rstrip(b"=").decode()
        environment["VAPID_PUBLIC_KEY"], environment["VAPID_PRIVATE_KEY"] = vapid_public, vapid_private
        environment["REDIS_URL"] = f"redis://127.0.0.1:{redis_port}"
        redis, redis_log = start_redis(temp, redis_port)
        try:
            with (temp / "puma.log").open("w+") as log:
                camp = subprocess.Popen((str(ruby), str(ruby.parent / "bundle"), "exec", "puma", "-C", "config/puma.rb"),
                    cwd=repository, env=environment, stdout=log, stderr=log)
                try:
                    wait_for_server(camp_port, camp)
                    cookie, csrf = login_campfire(camp_port)
                    member_cookie, member_csrf = login_campfire(camp_port, "member@example.invalid")
                    source, source_redirect = capture(camp_port, cookie, csrf, CASES, inaccessible_room=4)
                    member_source, _ = capture(camp_port, member_cookie, member_csrf, MEMBER_CASES)
                except Exception:
                    log.flush()
                    log.seek(0)
                    print(log.read()[-3000:])
                    raise
                finally:
                    stop_server(camp)
            result = subprocess.run(("python", "tools/import_campfire.py", "--source-db", str(source_db),
                "--source-files", str(repository / "storage/files"), "--target-db", str(target_db),
                "--target-uploads", str(uploads), "--rustfire-bin", "target/release/rustfire"),
                env=dict(os.environ, RUSTFIRE_CAMPFIRE_SECRET_KEY_BASE=environment["SECRET_KEY_BASE"],
                    RUSTFIRE_CAMPFIRE_VAPID_PUBLIC_KEY=vapid_public, RUSTFIRE_CAMPFIRE_VAPID_PRIVATE_KEY=vapid_private),
                text=True, capture_output=True, check=True)
            counts = json.loads(result.stdout)
            assert all(counts[name] == expected for name, expected in (("rooms", 4), ("messages", 5),
                ("memberships", 7), ("boosts", 1), ("searches", 2), ("push_subscriptions", 1))), counts
            with sqlite3.connect(source_db) as source_rows, sqlite3.connect(target_db) as target_rows:
                for query in (
                    "SELECT id,name,type,creator_id,created_at,updated_at FROM rooms ORDER BY id",
                    "SELECT room_id,user_id,involvement,unread_at,created_at FROM memberships ORDER BY room_id,user_id",
                    "SELECT id,room_id,creator_id,client_message_id,created_at,updated_at FROM messages ORDER BY id",
                    "SELECT id,message_id,booster_id,content,created_at FROM boosts ORDER BY id",
                    "SELECT id,user_id,query,created_at,updated_at FROM searches ORDER BY id",
                ):
                    assert source_rows.execute(query).fetchall() == target_rows.execute(query).fetchall(), query
            rust = start_server(target_db, rust_port, {"RUSTFIRE_UPLOAD_DIR": str(uploads),
                "RUSTFIRE_CAMPFIRE_SECRET_KEY_BASE": environment["SECRET_KEY_BASE"]})
            try:
                target, target_redirect = capture(rust_port, cookie, csrf, CASES, inaccessible_room=4)
                member_target, _ = capture(rust_port, member_cookie, member_csrf, MEMBER_CASES)
                with (temp / "puma-after-import.log").open("w+") as log:
                    camp = subprocess.Popen((str(ruby), str(ruby.parent / "bundle"), "exec", "puma", "-C", "config/puma.rb"),
                        cwd=repository, env=environment, stdout=log, stderr=log)
                    try:
                        wait_for_server(camp_port, camp)
                        source_post_stream = post_after_import(camp_port, member_cookie, member_csrf)
                        target_post_stream = post_after_import(rust_port, member_cookie, csrf_from_page(member_target["private"]))
                        source_post, _ = capture(camp_port, cookie, csrf, AFTER_POST_CASES)
                        target_post, _ = capture(rust_port, cookie, csrf, AFTER_POST_CASES)
                        member_source_post, _ = capture(camp_port, member_cookie, member_csrf, AFTER_POST_CASES)
                        member_target_post, _ = capture(rust_port, member_cookie, member_csrf, AFTER_POST_CASES)
                    except Exception:
                        log.flush()
                        log.seek(0)
                        print(log.read()[-3000:])
                        raise
                    finally:
                        stop_server(camp)
            finally:
                stop_server(rust)
            assert source_redirect == f"http://127.0.0.1:{camp_port}/", source_redirect
            assert target_redirect == f"http://127.0.0.1:{rust_port}/", target_redirect
            if args.sample_dir:
                args.sample_dir.mkdir(parents=True, exist_ok=True)
                (args.sample_dir / "campfire-after-post.turbo-stream.html").write_bytes(source_post_stream)
                (args.sample_dir / "rustfire-after-post.turbo-stream.html").write_bytes(target_post_stream)
                for label, documents in (("campfire", source), ("rustfire", target),
                    ("campfire-member", member_source), ("rustfire-member", member_target),
                    ("campfire-after-post", source_post), ("rustfire-after-post", target_post),
                    ("campfire-member-after-post", member_source_post),
                    ("rustfire-member-after-post", member_target_post)):
                    for name, body in documents.items():
                        (args.sample_dir / f"{label}-{name}.html").write_bytes(body)
            compare(source, target, "administrator", [b"1", b"2", b"3"])
            compare(member_source, member_target, "member", [b"1", b"2", b"3", b"4"])
            source_state = post_state(source_db)
            assert source_state == post_state(target_db)
            options = dict(normalize_times=True, normalize_avatar_paths=True,
                normalize_blob_paths=True, normalize_text_origins=True, ignore_csrf_inputs=True)
            assert_equal("post-import Turbo response", section(b"<body>" + source_post_stream + b"</body>", "body", **options),
                section(b"<body>" + target_post_stream + b"</body>", "body", **options))
            posted_id = str(source_state[0][0]).encode()
            compare(source_post, target_post, "administrator after post", [b"1", b"2", b"3", posted_id])
            compare(member_source_post, member_target_post, "member after post", [b"1", b"2", b"3", b"4", posted_id])
            print("PASS imported administrator and member pages, search, access, and post-import message write")
        finally:
            redis.terminate()
            redis.wait(timeout=10)
            redis_log.close()


if __name__ == "__main__":
    main()
