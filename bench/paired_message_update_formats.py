"""Compare public message-edit formats and saved changes against pinned Campfire."""

import http.client
import pathlib
import sqlite3
import subprocess
import tempfile
import urllib.parse

from direct_lookup import free_port, start_server, stop_server
from paired_banned_content import BUNDLE, REPOSITORY, REVISION, RUBY, isolated_campfire, start_redis
from paired_direct_lookup import login_campfire, seed_campfire, seed_rustfire, wait_for_server
from paired_turbo_fanout import seed_boost_message


CASES = (
    ("PATCH", "/rooms/1/messages/1", "text/html", 302, "text/html"),
    ("PUT", "/rooms/1/messages/1", "text/vnd.turbo-stream.html, text/html", 302, "text/html"),
    ("PATCH", "/rooms/1/messages/1.html", "application/json", 302, "text/html"),
    ("PUT", "/rooms/1/messages/1.json", "text/html", 500, "application/json"),
    ("PATCH", "/rooms/1/messages/1", "application/json", 500, "application/json"),
)


def saved(database, rails, number):
    with sqlite3.connect(database) as db:
        if rails:
            text = db.execute("SELECT body FROM action_text_rich_texts WHERE record_type='Message' AND record_id=1").fetchone()[0]
        else:
            text = db.execute("SELECT body FROM messages WHERE id=1").fetchone()[0]
    assert f"edit format {number}" in text, (number, text)


def update(port, cookie, csrf, database, rails, number, method, path, accept):
    body = urllib.parse.urlencode({"message[body]": f"edit format {number}", "authenticity_token": csrf})
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
    try:
        connection.request(method, path, body, {
            "Cookie": cookie,
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": accept,
        })
        response = connection.getresponse()
        location = response.getheader("Location")
        result = (response.status, response.getheader("Content-Type", "").split(";", 1)[0],
                  urllib.parse.urlsplit(location).path if location else None, response.read())
    finally:
        connection.close()
    saved(database, rails, number)
    return result


def collect(port, cookie, csrf, database, rails):
    return [update(port, cookie, csrf, database, rails, number, method, path, accept)
            for number, (method, path, accept, _, _) in enumerate(CASES)]


def main():
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPOSITORY, text=True).strip() == REVISION
    with tempfile.TemporaryDirectory(prefix="paired-edit-formats-") as scratch:
        temp = pathlib.Path(scratch)
        rust_db, camp_db = temp / "rust.sqlite3", temp / "camp.sqlite3"
        rust_port, camp_port, redis_port = free_port(), free_port(), free_port()
        seed_rustfire(rust_db, rust_port, [])
        environment = seed_campfire(REPOSITORY, RUBY, BUNDLE, REPOSITORY / "storage/db/production.sqlite3", camp_db, [], camp_port, temp)
        seed_boost_message(rust_db, camp_db)
        checkout = isolated_campfire(temp, redis_port)
        environment["REDIS_URL"] = f"redis://127.0.0.1:{redis_port}"
        redis, redis_log = start_redis(temp, redis_port)
        try:
            rust = start_server(rust_db, rust_port)
            try:
                rust_results = collect(rust_port, "session_token=benchmark-session", "benchmark-csrf", rust_db, False)
            finally:
                stop_server(rust)
            with open(temp / "puma.log", "w+") as log:
                camp = subprocess.Popen([str(RUBY), str(RUBY.parent / "bundle"), "exec", "puma", "-C", "config/puma.rb"],
                                        cwd=checkout, env=environment, stdout=log, stderr=log)
                try:
                    wait_for_server(camp_port, camp)
                    cookie, csrf = login_campfire(camp_port)
                    camp_results = collect(camp_port, cookie, csrf, camp_db, True)
                finally:
                    stop_server(camp)
            for number, (_, _, _, status, media_type) in enumerate(CASES):
                rust_status, rust_type, rust_location, rust_body = rust_results[number]
                camp_status, camp_type, camp_location, camp_body = camp_results[number]
                destination = "/rooms/1/messages/1" if status == 302 else None
                assert (rust_status, rust_type, rust_location) == (camp_status, camp_type, camp_location) == (status, media_type, destination), (number, rust_results[number][:3], camp_results[number][:3])
                assert rust_body == camp_body, (number, rust_body, camp_body)
        finally:
            redis.terminate()
            redis.wait(timeout=10)
            redis_log.close()
    print(f"PASS paired message edits: {len(CASES)} public format/method cases and saved changes")


if __name__ == "__main__":
    main()
