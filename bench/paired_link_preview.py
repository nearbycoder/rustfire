"""Compare rendered Open Graph messages and optionally benchmark preview-heavy reads."""

import argparse
import html
import html.parser
import http.client
import json
import pathlib
import sqlite3
import subprocess
import tempfile
import urllib.parse

from direct_lookup import free_port, start_server, stop_server
from message_markup import MessageMarkup
from paired_banned_content import BUNDLE, REPOSITORY, REVISION, RUBY, start_redis
from paired_direct_lookup import login_campfire, seed_campfire, seed_rustfire, wait_for_server
from paired_message_cache import fetch


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


class PreviewPageMarkup(MessageMarkup):
    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == "input" and values.get("name") == "authenticity_token":
            self.csrf_values.append(values.get("value"))
            return
        dynamic_times = {"datetime", "data-message-timestamp", "data-message-updated-at", "data-sort-value"}
        normalized = [(key, "<time>" if key in dynamic_times else value) for key, value in attrs]
        super().handle_starttag(tag, normalized)


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


def measure_reads(binary, port, cookie, clients, seconds):
    command = [str(binary), "--base", f"http://127.0.0.1:{port}", "--path", "/rooms/1/messages",
               "--cookie", cookie, "--expected-status", "200", "--expected-content-type", "text/html",
               "--expected-message-count", "40", "--accept", "text/html", "--clients", str(clients),
               "--seconds", str(seconds)]
    result = subprocess.run(command, text=True, capture_output=True, check=True)
    report = json.loads(result.stdout.strip().splitlines()[-1])
    assert report["errors"] == 0, report
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--read-clients", type=int, default=0, help="benchmark latest-40 preview page reads with this many clients")
    parser.add_argument("--seconds", type=float, default=10)
    parser.add_argument("--campfire-workers", type=int, default=22)
    parser.add_argument("--campfire-first", action="store_true", help="reverse the benchmark trial order")
    parser.add_argument("--sample-dir", type=pathlib.Path, help="retain the two latest-40 HTML pages")
    args = parser.parse_args()
    if args.read_clients < 0 or args.campfire_workers < 1 or (args.read_clients and args.seconds <= 0):
        parser.error("read clients and seconds must be positive")
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPOSITORY, text=True).strip() == REVISION
    with tempfile.TemporaryDirectory(prefix="paired-link-preview-") as scratch:
        temp = pathlib.Path(scratch)
        binary = temp / "checked_get"
        if args.read_clients:
            subprocess.run(["go", "build", "-o", str(binary), "bench/checked_get.go"], check=True)
        rust_db, camp_db = temp / "rust.sqlite3", temp / "camp.sqlite3"
        rust_port, camp_port, redis_port = free_port(), free_port(), free_port()
        seed_rustfire(rust_db, rust_port, [])
        camp_env = seed_campfire(REPOSITORY, RUBY, BUNDLE, REPOSITORY / "storage/db/production.sqlite3", camp_db, [], camp_port, temp)
        camp_env["REDIS_URL"] = f"redis://127.0.0.1:{redis_port}"
        camp_env["WEB_CONCURRENCY"] = str(args.campfire_workers if args.read_clients else 1)
        with sqlite3.connect(camp_db) as camp, sqlite3.connect(rust_db) as rust:
            camp.execute("DELETE FROM sqlite_sequence WHERE name='messages'")
            rust.executemany("UPDATE users SET name=?2,updated_at=?3 WHERE id=?1", camp.execute("SELECT id,name,updated_at FROM users WHERE id IN(1,2)").fetchall())
            rust.execute("UPDATE rooms SET name=? WHERE id=1", [camp.execute("SELECT name FROM rooms WHERE id=1").fetchone()[0]])
        redis, redis_log = start_redis(temp, redis_port)
        try:
            def run_rustfire():
                rust = start_server(rust_db, rust_port, {"RUSTFIRE_CAMPFIRE_SECRET_KEY_BASE": camp_env["SECRET_KEY_BASE"]})
                try:
                    cookie, csrf = "session_token=benchmark-session", "benchmark-csrf"
                    previews = [post(rust_port, cookie, csrf, case) for case in CASES]
                    page, report = None, None
                    if args.read_clients:
                        for index in range(40):
                            url = f"https://example.com/page/{index}"
                            post(rust_port, cookie, csrf, (f"bench-{index}", url, url, "https://example.com/image.png"))
                        page = fetch(rust_port, cookie, "/rooms/1/messages")[2]
                        report = measure_reads(binary, rust_port, cookie, args.read_clients, args.seconds)
                    return previews, page, report
                finally:
                    stop_server(rust)

            def run_campfire():
                with open(temp / "puma.log", "w+") as log:
                    camp = subprocess.Popen([str(RUBY), str(RUBY.parent / "bundle"), "exec", "puma", "-C", "config/puma.rb"], cwd=REPOSITORY, env=camp_env, stdout=log, stderr=log)
                    try:
                        wait_for_server(camp_port, camp)
                        cookie, csrf = login_campfire(camp_port)
                        previews = [post(camp_port, cookie, csrf, case) for case in CASES]
                        page, report = None, None
                        if args.read_clients:
                            for index in range(40):
                                url = f"https://example.com/page/{index}"
                                post(camp_port, cookie, csrf, (f"bench-{index}", url, url, "https://example.com/image.png"))
                            page = fetch(camp_port, cookie, "/rooms/1/messages")[2]
                            report = measure_reads(binary, camp_port, cookie, args.read_clients, args.seconds)
                        return previews, page, report
                    finally:
                        stop_server(camp)

            if args.campfire_first:
                (camp_previews, camp_page, camp_report), (rust_previews, rust_page, rust_report) = run_campfire(), run_rustfire()
            else:
                (rust_previews, rust_page, rust_report), (camp_previews, camp_page, camp_report) = run_rustfire(), run_campfire()
        finally:
            redis.terminate()
            redis.wait(timeout=10)
            redis_log.close()
    for case, rust_preview, camp_preview in zip(CASES, rust_previews, camp_previews):
        assert rust_preview == camp_preview, (case[0], rust_preview, camp_preview)
    print("PASS parsed preview messages, preview-only attachment, solo URLs, tweet URLs, and Twitter avatar layout match pinned Campfire")
    if args.read_clients:
        if args.sample_dir:
            args.sample_dir.mkdir(parents=True, exist_ok=True)
            (args.sample_dir / "rustfire-preview-page.html").write_bytes(rust_page)
            (args.sample_dir / "campfire-preview-page.html").write_bytes(camp_page)
        for index in range(40):
            client_id = f"paired-link-preview-bench-{index}"
            rust, camp = Presentation(client_id), Presentation(client_id)
            rust.feed(rust_page.decode())
            camp.feed(camp_page.decode())
            assert rust.structure and rust.structure == camp.structure, (index, rust.structure, camp.structure)
        rust_markup, camp_markup = PreviewPageMarkup(), PreviewPageMarkup()
        rust_markup.feed(rust_page.decode())
        camp_markup.feed(camp_page.decode())
        assert len(rust_markup.csrf_values) == 40 * 8
        assert all(rust_markup.csrf_values) and all(camp_markup.csrf_values)
        assert rust_markup.events == camp_markup.events, next(
            ((index, left, right) for index, (left, right) in enumerate(zip(rust_markup.events, camp_markup.events)) if left != right),
            (len(rust_markup.events), len(camp_markup.events)),
        )
        print(json.dumps({"clients": args.read_clients, "seconds": args.seconds, "campfire_workers": args.campfire_workers, "campfire_first": args.campfire_first,
                          "parsed_page_events": len(rust_markup.events), "campfire_cached_csrf_inputs": len(camp_markup.csrf_values),
                          "rustfire": {"reads": rust_report, "page_bytes": len(rust_page)},
                          "campfire": {"reads": camp_report, "page_bytes": len(camp_page)}}, sort_keys=True))


if __name__ == "__main__":
    main()
