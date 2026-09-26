"""Compare room notification controls and transitions with pinned Campfire.

Run after ``cargo build --release``. Both apps use disposable databases and
Campfire gets an isolated Redis instance.
"""

from html.parser import HTMLParser
import http.client
import pathlib
import re
import sqlite3
import subprocess
import tempfile
import urllib.parse

from direct_lookup import free_port, start_server, stop_server
from paired_banned_content import start_redis
from paired_direct_lookup import login_campfire, seed_campfire, seed_rustfire, wait_for_server


REPOSITORY = pathlib.Path("/tmp/once-campfire-reference")
RUBY = pathlib.Path("/tmp/rustfire-baseline/local/bin/ruby")
BUNDLE = pathlib.Path("/tmp/rustfire-baseline/bundle")
REVISION = "91d294f4a09f9bbe37f9548959bfcb43645678fb"


class FrameParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.events = []

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "input" and attributes.get("name") == "authenticity_token":
            attributes["value"] = "<csrf>"
        self.events.append(("start", tag, tuple(sorted(attributes.items()))))

    def handle_endtag(self, tag):
        self.events.append(("end", tag))

    def handle_data(self, data):
        if value := data.strip():
            self.events.append(("text", value))


def request(port, method, path, cookie, csrf, frame=None, body=b""):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    headers = {"Cookie": cookie, "Accept": "text/html", "X-CSRF-Token": csrf}
    if frame:
        headers["Turbo-Frame"] = frame
    if method == "POST":
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    try:
        connection.request(method, path, body, headers)
        response = connection.getresponse()
        return response.status, response.getheader("Location"), response.read().decode()
    finally:
        connection.close()


def frame_events(page):
    match = re.search(r"<turbo-frame\b.*?</turbo-frame>", page, re.S)
    assert match, page[:300]
    parser = FrameParser()
    parser.feed(match.group())
    return parser.events


def room_state(database, room_id):
    with sqlite3.connect(database) as db:
        return db.execute(
            "SELECT involvement FROM memberships WHERE room_id=? AND user_id=1", (room_id,)
        ).fetchone()[0]


def workflow(port, cookie, csrf, database):
    observations = []
    for room_id, kind, levels in (
        (1, "open", ["mentions", "everything", "nothing", "invisible", "mentions"]),
        (2, "direct", ["everything", "nothing", "everything"]),
        (3, "closed", ["mentions", "everything", "nothing"]),
    ):
        frame_id = f"involvement_rooms_{kind}_{room_id}"
        path = f"/rooms/{room_id}/involvement"
        for current, following in zip(levels, levels[1:]):
            assert room_state(database, room_id) == current
            status, _, page = request(port, "GET", path, cookie, csrf, frame_id)
            assert status == 200, (status, page[:300])
            events = frame_events(page)
            action = f"{path}?involvement={following}"
            assert ("start", "form", (("action", action), ("class", "button_to"), ("method", "post"))) in events
            assert any(event[:2] == ("start", "turbo-frame") and ("id", frame_id) in event[2] for event in events)
            body = urllib.parse.urlencode({"_method": "put", "authenticity_token": csrf}).encode()
            status, location, _ = request(port, "POST", action, cookie, csrf, body=body)
            assert status == 302 and urllib.parse.urlsplit(location).path == path, (status, location)
            assert room_state(database, room_id) == following
            observations.append((room_id, current, following, events))
    return observations


def main():
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPOSITORY, text=True).strip()
    assert revision == REVISION, revision
    with tempfile.TemporaryDirectory(prefix="paired-involvement-") as temporary:
        temp = pathlib.Path(temporary)
        rust_db, camp_db = temp / "rust.sqlite3", temp / "camp.sqlite3"
        rust_port, camp_port, redis_port = free_port(), free_port(), free_port()
        seed_rustfire(rust_db, rust_port, [(2,)])
        camp_env = seed_campfire(
            REPOSITORY, RUBY, BUNDLE, REPOSITORY / "storage/db/production.sqlite3",
            camp_db, [(2,)], camp_port, temp,
        )
        with sqlite3.connect(rust_db) as db:
            db.execute("INSERT INTO rooms(id,name,type,creator_id,created_at,updated_at) VALUES(3,'Private','Rooms::Closed',1,'2026-01-01T00:00:00Z','2026-01-01T00:00:00Z')")
            db.execute("INSERT INTO memberships(room_id,user_id,involvement,created_at) VALUES(3,1,'mentions','2026-01-01T00:00:00Z')")
        with sqlite3.connect(camp_db) as db:
            db.execute("INSERT INTO rooms(id,name,type,creator_id,created_at,updated_at) VALUES(3,'Private','Rooms::Closed',1,'2026-01-01 00:00:00','2026-01-01 00:00:00')")
            db.execute("INSERT INTO memberships(room_id,user_id,involvement,created_at,updated_at) VALUES(3,1,'mentions','2026-01-01 00:00:00','2026-01-01 00:00:00')")
        redis, redis_log = start_redis(temp, redis_port)
        camp_env["REDIS_URL"] = f"redis://127.0.0.1:{redis_port}"
        try:
            rust = start_server(rust_db, rust_port)
            try:
                rust_observations = workflow(rust_port, "session_token=benchmark-session", "benchmark-csrf", rust_db)
            finally:
                stop_server(rust)
            with open(temp / "puma.log", "w+") as log:
                camp = subprocess.Popen(
                    [str(RUBY), str(RUBY.parent / "bundle"), "exec", "puma", "-C", "config/puma.rb"],
                    cwd=REPOSITORY, env=camp_env, stdout=log, stderr=log,
                )
                try:
                    wait_for_server(camp_port, camp)
                    cookie, csrf = login_campfire(camp_port)
                    camp_observations = workflow(camp_port, cookie, csrf, camp_db)
                except Exception:
                    log.flush()
                    log.seek(0)
                    print(log.read()[-2000:])
                    raise
                finally:
                    stop_server(camp)
        finally:
            redis.terminate()
            redis.wait(timeout=10)
            redis_log.close()
    assert rust_observations == camp_observations, next(
        ((rust, camp) for rust, camp in zip(rust_observations, camp_observations) if rust != camp), None
    )
    print(f"PASS {len(rust_observations)} paired involvement frames, redirects, and persisted transitions")


if __name__ == "__main__":
    main()
