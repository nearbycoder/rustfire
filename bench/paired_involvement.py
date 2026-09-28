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
HELP_AGENTS = {
    "Chrome Linux": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Chrome Windows": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Chrome Android": "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36",
    "Chrome iPhone": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) CriOS/120.0.6045.109 Mobile/15E148 Safari/604.1",
    "Firefox Windows": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0",
    "Firefox Android": "Mozilla/5.0 (Android 14; Mobile; rv:121.0) Gecko/121.0 Firefox/121.0",
    "Firefox iPhone": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_2 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) FxiOS/120.0 Mobile/15E148 Safari/605.1.15",
    "Safari macOS": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Safari/605.1.15",
    "Safari iPhone": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_2 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Mobile/15E148 Safari/604.1",
    "Edge Windows": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 Edg/120.0.0.0",
}
HELP_ASSETS = (
    "external/gear-50e77d2b.svg",
    "external/install-edge-0b7cd918.svg",
    "external/share-f9d3e998.svg",
    "external/sliders-3979a007.svg",
    "external/switch-eec22a2d.svg",
    "external/web-24ffe636.svg",
    "lock-7cdf4b3d.svg",
    "menu-dots-vertical-c247e3cc.svg",
)


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


def request(port, method, path, cookie, csrf, frame=None, body=b"", agent=None):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    headers = {"Cookie": cookie, "Accept": "text/html", "X-CSRF-Token": csrf}
    if frame:
        headers["Turbo-Frame"] = frame
    if agent:
        headers["User-Agent"] = agent
    if method == "POST":
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    try:
        connection.request(method, path, body, headers)
        response = connection.getresponse()
        return response.status, response.getheader("Location"), response.read().decode()
    finally:
        connection.close()


def frame_events(page, frame_id=None):
    start = rf'<turbo-frame\b[^>]*\bid=["\']{re.escape(frame_id)}["\'][^>]*>' if frame_id else r"<turbo-frame\b[^>]*>"
    match = re.search(start + r".*?</turbo-frame>", page, re.S)
    assert match, page[:300]
    parser = FrameParser()
    parser.feed(match.group())
    return parser.events


def help_snapshots(port, cookie, csrf):
    def normalize(value):
        if isinstance(value, str):
            return re.sub(r"http://127\.0\.0\.1:\d+/", "ROOT_URL", value)
        if isinstance(value, tuple):
            return tuple(normalize(item) for item in value)
        return value

    snapshots = {}
    for name, agent in HELP_AGENTS.items():
        status, _, page = request(port, "GET", "/rooms/1", cookie, csrf, agent=agent)
        assert status == 200, (name, status, page[:300])
        match = re.search(r'<dialog\b[^>]*data-notifications-target=["\']notAllowedNotice["\'][^>]*>.*?</dialog>', page, re.S)
        assert match, (name, page[:300])
        dialog = match.group().replace("Campfire", "Rustfire")
        dialog = re.sub(r"http://127\.0\.0\.1:\d+/", "ROOT_URL", dialog)
        parser = FrameParser()
        parser.feed(dialog)
        snapshots[name] = [normalize(event) for event in parser.events]
    return snapshots


def room_state(database, room_id):
    with sqlite3.connect(database) as db:
        return db.execute(
            "SELECT involvement FROM memberships WHERE room_id=? AND user_id=1", (room_id,)
        ).fetchone()[0]


def membership_updated_at(database, room_id):
    with sqlite3.connect(database) as db:
        return db.execute("SELECT updated_at FROM memberships WHERE room_id=? AND user_id=1", (room_id,)).fetchone()[0]


def workflow(port, cookie, csrf, database):
    observations = []
    for room_id, kind, levels in (
        (1, "open", ["mentions", "everything", "nothing", "invisible", "mentions"]),
        (2, "direct", ["everything", "nothing", "everything"]),
        (3, "closed", ["mentions", "everything", "nothing"]),
    ):
        frame_id = f"involvement_rooms_{kind}_{room_id}"
        path = f"/rooms/{room_id}/involvement"
        status, _, room_page = request(port, "GET", f"/rooms/{room_id}", cookie, csrf)
        assert status == 200, (room_id, status, room_page[:300])
        observations.append(("bell", room_id, frame_events(room_page, frame_id)))
        for current, following in zip(levels, levels[1:]):
            assert room_state(database, room_id) == current
            status, _, page = request(port, "GET", path, cookie, csrf, frame_id)
            assert status == 200, (status, page[:300])
            events = frame_events(page)
            action = f"{path}?involvement={following}"
            assert ("start", "form", (("action", action), ("class", "button_to"), ("method", "post"))) in events
            assert any(event[:2] == ("start", "turbo-frame") and ("id", frame_id) in event[2] for event in events)
            if room_id == 1 and current == "mentions":
                rejected = []
                for label, method in (("plain-post", None), ("delete-override", "delete")):
                    fields = {"authenticity_token": csrf}
                    if method:
                        fields["_method"] = method
                    invalid_body = urllib.parse.urlencode(fields).encode()
                    invalid_status, _, _ = request(port, "POST", action, cookie, csrf, body=invalid_body)
                    rejected.append((label, invalid_status, room_state(database, room_id)))
                assert rejected == [("plain-post", 404, current), ("delete-override", 404, current)], rejected
                observations.append(("rejected", rejected))
                unchanged_at = membership_updated_at(database, room_id)
                same_body = urllib.parse.urlencode({"_method": "put", "authenticity_token": csrf}).encode()
                same_status, _, _ = request(port, "POST", f"{path}?involvement={current}", cookie, csrf, body=same_body)
                same_touched = membership_updated_at(database, room_id) != unchanged_at
                assert same_status == 302 and not same_touched, (same_status, same_touched)
                observations.append(("unchanged involvement", same_status, same_touched))
            previous_at = membership_updated_at(database, room_id)
            body = urllib.parse.urlencode({"_method": "put", "authenticity_token": csrf}).encode()
            status, location, _ = request(port, "POST", action, cookie, csrf, body=body)
            assert status == 302 and urllib.parse.urlsplit(location).path == path, (status, location)
            assert room_state(database, room_id) == following
            changed_touched = membership_updated_at(database, room_id) != previous_at
            assert changed_touched, (room_id, current, following)
            observations.append((room_id, current, following, events, changed_touched))
    return observations


def main():
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPOSITORY, text=True).strip()
    assert revision == REVISION, revision
    for asset in HELP_ASSETS:
        assert (pathlib.Path(__file__).resolve().parents[1] / "static/assets" / asset).read_bytes() == (REPOSITORY / "public/assets" / asset).read_bytes(), asset
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
                rust_help = help_snapshots(rust_port, "session_token=benchmark-session", "benchmark-csrf")
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
                    camp_help = help_snapshots(camp_port, cookie, csrf)
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
    for name in HELP_AGENTS:
        rust, camp = rust_help[name], camp_help[name]
        assert rust == camp, (name, next(((index, left, right) for index, (left, right) in enumerate(zip(rust, camp)) if left != right), (len(rust), len(camp))))
    print(f"PASS {len(rust_observations)} paired involvement frames, redirects, and persisted transitions")
    print(f"PASS notification help markup for {len(HELP_AGENTS)} browser and system profiles")


if __name__ == "__main__":
    main()
