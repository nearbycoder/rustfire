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
import pathlib
import re
import sqlite3
import subprocess
import tempfile

from direct_lookup import free_port, start_server, stop_server
from paired_direct_lookup import login_campfire, seed_campfire, seed_rustfire, wait_for_server


VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"


class Section(HTMLParser):
    def __init__(self, target):
        super().__init__(convert_charrefs=True)
        self.target = target
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
                elif value:
                    value = re.sub(r"http://127\.0\.0\.1:\d+", "<origin>", value)
                    if key in {"href", "data-lightbox-url-value"} and value.startswith("/qr_code/"):
                        value = "<origin-specific-qr>"
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


def section(page, target):
    parser = Section(target)
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


def get_room(port, cookie):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    try:
        connection.request("GET", "/rooms/1", headers={"Cookie": cookie, "User-Agent": AGENT, "Accept": "text/html"})
        response = connection.getresponse()
        body = response.read()
        assert response.status == 200, (response.status, body[:200])
        return body
    finally:
        connection.close()


def assert_equal(label, expected, actual):
    for index, (left, right) in enumerate(zip(expected, actual)):
        assert left == right, (label, index, left, right)
    assert len(expected) == len(actual), (label, len(expected), len(actual))
    print(f"{label}: {len(actual)} matching parsed tokens")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--campfire-repo", type=pathlib.Path, default=pathlib.Path("/tmp/once-campfire-reference"))
    parser.add_argument("--ruby", type=pathlib.Path, default=pathlib.Path("/tmp/rustfire-baseline/local/bin/ruby"))
    parser.add_argument("--bundle-path", type=pathlib.Path, default=pathlib.Path("/tmp/rustfire-baseline/bundle"))
    parser.add_argument("--sample-dir", type=pathlib.Path)
    args = parser.parse_args()
    repository = args.campfire_repo.resolve()
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repository, text=True).strip()
    assert revision == "91d294f4a09f9bbe37f9548959bfcb43645678fb", revision
    with tempfile.TemporaryDirectory(prefix="paired-room-shell-") as directory:
        temp = pathlib.Path(directory)
        rust_db, camp_db = temp / "rustfire.sqlite3", temp / "campfire.sqlite3"
        rust_port, camp_port = free_port(), free_port()
        seed_rustfire(rust_db, rust_port, [])
        env = seed_campfire(repository, args.ruby, args.bundle_path, repository / "storage/db/production.sqlite3", camp_db, [], camp_port, temp)
        with sqlite3.connect(camp_db) as camp, sqlite3.connect(rust_db) as rust:
            account = camp.execute("SELECT name,join_code,updated_at FROM accounts WHERE id=1").fetchone()
            room = camp.execute("SELECT name,updated_at FROM rooms WHERE id=1").fetchone()
            user = camp.execute("SELECT name,updated_at FROM users WHERE id=1").fetchone()
            rust.execute("UPDATE accounts SET name=?1,join_code=?2,updated_at=?3 WHERE id=1", account)
            rust.execute("UPDATE rooms SET name=?1,updated_at=?2 WHERE id=1", room)
            rust.execute("UPDATE users SET name=?1,updated_at=?2 WHERE id=1", user)
        rust = start_server(rust_db, rust_port, {"RUSTFIRE_CAMPFIRE_SECRET_KEY_BASE": env["SECRET_KEY_BASE"]})
        with open(temp / "puma.log", "w+") as log:
            camp = subprocess.Popen([str(args.ruby), str(args.ruby.parent / "bundle"), "exec", "puma", "-C", "config/puma.rb"], cwd=repository, env=env, stdout=log, stderr=log)
            try:
                wait_for_server(camp_port, camp)
                camp_cookie, _ = login_campfire(camp_port)
                source = get_room(camp_port, camp_cookie)
                target = get_room(rust_port, "session_token=benchmark-session")
                if args.sample_dir:
                    args.sample_dir.mkdir(parents=True, exist_ok=True)
                    (args.sample_dir / "campfire-room.html").write_bytes(source)
                    (args.sample_dir / "rustfire-room.html").write_bytes(target)
                for label in ("nav", "footer", "sidebar"):
                    assert_equal(label, section(source, label), section(target, label))
                left, right = section(source, "message-area"), section(target, "message-area")
                assert_equal("message-area", left, right)
                assert_equal("message template", message_template(source), message_template(target))
                assert 'class="sidebar admin"' in target.decode()
                assert 'name="current-room-id" content="1"' in target.decode()
                print("PASS room shell landmarks and sampled source sections")
            finally:
                stop_server(rust)
                stop_server(camp)


if __name__ == "__main__":
    main()
