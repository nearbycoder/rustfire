"""Compare Campfire's rendered rich-text sanitization with Rustfire."""

import http.client
import pathlib
import sqlite3
import subprocess
import tempfile
import urllib.parse

from direct_lookup import free_port, start_server, stop_server
from paired_banned_content import BUNDLE, REPOSITORY, REVISION, RUBY, start_redis
from paired_direct_lookup import login_campfire, seed_campfire, seed_rustfire, wait_for_server
from paired_link_preview import Presentation


CASES = [
    ("unattached-image", "<div>Hello <img src='https://evil.example/image.svg'>World</div>"),
    ("event-handler", "<div><a href='/x' onmouseover='alert(1)'>x</a> <span onclick='alert(2)'>y</span></div>"),
    ("data-link", "<div><a href='data:text/html,pwned'>x</a></div>"),
    ("formatting", "<div><a href='https://example.com'>example</a> <strong>bold</strong> <code>code</code><ul><li>one</li><li>two</li></ul></div>"),
]


def post(port, cookie, csrf, case):
    name, body = case
    client_id = f"paired-rich-filter-{name}"
    fields = urllib.parse.urlencode({
        "message[body]": body,
        "message[client_message_id]": client_id,
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
        payload = response.read()
        assert response.status == 200, (name, response.status, payload[:300])
        presentation = Presentation(client_id)
        presentation.feed(payload.decode())
        assert presentation.structure, (name, payload[:300])
        return presentation.structure
    finally:
        connection.close()


def main():
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPOSITORY, text=True).strip() == REVISION
    with tempfile.TemporaryDirectory(prefix="paired-rich-filters-") as scratch:
        temp = pathlib.Path(scratch)
        rust_db, camp_db = temp / "rust.sqlite3", temp / "camp.sqlite3"
        rust_port, camp_port, redis_port = free_port(), free_port(), free_port()
        seed_rustfire(rust_db, rust_port, [])
        camp_env = seed_campfire(REPOSITORY, RUBY, BUNDLE, REPOSITORY / "storage/db/production.sqlite3", camp_db, [], camp_port, temp)
        camp_env["REDIS_URL"] = f"redis://127.0.0.1:{redis_port}"
        camp_env["WEB_CONCURRENCY"] = "1"
        with sqlite3.connect(camp_db) as camp, sqlite3.connect(rust_db) as rust:
            camp.execute("DELETE FROM sqlite_sequence WHERE name='messages'")
            rust.executemany("UPDATE users SET name=?2,updated_at=?3 WHERE id=?1", camp.execute("SELECT id,name,updated_at FROM users WHERE id IN(1,2)").fetchall())
            rust.execute("UPDATE rooms SET name=? WHERE id=1", [camp.execute("SELECT name FROM rooms WHERE id=1").fetchone()[0]])
        redis, redis_log = start_redis(temp, redis_port)
        try:
            rust = start_server(rust_db, rust_port, {"RUSTFIRE_CAMPFIRE_SECRET_KEY_BASE": camp_env["SECRET_KEY_BASE"]})
            try:
                rust_results = [post(rust_port, "session_token=benchmark-session", "benchmark-csrf", case) for case in CASES]
            finally:
                stop_server(rust)
            with open(temp / "puma.log", "w+") as log:
                camp = subprocess.Popen([str(RUBY), str(RUBY.parent / "bundle"), "exec", "puma", "-C", "config/puma.rb"], cwd=REPOSITORY, env=camp_env, stdout=log, stderr=log)
                try:
                    wait_for_server(camp_port, camp)
                    cookie, csrf = login_campfire(camp_port)
                    camp_results = [post(camp_port, cookie, csrf, case) for case in CASES]
                finally:
                    stop_server(camp)
        finally:
            redis.terminate()
            redis.wait(timeout=10)
            redis_log.close()
    for case, rust_result, camp_result in zip(CASES, rust_results, camp_results):
        assert rust_result == camp_result, (case[0], rust_result, camp_result)
    print("PASS paired rich-text image removal, unsafe attributes, data links, and formatting")


if __name__ == "__main__":
    main()
