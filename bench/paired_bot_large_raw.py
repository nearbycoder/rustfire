"""Compare a large raw-text bot message above Rustfire's former 25 MiB limit."""

import argparse
import hashlib
import http.client
import pathlib
import sqlite3
import subprocess
import tempfile
from urllib.parse import urlsplit

from direct_lookup import free_port, start_server, stop_server
from paired_banned_content import BUNDLE, REPOSITORY, REVISION, RUBY, isolated_campfire, start_redis
from paired_direct_lookup import seed_campfire, seed_rustfire, wait_for_server
from paired_mention_webhook import BOT_ID, BOT_TOKEN, seed_bot


def post(port, data):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=120)
    try:
        connection.request("POST", f"/rooms/1/{BOT_ID}-{BOT_TOKEN}/messages", data,
                           {"Accept": "application/json", "Content-Type": "text/plain"})
        response = connection.getresponse()
        body = response.read()
        location = urlsplit(response.getheader("Location", "")).path
        return response.status, response.getheader("Content-Type", ""), location, body
    finally:
        connection.close()


def saved(database, campfire):
    with sqlite3.connect(database) as db:
        if campfire:
            rows = db.execute("""SELECT m.id,t.body FROM messages m JOIN action_text_rich_texts t
                ON t.record_type='Message' AND t.record_id=m.id AND t.name='body'""").fetchall()
        else:
            rows = db.execute("SELECT id,body FROM messages").fetchall()
    assert len(rows) == 1, len(rows)
    message_id, body = rows[0]
    encoded = body.encode()
    with sqlite3.connect(database) as db:
        search = db.execute("SELECT body FROM message_search_index WHERE rowid=?", [message_id]).fetchone()
    assert search is not None, "Large bot message has no search entry"
    indexed = search[0].encode()
    return (message_id, len(encoded), hashlib.sha256(encoded).hexdigest(),
            len(indexed), hashlib.sha256(indexed).hexdigest())


def exercise(port, database, campfire, data):
    status, media, location, body = post(port, data)
    assert status == 201 and media == "application/json" and not body, (status, media, body[:200])
    message_id, size, digest, indexed_size, indexed_digest = saved(database, campfire)
    assert location == f"/messages/{message_id}", location
    return status, media, "/messages/ID", size, digest, indexed_size, indexed_digest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mib", type=int, default=25, help="payload MiB plus one byte")
    args = parser.parse_args()
    if args.mib < 1:
        parser.error("--mib must be positive")
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPOSITORY, text=True).strip() == REVISION
    data = b"x" * (args.mib * 1024 * 1024 + 1)
    expected_digest = hashlib.sha256(data).hexdigest()
    with tempfile.TemporaryDirectory(prefix="paired-bot-large-raw-") as scratch:
        temp = pathlib.Path(scratch)
        rust_db, camp_db = temp / "rust.sqlite3", temp / "camp.sqlite3"
        rust_port, camp_port, redis_port = free_port(), free_port(), free_port()
        seed_rustfire(rust_db, rust_port, [])
        checkout = isolated_campfire(temp, redis_port)
        camp_env = seed_campfire(checkout, RUBY, BUNDLE, REPOSITORY / "storage/db/production.sqlite3", camp_db, [], camp_port, temp)
        camp_env["REDIS_URL"] = f"redis://127.0.0.1:{redis_port}"
        camp_env["WEB_CONCURRENCY"] = "1"
        seed_bot(rust_db, False, "", with_webhooks=False)
        seed_bot(camp_db, True, "", with_webhooks=False)
        redis, redis_log = start_redis(temp, redis_port)
        try:
            rust = start_server(rust_db, rust_port, {"RUSTFIRE_DISABLE_WEBHOOKS": "1"})
            try:
                rust_result = exercise(rust_port, rust_db, False, data)
            finally:
                stop_server(rust)
            with open(temp / "puma.log", "w+") as log:
                camp = subprocess.Popen([str(RUBY), str(RUBY.parent / "bundle"), "exec", "puma", "-C", "config/puma.rb"],
                                        cwd=checkout, env=camp_env, stdout=log, stderr=log)
                try:
                    wait_for_server(camp_port, camp)
                    camp_result = exercise(camp_port, camp_db, True, data)
                finally:
                    stop_server(camp)
        finally:
            redis.terminate()
            redis.wait(timeout=10)
            redis_log.close()
    assert rust_result == camp_result, (rust_result, camp_result)
    assert rust_result[3:5] == (len(data), expected_digest), rust_result
    print(f"PASS paired {len(data)}-byte raw bot message, response, saved SHA-256, and search index")


if __name__ == "__main__":
    main()
