"""Compare a browser-style message POST and its Turbo response on disposable fixtures."""

import argparse
import http.client
import pathlib
import sqlite3
import subprocess
import tempfile
import urllib.parse

from direct_lookup import free_port, start_server, stop_server
from paired_banned_content import BUNDLE, REPOSITORY, REVISION, RUBY, start_redis
from paired_direct_lookup import login_campfire, seed_campfire, seed_rustfire, wait_for_server
from paired_turbo_fanout import check_message_times, stable_message_attributes, stream_structure


CLIENT_ID = "paired-create-1"
BODY = "paired create sample"


def post_message(port, cookie, csrf):
    fields = urllib.parse.urlencode({
        "message[body]": BODY,
        "message[client_message_id]": CLIENT_ID,
        "authenticity_token": csrf,
    })
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
    try:
        connection.request("POST", "/rooms/1/messages", fields, {
            "Cookie": cookie,
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "text/vnd.turbo-stream.html, text/html",
        })
        response = connection.getresponse()
        body = response.read()
        assert response.status == 200, (response.status, body[:300])
        assert response.getheader("Content-Type", "").split(";")[0] == "text/vnd.turbo-stream.html"
        assert BODY.encode() in body
        return body
    finally:
        connection.close()


def saved_message(database, rails):
    with sqlite3.connect(database) as db:
        rows = db.execute("SELECT id,room_id,creator_id,client_message_id FROM messages").fetchall()
        assert rows == [(1, 1, 1, CLIENT_ID)], rows
        if rails:
            text = db.execute("SELECT body FROM action_text_rich_texts WHERE record_type='Message' AND record_id=1").fetchone()[0]
        else:
            text = db.execute("SELECT body FROM messages WHERE id=1").fetchone()[0]
    assert BODY in text, text


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sample-dir", type=pathlib.Path, help="save both raw Turbo POST responses")
    args = parser.parse_args()
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPOSITORY, text=True).strip() == REVISION
    with tempfile.TemporaryDirectory(prefix="paired-message-create-") as scratch:
        temp = pathlib.Path(scratch)
        rust_db, camp_db = temp / "rust.sqlite3", temp / "camp.sqlite3"
        rust_port, camp_port, redis_port = free_port(), free_port(), free_port()
        seed_rustfire(rust_db, rust_port, [])
        camp_env = seed_campfire(REPOSITORY, RUBY, BUNDLE, REPOSITORY / "storage/db/production.sqlite3", camp_db, [], camp_port, temp)
        camp_env["REDIS_URL"] = f"redis://127.0.0.1:{redis_port}"
        with sqlite3.connect(camp_db) as camp, sqlite3.connect(rust_db) as rust:
            camp.execute("DELETE FROM sqlite_sequence WHERE name='messages'")
            rust.executemany("UPDATE users SET name=?2,updated_at=?3 WHERE id=?1", camp.execute("SELECT id,name,updated_at FROM users WHERE id IN(1,2)").fetchall())
            rust.execute("UPDATE rooms SET name=? WHERE id=1", [camp.execute("SELECT name FROM rooms WHERE id=1").fetchone()[0]])
        redis, redis_log = start_redis(temp, redis_port)
        try:
            rust = start_server(rust_db, rust_port, {
                "RUSTFIRE_CAMPFIRE_SECRET_KEY_BASE": camp_env["SECRET_KEY_BASE"],
                "RUSTFIRE_PUBLIC_URL": "http://127.0.0.1",
            })
            try:
                rust_body = post_message(rust_port, "session_token=benchmark-session", "benchmark-csrf")
            finally:
                stop_server(rust)
            with open(temp / "puma.log", "w+") as log:
                camp = subprocess.Popen([str(RUBY), str(RUBY.parent / "bundle"), "exec", "puma", "-C", "config/puma.rb"], cwd=REPOSITORY, env=camp_env, stdout=log, stderr=log)
                try:
                    wait_for_server(camp_port, camp)
                    cookie, csrf = login_campfire(camp_port)
                    camp_body = post_message(camp_port, cookie, csrf)
                finally:
                    stop_server(camp)
        finally:
            redis.terminate()
            redis.wait(timeout=10)
            redis_log.close()
        saved_message(rust_db, False)
        saved_message(camp_db, True)
        output_dir = args.sample_dir or temp
        output_dir.mkdir(parents=True, exist_ok=True)
        rust_sample, camp_sample = output_dir / "rustfire-create.html", output_dir / "campfire-create.html"
        rust_sample.write_bytes(rust_body)
        camp_sample.write_bytes(camp_body)
        rust_tags, rust_keys, rust_attrs, rust_text = stream_structure(rust_sample)
        camp_tags, camp_keys, camp_attrs, camp_text = stream_structure(camp_sample)
        assert rust_tags == camp_tags and rust_keys == camp_keys
        check_message_times(rust_attrs, rust_sample)
        check_message_times(camp_attrs, camp_sample)
        assert stable_message_attributes(rust_attrs) == stable_message_attributes(camp_attrs)
        assert rust_text == camp_text
        print(f"PASS paired message POST status, Turbo type, saved row, and parsed response; bytes rustfire={len(rust_body)} campfire={len(camp_body)}")


if __name__ == "__main__":
    main()
