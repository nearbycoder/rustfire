"""Compare Campfire and Rustfire's inline boost controls and HTTP actions."""

import http.client
import pathlib
import re
import sqlite3
import subprocess
import tempfile
import urllib.parse

from direct_lookup import ROOT, free_port, start_server, stop_server
from paired_banned_content import start_redis
from paired_direct_lookup import login_campfire, seed_campfire, seed_rustfire, wait_for_server
from paired_turbo_fanout import MessageTagSequence, seed_boost_message


REPOSITORY = pathlib.Path("/tmp/once-campfire-reference")
RUBY = pathlib.Path("/tmp/rustfire-baseline/local/bin/ruby")
BUNDLE = pathlib.Path("/tmp/rustfire-baseline/bundle")
REVISION = "91d294f4a09f9bbe37f9548959bfcb43645678fb"


def request(port, cookie, method, path, csrf=None, fields=None, frame=None):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    headers = {"Cookie": cookie, "Accept": "text/html"}
    body = None
    if fields is not None:
        body = urllib.parse.urlencode([*fields.items(), ("authenticity_token", csrf)])
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    if csrf and method != "GET":
        headers["X-CSRF-Token"] = csrf
    if frame:
        headers["Turbo-Frame"] = frame
    try:
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        return response.status, response.getheader("Location"), response.read().decode()
    finally:
        connection.close()


def frame_html(page, frame_id):
    start = re.search(rf"<turbo-frame\b[^>]*\bid=['\"]{re.escape(frame_id)}['\"][^>]*>", page)
    assert start, (frame_id, page[:300])
    depth = 1
    for tag in re.finditer(r"</?turbo-frame\b[^>]*>", page[start.end():]):
        depth += -1 if tag.group().startswith("</") else 1
        if depth == 0:
            return page[start.start():start.end() + tag.end()]
    raise AssertionError(f"unclosed frame {frame_id}")


class BoostFrameTags(MessageTagSequence):
    def __init__(self, ignore_cached_csrf):
        super().__init__()
        self.ignore_cached_csrf = ignore_cached_csrf

    def handle_starttag(self, tag, attrs):
        if self.ignore_cached_csrf and tag == "input" and ("name", "authenticity_token") in attrs:
            return
        super().handle_starttag(tag, attrs)


def structure(page, frame_id, ignore_cached_csrf=False):
    parser = BoostFrameTags(ignore_cached_csrf)
    parser.feed(frame_html(page, frame_id))
    attrs = []
    for tag, pairs in parser.attributes:
        values = dict(pairs)
        if tag == "input" and values.get("name") == "authenticity_token":
            assert values.get("value"), "empty boost form CSRF token"
            values["value"] = "<csrf>"
        attrs.append((tag, tuple(sorted(values.items()))))
    return parser.tags, parser.attribute_keys, attrs, parser.text


def check_app(name, port, cookie, csrf, database, samples):
    for key, path, frame in (("index", "/messages/1/boosts", "boosting_message_boost-fixture"),
                             ("new", "/messages/1/boosts/new", "new_boost_message_boost-fixture"),
                             ("index-direct", "/messages/1/boosts", None),
                             ("new-direct", "/messages/1/boosts/new", None)):
        status, _, body = request(port, cookie, "GET", path, frame=frame)
        assert status == 200, (name, key, status)
        samples.joinpath(f"{name}-{key}.html").write_text(body)
    status, location, _ = request(port, cookie, "POST", "/messages/1/boosts", csrf, {"boost[content]": "🔥"})
    assert status in (302, 303), (name, status, location)
    assert urllib.parse.urlsplit(location).path == "/messages/1/boosts"
    with sqlite3.connect(database) as db:
        boost = db.execute("SELECT id,content,booster_id FROM boosts WHERE message_id=1").fetchone()
    assert boost and boost[1:] == ("🔥", 1), (name, boost)
    status, _, body = request(port, cookie, "GET", "/messages/1/boosts", frame="boosting_message_boost-fixture")
    assert status == 200
    samples.joinpath(f"{name}-created.html").write_text(body)
    status, _, body = request(port, cookie, "POST", f"/messages/1/boosts/{boost[0]}", csrf,
                              {"_method": "delete"}, frame="boosting_message_boost-fixture")
    samples.joinpath(f"{name}-deleted.html").write_text(body)
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT count(*) FROM boosts WHERE message_id=1").fetchone() == (0,)
    return status


def main():
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPOSITORY, text=True).strip() == REVISION
    with tempfile.TemporaryDirectory(prefix="paired-boost-controls-") as scratch:
        temp = pathlib.Path(scratch)
        rust_db, camp_db = temp / "rust.sqlite3", temp / "camp.sqlite3"
        rust_port, camp_port, redis_port = free_port(), free_port(), free_port()
        seed_rustfire(rust_db, rust_port, [])
        camp_env = seed_campfire(REPOSITORY, RUBY, BUNDLE, REPOSITORY / "storage/db/production.sqlite3", camp_db, [], camp_port, temp)
        camp_env["REDIS_URL"] = f"redis://127.0.0.1:{redis_port}"
        camp_env["WEB_CONCURRENCY"] = "1"
        seed_boost_message(rust_db, camp_db)
        with sqlite3.connect(camp_db) as camp, sqlite3.connect(rust_db) as rust_db_connection:
            name, bio, updated_at = camp.execute("SELECT name,bio,updated_at FROM users WHERE id=1").fetchone()
            rust_db_connection.execute("UPDATE users SET name=?,bio=?,updated_at=? WHERE id=1", (name, bio, updated_at))
        redis, redis_log = start_redis(temp, redis_port)
        try:
            rust = start_server(rust_db, rust_port, {"RUSTFIRE_CAMPFIRE_SECRET_KEY_BASE": camp_env["SECRET_KEY_BASE"]})
            try:
                rust_actions = check_app("rustfire", rust_port, "session_token=benchmark-session", "benchmark-csrf", rust_db, temp)
            finally:
                stop_server(rust)
            with open(temp / "puma.log", "w+") as log:
                camp = subprocess.Popen([str(RUBY), str(RUBY.parent / "bundle"), "exec", "puma", "-C", "config/puma.rb"],
                                        cwd=REPOSITORY, env=camp_env, stdout=log, stderr=log)
                try:
                    wait_for_server(camp_port, camp)
                    cookie, csrf = login_campfire(camp_port)
                    camp_actions = check_app("campfire", camp_port, cookie, csrf, camp_db, temp)
                except Exception:
                    log.flush()
                    log.seek(0)
                    print(log.read()[-2000:])
                    raise
                finally:
                    stop_server(camp)
            assert rust_actions == camp_actions, (rust_actions, camp_actions)
            for key, frame in (("index", "boosting_message_boost-fixture"),
                               ("new", "new_boost_message_boost-fixture"),
                               ("index-direct", "boosting_message_boost-fixture"),
                               ("new-direct", "new_boost_message_boost-fixture"),
                               ("created", "boosting_message_boost-fixture")):
                ignore_csrf = key in ("created", "new-direct")
                rust_markup = structure((temp / f"rustfire-{key}.html").read_text(), frame, ignore_csrf)
                camp_markup = structure((temp / f"campfire-{key}.html").read_text(), frame, ignore_csrf)
                mismatches = []
                for part, (left, right) in enumerate(zip(rust_markup, camp_markup)):
                    differences = [(index, a, b) for index, (a, b) in enumerate(zip(left, right)) if a != b]
                    if differences or len(left) != len(right):
                        mismatches.append((part, len(left), len(right), differences))
                assert not mismatches, (key, mismatches)
        finally:
            redis.terminate()
            redis.wait(timeout=10)
            redis_log.close()
    print("PASS paired boost index/new frames and create/delete actions")


if __name__ == "__main__":
    main()
