"""Compare representative successful response bodies with pinned Campfire.

Run after ``cargo build --release``. Dynamic avatar signatures and server origins
are normalized; the message's text, markup, metadata, and JSON remain checked.
"""

import datetime
import http.client
import json
import pathlib
import re
import sqlite3
import subprocess
import tempfile

from direct_lookup import free_port, start_server, stop_server
from paired_banned_content import BUNDLE, REPOSITORY, REVISION, RUBY, isolated_campfire, start_redis
from paired_direct_lookup import login_campfire, seed_campfire, seed_rustfire, wait_for_server
from paired_room_shell import Markup
from paired_turbo_fanout import seed_boost_message


CASES = (
    ("/up", "text/html"),
    ("/up.html", "application/json"),
    ("/up.json", "text/html"),
    ("/up?format=json", "text/html"),
    ("/up", "application/json"),
    ("/service-worker.js", "text/javascript"),
    ("/rooms/1/invalid/messages.html", "text/html"),
    ("/rooms/1/invalid/messages.json", "text/html"),
    ("/rooms/1/invalid/messages?format=html", "text/html"),
)


def request(port, cookie, path, accept):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
    try:
        connection.request("GET", path, headers={"Cookie": cookie, "Accept": accept})
        response = connection.getresponse()
        return response.status, response.getheader("Content-Type"), response.read()
    finally:
        connection.close()


def collect(port, cookie):
    return {case: request(port, cookie, *case) for case in CASES}


def canonical(value):
    if isinstance(value, str):
        value = re.sub(r"http://127\.0\.0\.1:\d+", "<origin>", value)
        return re.sub(r"(/users/)[A-Za-z0-9_-]+--[a-f0-9]+(/avatar\?v=\d+)", r"\1<signed>\2", value)
    if isinstance(value, list):
        return [canonical(item) for item in value]
    if isinstance(value, tuple):
        return tuple(canonical(item) for item in value)
    if isinstance(value, dict):
        return {key: canonical(item) for key, item in value.items()}
    return value


def markup(body):
    parser = Markup()
    parser.feed(body.decode())
    tokens = []
    for token in parser.tokens:
        if token[:2] == ("start", "input") and ("name", "authenticity_token") in token[2]:
            tokens.append(("start", "input", tuple((key, "<csrf>" if key == "value" else value) for key, value in token[2])))
        else:
            tokens.append(token)
    return canonical(tokens)


def check(rust, camp):
    now = datetime.datetime.now(datetime.timezone.utc)
    for case in CASES:
        rust_status, rust_type, rust_body = rust[case]
        camp_status, camp_type, camp_body = camp[case]
        assert (rust_status, rust_type) == (camp_status, camp_type) == (200, camp_type), (case, rust_status, rust_type, camp_status, camp_type)
        path, accept = case
        if path.startswith("/up") and (path.endswith(".json") or path.endswith("?format=json") or (accept == "application/json" and not path.endswith(".html"))):
            normalized = []
            for body in (rust_body, camp_body):
                value = json.loads(body)
                assert set(value) == {"status", "timestamp"} and value["status"] == "up", (case, value)
                timestamp = datetime.datetime.fromisoformat(value["timestamp"].replace("Z", "+00:00"))
                assert abs((now - timestamp).total_seconds()) < 20, (case, value)
                assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", value["timestamp"]), (case, value)
                normalized.append(body.replace(value["timestamp"].encode(), b"<timestamp>"))
            assert normalized[0] == normalized[1], (case, normalized)
        elif path.endswith(".json") or path.endswith("?format=html"):
            assert canonical(json.loads(rust_body)) == canonical(json.loads(camp_body)), (case, rust_body[:250], camp_body[:250])
        elif path.endswith("messages.html"):
            rust_tokens, camp_tokens = markup(rust_body), markup(camp_body)
            assert rust_tokens == camp_tokens, (case, len(rust_tokens), len(camp_tokens), next(((i, a, b) for i, (a, b) in enumerate(zip(rust_tokens, camp_tokens)) if a != b), None))
        else:
            assert rust_body == camp_body, (case, len(rust_body), len(camp_body))


def main():
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPOSITORY, text=True).strip() == REVISION
    with tempfile.TemporaryDirectory(prefix="paired-success-bodies-") as scratch:
        temp = pathlib.Path(scratch)
        rust_db, camp_db = temp / "rust.sqlite3", temp / "camp.sqlite3"
        rust_port, camp_port, redis_port = free_port(), free_port(), free_port()
        seed_rustfire(rust_db, rust_port, [])
        camp_env = seed_campfire(REPOSITORY, RUBY, BUNDLE, REPOSITORY / "storage/db/production.sqlite3", camp_db, [], camp_port, temp)
        seed_boost_message(rust_db, camp_db)
        with sqlite3.connect(camp_db) as db:
            db.execute("UPDATE users SET name='User 1',updated_at='2026-01-01 00:00:00' WHERE id=1")
            db.execute("UPDATE rooms SET name='Campfire' WHERE id=1")
        checkout = isolated_campfire(temp, redis_port)
        camp_env["REDIS_URL"] = f"redis://127.0.0.1:{redis_port}"
        redis, redis_log = start_redis(temp, redis_port)
        try:
            process = start_server(rust_db, rust_port)
            try:
                rust = collect(rust_port, "session_token=benchmark-session")
            finally:
                stop_server(process)
            with open(temp / "puma.log", "w+") as log:
                process = subprocess.Popen([str(RUBY), str(RUBY.parent / "bundle"), "exec", "puma", "-C", "config/puma.rb"], cwd=checkout, env=camp_env, stdout=log, stderr=log)
                try:
                    wait_for_server(camp_port, process)
                    cookie, _ = login_campfire(camp_port)
                    camp = collect(camp_port, cookie)
                finally:
                    stop_server(process)
            check(rust, camp)
        finally:
            redis.terminate()
            redis.wait(timeout=10)
            redis_log.close()
    print(f"PASS {len(CASES)} paired successful response bodies, including complete bot HTML/JSON and byte-identical health HTML/service worker")


if __name__ == "__main__":
    main()
