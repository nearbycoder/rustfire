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


def seed_graph(database):
    rooms = ((2, "Private Plans", "Rooms::Closed", 1, STAMP, STAMP),
        (3, None, "Rooms::Direct", 1, STAMP, STAMP),
        (4, "Hidden Archive", "Rooms::Closed", 2, STAMP, STAMP))
    memberships = ((2, 1, "mentions", STAMP, STAMP, STAMP),
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


def capture(port, cookie, csrf):
    pages = {}
    for name, path in CASES.items():
        headers = {"User-Agent": AGENT}
        if name == "sidebar":
            headers["Turbo-Frame"] = "user_sidebar"
        status, _, body = request(port, "GET", path, cookie, csrf, extra_headers=headers)
        assert status == 200, (name, path, status, body[:300])
        pages[name] = body
    status, location, _ = request(port, "GET", "/rooms/4", cookie, csrf, extra_headers={"User-Agent": AGENT})
    assert status == 302, (status, location)
    return pages, location


def compare(source, target):
    search_ids = re.findall(rb'data-message-id=["\'](\d+)', source["search"])
    assert search_ids == [b"1", b"2", b"3"], search_ids
    assert re.findall(rb'/searches\?q=(constellation|private)', source["search"])[:2] == [b"constellation", b"private"]
    assert "🎉".encode() in source["private"]
    for name in CASES:
        if name == "sidebar":
            options = dict(normalize_times=True, normalize_avatar_paths=True,
                normalize_blob_paths=True, normalize_text_origins=True, ignore_csrf_inputs=True)
            assert_equal("imported sidebar frame", section(source[name], "user_sidebar", **options),
                section(target[name], "user_sidebar", **options))
            continue
        for part in ("head", "body"):
            options = dict(normalize_times=True, normalize_avatar_paths=True,
                normalize_blob_paths=True, normalize_text_origins=True, ignore_csrf_inputs=True)
            assert_equal(f"imported {name} {part}", section(source[name], part, **options),
                section(target[name], part, **options))


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
                    source, source_redirect = capture(camp_port, cookie, csrf)
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
                target, target_redirect = capture(rust_port, cookie, csrf)
            finally:
                stop_server(rust)
            assert source_redirect == f"http://127.0.0.1:{camp_port}/", source_redirect
            assert target_redirect == f"http://127.0.0.1:{rust_port}/", target_redirect
            if args.sample_dir:
                args.sample_dir.mkdir(parents=True, exist_ok=True)
                for label, documents in (("campfire", source), ("rustfire", target)):
                    for name, body in documents.items():
                        (args.sample_dir / f"{label}-{name}.html").write_bytes(body)
            compare(source, target)
            print("PASS imported open/private/direct room pages, boost, sidebar, search, and inaccessible room redirect")
        finally:
            redis.terminate()
            redis.wait(timeout=10)
            redis_log.close()


if __name__ == "__main__":
    main()
