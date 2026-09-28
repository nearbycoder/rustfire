"""Compare a saved malformed Campfire ActionText message with its Rustfire import.

Run after ``cargo build --release`` with the pinned Campfire checkout and bundle.
"""

import hashlib
import http.client
import json
import os
import pathlib
import sqlite3
import subprocess
import sys
import tempfile

from direct_lookup import free_port, start_server, stop_server
from paired_banned_content import BUNDLE, REPOSITORY, REVISION, RUBY, start_redis
from paired_direct_lookup import login_campfire, seed_campfire, wait_for_server
from paired_rich_filters import post_blank


NAME = "import-invalid-sgid"
BODY = "<div>A<action-text-attachment sgid='invalid' filename='notes.txt'></action-text-attachment>Z</div>"


def get(port, cookie, path):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
    try:
        connection.request("GET", path, headers={"Cookie": cookie, "Accept": "text/html"})
        response = connection.getresponse()
        payload = response.read()
        return response.status, response.getheader("Content-Type"), payload
    finally:
        connection.close()


def saved(database):
    with sqlite3.connect(database) as db:
        row = db.execute("SELECT id,client_message_id FROM messages WHERE client_message_id=?", (f"paired-rich-filter-{NAME}",)).fetchone()
        assert row is not None
        indexed = db.execute("SELECT body FROM message_search_index WHERE rowid=?", (row[0],)).fetchone()
        return row, indexed


def main():
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPOSITORY, text=True).strip() == REVISION
    with tempfile.TemporaryDirectory(prefix="paired-import-invalid-action-text-") as scratch:
        temp = pathlib.Path(scratch)
        camp_db, rust_db, uploads = temp / "camp.sqlite3", temp / "rust.sqlite3", temp / "uploads"
        camp_port, rust_port, redis_port = free_port(), free_port(), free_port()
        env = seed_campfire(REPOSITORY, RUBY, BUNDLE, REPOSITORY / "storage/db/production.sqlite3", camp_db, [], camp_port, temp)
        env["REDIS_URL"] = f"redis://127.0.0.1:{redis_port}"
        env["WEB_CONCURRENCY"] = "1"
        redis, redis_log = start_redis(temp, redis_port)
        try:
            with (temp / "puma.log").open("w+") as log:
                process = subprocess.Popen([str(RUBY), str(RUBY.parent / "bundle"), "exec", "puma", "-C", "config/puma.rb"], cwd=REPOSITORY, env=env, stdout=log, stderr=log)
                try:
                    wait_for_server(camp_port, process)
                    cookie, csrf = login_campfire(camp_port)
                    assert post_blank(camp_port, cookie, csrf, NAME, BODY)[0] == 500
                    source_row, source_index = saved(camp_db)
                    assert source_index is None
                    source_page = get(camp_port, cookie, "/rooms/1/messages")
                    source_edit = get(camp_port, cookie, f"/rooms/1/messages/{source_row[0]}/edit")
                finally:
                    stop_server(process)
        finally:
            redis.terminate()
            redis.wait(timeout=10)
            redis_log.close()
        command = [sys.executable, "tools/import_campfire.py", "--source-db", str(camp_db),
                   "--source-files", str(REPOSITORY / "storage/files"), "--target-db", str(rust_db),
                   "--target-uploads", str(uploads), "--rustfire-bin", "target/release/rustfire"]
        importer_env = dict(os.environ, RUSTFIRE_CAMPFIRE_SECRET_KEY_BASE=env["SECRET_KEY_BASE"])
        result = subprocess.run(command, cwd=pathlib.Path(__file__).resolve().parent.parent,
                                env=importer_env, check=True, capture_output=True, text=True)
        counts = json.loads(result.stdout)
        assert counts["preserved_missing_search"] == 1 and counts["reindexed_messages"] == 0, counts
        target_row, target_index = saved(rust_db)
        assert target_row == source_row, (target_row, source_row)
        assert target_index == source_index, (target_index, source_index)
        rust_process = start_server(rust_db, rust_port, {"RUSTFIRE_UPLOAD_DIR": str(uploads), "RUSTFIRE_CAMPFIRE_SECRET_KEY_BASE": env["SECRET_KEY_BASE"]})
        try:
            target_page = get(rust_port, cookie, "/rooms/1/messages")
            target_edit = get(rust_port, cookie, f"/rooms/1/messages/{target_row[0]}/edit")
        finally:
            stop_server(rust_process)
        assert source_page[:2] == target_page[:2], (source_page[:2], target_page[:2])
        assert source_page[2].count(b"Failed to load message content") == target_page[2].count(b"Failed to load message content") == 1
        assert source_page[2].strip() == target_page[2].strip(), (source_page[2], target_page[2])
        assert source_edit[:2] == target_edit[:2], (source_edit[:2], target_edit[:2])
        assert hashlib.sha256(source_edit[2]).digest() == hashlib.sha256(target_edit[2]).digest()
        print(f"PASS imported malformed ActionText message {target_row[0]} retains missing FTS row, failed-content presentation, and edit failure")


if __name__ == "__main__":
    main()
