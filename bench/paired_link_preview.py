"""Compare rendered Open Graph messages from identical Campfire and Rustfire posts."""

import html
import html.parser
import http.client
import pathlib
import sqlite3
import subprocess
import tempfile
import urllib.parse

from direct_lookup import free_port, start_server, stop_server
from paired_banned_content import BUNDLE, REPOSITORY, REVISION, RUBY, start_redis
from paired_direct_lookup import login_campfire, seed_campfire, seed_rustfire, wait_for_server


CASES = [
    ("with-text", "Link ", "https://example.com/page", "https://example.com/image.png"),
    ("preview-only", "", "https://example.com/page", "https://example.com/image.png"),
    ("solo-url", "https://example.com/page", "https://example.com/page", "https://example.com/image.png"),
    ("solo-tweet", "https://x.com/37signals/status/123?s=20", "https://twitter.com/37signals/status/123", "https://example.com/image.png"),
    ("twitter-avatar", "Look ", "https://twitter.com/37signals/status/123", "https://pbs.twimg.com/profile_images/example/avatar.png"),
]


class Presentation(html.parser.HTMLParser):
    def __init__(self, client_id):
        super().__init__()
        self.client_id = client_id
        self.depth = 0
        self.structure = []

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "div" and attributes.get("id") == f"presentation_message_{self.client_id}":
            self.depth = 1
        elif self.depth and tag not in {"img", "br", "hr", "input"}:
            self.depth += 1
        if self.depth:
            self.structure.append((tag, tuple(sorted(attrs))))

    def handle_startendtag(self, tag, attrs):
        if self.depth:
            self.structure.append((tag, tuple(sorted(attrs))))

    def handle_endtag(self, tag):
        if self.depth:
            self.depth -= 1

    def handle_data(self, data):
        if self.depth and data.strip():
            self.structure.append(data.strip())


def post(port, cookie, csrf, case):
    name, prefix, href, image = case
    client_id = f"paired-link-preview-{name}"
    attachment = f'<action-text-attachment content-type="application/vnd.actiontext.opengraph-embed" href="{html.escape(href, quote=True)}" url="{html.escape(image, quote=True)}" filename="Example title" caption="Example description"></action-text-attachment>'
    message = f"<div>{html.escape(prefix)}{attachment}</div>"
    fields = urllib.parse.urlencode({
        "message[body]": message,
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
        body = response.read()
        assert response.status == 200, (response.status, body[:300])
        preview = Presentation(client_id)
        preview.feed(body.decode())
        assert preview.structure, body[:300]
        return preview.structure
    finally:
        connection.close()


def main():
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPOSITORY, text=True).strip() == REVISION
    with tempfile.TemporaryDirectory(prefix="paired-link-preview-") as scratch:
        temp = pathlib.Path(scratch)
        rust_db, camp_db = temp / "rust.sqlite3", temp / "camp.sqlite3"
        rust_port, camp_port, redis_port = free_port(), free_port(), free_port()
        seed_rustfire(rust_db, rust_port, [])
        camp_env = seed_campfire(REPOSITORY, RUBY, BUNDLE, REPOSITORY / "storage/db/production.sqlite3", camp_db, [], camp_port, temp)
        camp_env["REDIS_URL"] = f"redis://127.0.0.1:{redis_port}"
        camp_env["WEB_CONCURRENCY"] = "1"
        with sqlite3.connect(camp_db) as camp, sqlite3.connect(rust_db) as rust:
            rust.executemany("UPDATE users SET name=?2,updated_at=?3 WHERE id=?1", camp.execute("SELECT id,name,updated_at FROM users WHERE id IN(1,2)").fetchall())
            rust.execute("UPDATE rooms SET name=? WHERE id=1", [camp.execute("SELECT name FROM rooms WHERE id=1").fetchone()[0]])
        redis, redis_log = start_redis(temp, redis_port)
        try:
            rust = start_server(rust_db, rust_port, {"RUSTFIRE_CAMPFIRE_SECRET_KEY_BASE": camp_env["SECRET_KEY_BASE"]})
            try:
                rust_previews = [post(rust_port, "session_token=benchmark-session", "benchmark-csrf", case) for case in CASES]
            finally:
                stop_server(rust)
            with open(temp / "puma.log", "w+") as log:
                camp = subprocess.Popen([str(RUBY), str(RUBY.parent / "bundle"), "exec", "puma", "-C", "config/puma.rb"], cwd=REPOSITORY, env=camp_env, stdout=log, stderr=log)
                try:
                    wait_for_server(camp_port, camp)
                    cookie, csrf = login_campfire(camp_port)
                    camp_previews = [post(camp_port, cookie, csrf, case) for case in CASES]
                finally:
                    stop_server(camp)
        finally:
            redis.terminate()
            redis.wait(timeout=10)
            redis_log.close()
    for case, rust_preview, camp_preview in zip(CASES, rust_previews, camp_previews):
        assert rust_preview == camp_preview, (case[0], rust_preview, camp_preview)
    print("PASS parsed preview messages, preview-only attachment, solo URLs, tweet URLs, and Twitter avatar layout match pinned Campfire")


if __name__ == "__main__":
    main()
