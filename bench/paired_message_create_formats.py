"""Compare public message-create format negotiation and saved side effects."""

import http.client
import pathlib
import sqlite3
import subprocess
import tempfile
import urllib.parse

from direct_lookup import free_port, start_server, stop_server
from paired_banned_content import BUNDLE, REPOSITORY, REVISION, RUBY, isolated_campfire, start_redis
from paired_direct_lookup import login_campfire, seed_campfire, seed_rustfire, wait_for_server


CASES = (
    ("/rooms/1/messages", "text/html", 406, "text/html"),
    ("/rooms/1/messages.html", "text/vnd.turbo-stream.html, text/html", 406, "text/html"),
    ("/rooms/1/messages.turbo_stream", "text/html", 200, "text/vnd.turbo-stream.html"),
    ("/rooms/1/messages.turbo_stream", "application/json", 200, "text/vnd.turbo-stream.html"),
    ("/rooms/1/messages.json", "text/html", 406, "application/json"),
    ("/rooms/1/messages", "application/json", 406, "application/json"),
    ("/rooms/1/messages", "text/vnd.turbo-stream.html, text/html", 200, "text/vnd.turbo-stream.html"),
)


def create(port, cookie, csrf, path, accept, number):
    body = urllib.parse.urlencode({
        "message[body]": f"format probe {number}",
        "message[client_message_id]": f"format-{number}",
        "authenticity_token": csrf,
    })
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
    try:
        connection.request("POST", path, body, {
            "Cookie": cookie,
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": accept,
        })
        response = connection.getresponse()
        return response.status, response.getheader("Content-Type", "").split(";", 1)[0], response.getheader("Location"), response.read()
    finally:
        connection.close()


def collect(port, cookie, csrf):
    return [create(port, cookie, csrf, path, accept, number)
            for number, (path, accept, _, _) in enumerate(CASES)]


def saved(database, rails):
    with sqlite3.connect(database) as db:
        rows = db.execute("SELECT id,client_message_id FROM messages ORDER BY id").fetchall()
        assert rows == [(number + 1, f"format-{number}") for number in range(len(CASES))], rows
        for number in range(len(CASES)):
            if rails:
                body = db.execute("SELECT body FROM action_text_rich_texts WHERE record_type='Message' AND record_id=?", (number + 1,)).fetchone()[0]
            else:
                body = db.execute("SELECT body FROM messages WHERE id=?", (number + 1,)).fetchone()[0]
            assert f"format probe {number}" in body, (number, body)


def main():
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPOSITORY, text=True).strip() == REVISION
    with tempfile.TemporaryDirectory(prefix="paired-create-formats-") as scratch:
        temp = pathlib.Path(scratch)
        rust_db, camp_db = temp / "rust.sqlite3", temp / "camp.sqlite3"
        rust_port, camp_port, redis_port = free_port(), free_port(), free_port()
        seed_rustfire(rust_db, rust_port, [])
        environment = seed_campfire(REPOSITORY, RUBY, BUNDLE, REPOSITORY / "storage/db/production.sqlite3", camp_db, [], camp_port, temp)
        with sqlite3.connect(camp_db) as db:
            db.execute("DELETE FROM sqlite_sequence WHERE name='messages'")
        checkout = isolated_campfire(temp, redis_port)
        environment["REDIS_URL"] = f"redis://127.0.0.1:{redis_port}"
        redis, redis_log = start_redis(temp, redis_port)
        try:
            rust = start_server(rust_db, rust_port)
            try:
                rust_results = collect(rust_port, "session_token=benchmark-session", "benchmark-csrf")
            finally:
                stop_server(rust)
            with open(temp / "puma.log", "w+") as log:
                camp = subprocess.Popen([str(RUBY), str(RUBY.parent / "bundle"), "exec", "puma", "-C", "config/puma.rb"],
                                        cwd=checkout, env=environment, stdout=log, stderr=log)
                try:
                    wait_for_server(camp_port, camp)
                    cookie, csrf = login_campfire(camp_port)
                    camp_results = collect(camp_port, cookie, csrf)
                finally:
                    stop_server(camp)
            for number, (_, _, status, media_type) in enumerate(CASES):
                rust_status, rust_type, rust_location, rust_body = rust_results[number]
                camp_status, camp_type, camp_location, camp_body = camp_results[number]
                assert (rust_status, rust_type, rust_location) == (camp_status, camp_type, camp_location) == (status, media_type, None), (number, rust_results[number][:3], camp_results[number][:3])
                if status == 406:
                    assert rust_body == camp_body, (number, rust_body, camp_body)
                else:
                    assert f"format probe {number}".encode() in rust_body and f"format probe {number}".encode() in camp_body
            saved(rust_db, False)
            saved(camp_db, True)
        finally:
            redis.terminate()
            redis.wait(timeout=10)
            redis_log.close()
    print(f"PASS paired message creation formats, 406 bodies, and all {len(CASES)} saved messages")


if __name__ == "__main__":
    main()
