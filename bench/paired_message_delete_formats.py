"""Compare message deletion formats and deletion side effects with pinned Campfire."""

import http.client
import pathlib
import sqlite3
import subprocess
import tempfile

from direct_lookup import free_port, start_server, stop_server
from paired_banned_content import BUNDLE, REPOSITORY, REVISION, RUBY, isolated_campfire, start_redis
from paired_direct_lookup import login_campfire, seed_campfire, seed_rustfire, wait_for_server


CASES = (
    ("text/html", "", 406, "text/html"),
    ("text/vnd.turbo-stream.html", "", 200, "text/vnd.turbo-stream.html"),
    ("text/html", ".turbo_stream", 200, "text/vnd.turbo-stream.html"),
    ("text/html", ".json", 406, "application/json"),
    ("text/vnd.turbo-stream.html", ".html", 406, "text/html"),
    ("application/json", "", 406, "application/json"),
)


def seed_messages(rust_db, camp_db):
    stamp = "2026-01-01T00:00:00Z"
    with sqlite3.connect(rust_db) as db:
        db.executemany(
            "INSERT INTO messages(id,room_id,creator_id,body,client_message_id,created_at,updated_at) VALUES(?1,1,1,'delete fixture',?2,?3,?3)",
            [(number, f"delete-{number}", stamp) for number in range(1, len(CASES) + 1)],
        )
    with sqlite3.connect(camp_db) as db:
        for number in range(1, len(CASES) + 1):
            db.execute(
                "INSERT INTO messages(id,room_id,creator_id,client_message_id,created_at,updated_at) VALUES(?1,1,1,?2,?3,?3)",
                (number, f"delete-{number}", stamp),
            )
            db.execute(
                "INSERT INTO action_text_rich_texts(name,body,record_type,record_id,created_at,updated_at) VALUES('body','delete fixture','Message',?1,?2,?2)",
                (number, stamp),
            )


def delete(port, cookie, csrf, database, number, accept, suffix):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
    try:
        connection.request("DELETE", f"/rooms/1/messages/{number}{suffix}", headers={
            "Cookie": cookie, "X-CSRF-Token": csrf, "Accept": accept,
        })
        response = connection.getresponse()
        result = response.status, response.getheader("Content-Type", "").split(";", 1)[0], response.getheader("Location"), response.read()
    finally:
        connection.close()
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT COUNT(*) FROM messages WHERE id=?", (number,)).fetchone() == (0,), number
    return result


def collect(port, cookie, csrf, database):
    return [delete(port, cookie, csrf, database, number, accept, suffix)
            for number, (accept, suffix, _, _) in enumerate(CASES, 1)]


def main():
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPOSITORY, text=True).strip() == REVISION
    with tempfile.TemporaryDirectory(prefix="paired-delete-formats-") as scratch:
        temp = pathlib.Path(scratch)
        rust_db, camp_db = temp / "rust.sqlite3", temp / "camp.sqlite3"
        rust_port, camp_port, redis_port = free_port(), free_port(), free_port()
        seed_rustfire(rust_db, rust_port, [])
        environment = seed_campfire(REPOSITORY, RUBY, BUNDLE, REPOSITORY / "storage/db/production.sqlite3", camp_db, [], camp_port, temp)
        seed_messages(rust_db, camp_db)
        checkout = isolated_campfire(temp, redis_port)
        environment["REDIS_URL"] = f"redis://127.0.0.1:{redis_port}"
        redis, redis_log = start_redis(temp, redis_port)
        try:
            rust = start_server(rust_db, rust_port)
            try:
                rust_results = collect(rust_port, "session_token=benchmark-session", "benchmark-csrf", rust_db)
            finally:
                stop_server(rust)
            with open(temp / "puma.log", "w+") as log:
                camp = subprocess.Popen([str(RUBY), str(RUBY.parent / "bundle"), "exec", "puma", "-C", "config/puma.rb"],
                                        cwd=checkout, env=environment, stdout=log, stderr=log)
                try:
                    wait_for_server(camp_port, camp)
                    cookie, csrf = login_campfire(camp_port)
                    camp_results = collect(camp_port, cookie, csrf, camp_db)
                finally:
                    stop_server(camp)
            for index, (_, _, status, media_type) in enumerate(CASES):
                rust_status, rust_type, rust_location, rust_body = rust_results[index]
                camp_status, camp_type, camp_location, camp_body = camp_results[index]
                assert (rust_status, rust_type, rust_location, rust_body) == (camp_status, camp_type, camp_location, camp_body), (index, rust_results[index], camp_results[index])
                assert (rust_status, rust_type, rust_location) == (status, media_type, None)
            with sqlite3.connect(camp_db) as db:
                assert db.execute("SELECT COUNT(*) FROM action_text_rich_texts WHERE record_type='Message' AND record_id BETWEEN 1 AND ?", (len(CASES),)).fetchone() == (0,)
        finally:
            redis.terminate()
            redis.wait(timeout=10)
            redis_log.close()
    print(f"PASS paired message deletion: {len(CASES)} response formats and saved removals")


if __name__ == "__main__":
    main()
