"""Compare the room page shell against pinned Campfire on disposable fixtures.

The probe deliberately covers the navigation, composer footer, initial sidebar
frame, rendered message area, and parsed optimistic-message template. It does
not claim full page or browser-behavior parity.
Run after ``cargo build --release``.
"""

import argparse
import html
import http.client
from html.parser import HTMLParser
import json
import pathlib
import re
import sqlite3
import subprocess
import tempfile
import urllib.parse

from direct_lookup import free_port, start_server, stop_server
from paired_account_logo import multipart
from paired_bot_admin import cleanup_campfire_uploads, request
from paired_direct_lookup import login_campfire, seed_campfire, seed_rustfire, wait_for_server


VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"


class Section(HTMLParser):
    def __init__(self, target, normalize_times=False):
        super().__init__(convert_charrefs=True)
        self.target = target
        self.normalize_times = normalize_times
        self.depth = 0
        self.tokens = []

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if self.depth == 0 and values.get("id") == self.target:
            self.depth = 1
        elif self.depth and tag not in VOID:
            self.depth += 1
        if self.depth:
            normalized = []
            for key, value in attrs:
                if key == "value" and values.get("name") == "authenticity_token":
                    value = "<csrf>"
                elif self.normalize_times and key in {"data-refresh-room-loaded-at-value", "data-message-timestamp", "data-message-updated-at", "data-sort-value", "datetime"}:
                    assert value, (key, value)
                    value = "<generated-time>"
                elif value:
                    value = re.sub(r"http://127\.0\.0\.1(?::\d+)?", "<origin>", value)
                    if key in {"href", "data-lightbox-url-value"} and value.startswith("/qr_code/"):
                        value = "<origin-specific-qr>"
                    if key == "src" and value.startswith("/account/logo?v="):
                        assert value.split("?v=", 1)[1].isdigit(), value
                        value = "<versioned-logo>"
                    value = value.replace("Campfire", "Rustfire")
                normalized.append((key, value))
            self.tokens.append(("start", tag, tuple(sorted(normalized))))

    def handle_endtag(self, tag):
        if self.depth and tag not in VOID:
            self.tokens.append(("end", tag))
            self.depth -= 1

    def handle_data(self, data):
        if self.depth and data.strip():
            text = " ".join(data.split()).replace("Campfire", "Rustfire")
            if self.target == "message-area" and "$messageClasses$" in text:
                text = "<message-template>"
            self.tokens.append(("text", text))


def section(page, target, normalize_times=False):
    parser = Section(target, normalize_times)
    parser.feed(page.decode())
    assert parser.tokens, f"Missing {target}"
    return parser.tokens


class Markup(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tokens = []

    def handle_starttag(self, tag, attrs):
        self.tokens.append(("start", tag, tuple(sorted(attrs))))

    def handle_endtag(self, tag):
        if tag not in VOID:
            self.tokens.append(("end", tag))

    def handle_data(self, data):
        if data.strip():
            self.tokens.append(("text", " ".join(data.split()).replace("Campfire", "Rustfire")))


def message_template(page):
    match = re.search(rb'<script type="text/template" data-messages-target="template">(.*?)</script>', page, re.S)
    assert match, "Missing optimistic-message template"
    parser = Markup()
    parser.feed(html.unescape(match.group(1).decode()))
    return parser.tokens


def get_room(port, cookie, path):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    try:
        connection.request("GET", path, headers={"Cookie": cookie, "User-Agent": AGENT, "Accept": "text/html"})
        response = connection.getresponse()
        body = response.read()
        assert response.status == 200, (response.status, body[:200])
        return body
    finally:
        connection.close()


def post_message(port, cookie, csrf):
    payload = urllib.parse.urlencode({
        "message[body]": "Room page message check",
        "message[client_message_id]": "room-page-1",
        "authenticity_token": csrf,
    })
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    try:
        connection.request("POST", "/rooms/1/messages", payload, {
            "Cookie": cookie,
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "text/vnd.turbo-stream.html, text/html",
            "User-Agent": AGENT,
        })
        response = connection.getresponse()
        body = response.read()
        assert response.status == 200 and response.getheader("Content-Type", "").startswith("text/vnd.turbo-stream.html"), (response.status, body[:200])
    finally:
        connection.close()


def post_account_logo(port, cookie, csrf, jpeg):
    payload, content_type = multipart(jpeg)
    status, location, body = request(port, "PATCH", "/account", cookie, csrf, payload, content_type)
    assert status in {302, 303} and location, (status, body[:200])


def measure_room_page(binary, port, cookie, clients, seconds):
    result = subprocess.run([
        str(binary), "--base", f"http://127.0.0.1:{port}", "--path", "/rooms/1",
        "--cookie", cookie, "--accept", "text/html", "--expected-status", "200",
        "--expected-content-type", "text/html", "--expected-message-count", "1",
        "--clients", str(clients), "--seconds", str(seconds),
    ], capture_output=True, text=True, check=True)
    report = json.loads(result.stdout)
    assert report["errors"] == 0 and report["successes"] > 0, report
    return report


def assert_equal(label, expected, actual):
    for index, (left, right) in enumerate(zip(expected, actual)):
        assert left == right, (label, index, left, right)
    assert len(expected) == len(actual), (label, len(expected), len(actual))
    print(f"{label}: {len(actual)} matching parsed tokens")


def compare_room(label, path, camp_port, camp_cookie, rust_port, rust_cookie, sample_dir, normalize_times=False):
    source = get_room(camp_port, camp_cookie, path)
    target = get_room(rust_port, rust_cookie, path)
    if sample_dir:
        sample_dir.mkdir(parents=True, exist_ok=True)
        stem = label.replace(" ", "-")
        (sample_dir / f"campfire-{stem}.html").write_bytes(source)
        (sample_dir / f"rustfire-{stem}.html").write_bytes(target)
    for part in ("nav", "footer", "sidebar", "message-area"):
        assert_equal(f"{label} {part}", section(source, part, normalize_times), section(target, part, normalize_times))
    assert_equal(f"{label} message template", message_template(source), message_template(target))
    assert f'name="current-room-id" content="{path.rsplit("/", 1)[-1]}"' in target.decode()
    return source, target


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--campfire-repo", type=pathlib.Path, default=pathlib.Path("/tmp/once-campfire-reference"))
    parser.add_argument("--ruby", type=pathlib.Path, default=pathlib.Path("/tmp/rustfire-baseline/local/bin/ruby"))
    parser.add_argument("--bundle-path", type=pathlib.Path, default=pathlib.Path("/tmp/rustfire-baseline/bundle"))
    parser.add_argument("--sample-dir", type=pathlib.Path)
    parser.add_argument("--read-clients", type=int, nargs="*", default=[])
    parser.add_argument("--seconds", type=float, default=5.0)
    parser.add_argument("--campfire-workers", type=int, default=22)
    parser.add_argument("--rustfire-first", action="store_true")
    args = parser.parse_args()
    if any(value < 1 for value in args.read_clients) or args.seconds <= 0 or args.campfire_workers < 1:
        parser.error("positive client counts, seconds, and worker count are required")
    repository = args.campfire_repo.resolve()
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repository, text=True).strip()
    assert revision == "91d294f4a09f9bbe37f9548959bfcb43645678fb", revision
    with tempfile.TemporaryDirectory(prefix="paired-room-shell-") as directory:
        temp = pathlib.Path(directory)
        rust_db, camp_db = temp / "rustfire.sqlite3", temp / "campfire.sqlite3"
        rust_port, camp_port = free_port(), free_port()
        seed_rustfire(rust_db, rust_port, [[2]])
        env = seed_campfire(repository, args.ruby, args.bundle_path, repository / "storage/db/production.sqlite3", camp_db, [[2]], camp_port, temp)
        env["WEB_CONCURRENCY"] = str(args.campfire_workers if args.read_clients else 1)
        with sqlite3.connect(camp_db) as camp, sqlite3.connect(rust_db) as rust:
            account = camp.execute("SELECT name,join_code,updated_at FROM accounts WHERE id=1").fetchone()
            room = camp.execute("SELECT name,created_at,updated_at FROM rooms WHERE id=1").fetchone()
            user = camp.execute("SELECT name,updated_at FROM users WHERE id=1").fetchone()
            rust.execute("UPDATE accounts SET name=?1,join_code=?2,updated_at=?3 WHERE id=1", account)
            rust.execute("UPDATE rooms SET name=?1,created_at=?2,updated_at=?3 WHERE id=1", room)
            rust.execute("UPDATE users SET name=?1,updated_at=?2 WHERE id=1", user)
            camp.execute("UPDATE users SET name='User 2' WHERE id=2")
            camp.execute("DELETE FROM sqlite_sequence WHERE name='messages'")
            after_original = "2027-01-01 00:00:00.000000"
            for db in (camp, rust):
                db.execute("UPDATE rooms SET created_at=?1,updated_at=?1 WHERE id=2", [after_original])
                db.execute("INSERT INTO rooms(id,name,type,creator_id,created_at,updated_at) VALUES(3,'Private check','Rooms::Closed',1,?1,?1)", [after_original])
            camp.executemany("INSERT INTO memberships(room_id,user_id,involvement,created_at,updated_at) VALUES(3,?1,'mentions',?2,?2)", [(1, after_original), (2, after_original)])
            rust.executemany("INSERT INTO memberships(room_id,user_id,involvement,created_at) VALUES(3,?1,'mentions',?2)", [(1, after_original), (2, after_original)])
        rust = start_server(rust_db, rust_port, {"RUSTFIRE_CAMPFIRE_SECRET_KEY_BASE": env["SECRET_KEY_BASE"], "RUSTFIRE_UPLOAD_DIR": str(temp / "uploads")})
        with open(temp / "puma.log", "w+") as log:
            camp = subprocess.Popen([str(args.ruby), str(args.ruby.parent / "bundle"), "exec", "puma", "-C", "config/puma.rb"], cwd=repository, env=env, stdout=log, stderr=log)
            try:
                wait_for_server(camp_port, camp)
                camp_cookie, camp_csrf = login_campfire(camp_port)
                for label, path in (("original", "/rooms/1"), ("direct", "/rooms/2"), ("private", "/rooms/3")):
                    _, target = compare_room(label, path, camp_port, camp_cookie, rust_port, "session_token=benchmark-session", args.sample_dir)
                    assert 'class="sidebar admin"' in target.decode()
                post_message(camp_port, camp_cookie, camp_csrf)
                post_message(rust_port, "session_token=benchmark-session", "benchmark-csrf")
                compare_room("original with message", "/rooms/1", camp_port, camp_cookie, rust_port, "session_token=benchmark-session", args.sample_dir, normalize_times=True)
                jpeg = (repository / "test/fixtures/files/moon.jpg").read_bytes()
                post_account_logo(camp_port, camp_cookie, camp_csrf, jpeg)
                post_account_logo(rust_port, "session_token=benchmark-session", "benchmark-csrf", jpeg)
                logo_source, logo_page = compare_room("original with logo", "/rooms/1", camp_port, camp_cookie, rust_port, "session_token=benchmark-session", args.sample_dir, normalize_times=True)
                for page in (logo_source, logo_page):
                    assert 'class="sidebar admin account-has-logo"' in page.decode()
                if args.read_clients:
                    binary = temp / "checked_get"
                    subprocess.run(["go", "build", "-o", str(binary), "bench/checked_get.go"], check=True)
                    applications = [("Rustfire", rust_port, "session_token=benchmark-session"), ("Campfire", camp_port, camp_cookie)]
                    if not args.rustfire_first:
                        applications.reverse()
                    for clients in args.read_clients:
                        for name, port, cookie in applications:
                            report = measure_room_page(binary, port, cookie, clients, args.seconds)
                            print(f"{name} {clients} clients: {report['rps']:.1f} rps, p95 {report['p95_ms']:.2f} ms, {report['errors']} errors")
                print("PASS room shell across original, direct, and private rooms")
            finally:
                stop_server(rust)
                stop_server(camp)
                cleanup_campfire_uploads(repository / "storage/db/production.sqlite3", camp_db, repository)


if __name__ == "__main__":
    main()
