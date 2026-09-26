"""Compare the room page shell against pinned Campfire on disposable fixtures.

The probe deliberately covers the navigation, composer footer, initial sidebar
frame, rendered message area, and parsed optimistic-message template. It does
not claim full page or browser-behavior parity.
Run after ``cargo build --release``.
"""

import argparse
import base64
import hashlib
import html
import http.client
from html.parser import HTMLParser
import hmac
import json
import pathlib
import re
import sqlite3
import subprocess
import tempfile
import urllib.parse
import urllib.request

from direct_lookup import free_port, start_server, stop_server
from paired_account_logo import multipart
from paired_bot_admin import cleanup_campfire_uploads, request
from paired_direct_lookup import login_campfire, seed_campfire, seed_rustfire, wait_for_server


VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"


class Section(HTMLParser):
    def __init__(self, target, normalize_times=False, ignore_csrf_inputs=False, normalize_blob_paths=False):
        super().__init__(convert_charrefs=True)
        self.target = target
        self.normalize_times = normalize_times
        self.ignore_csrf_inputs = ignore_csrf_inputs
        self.normalize_blob_paths = normalize_blob_paths
        self.depth = 0
        self.tokens = []

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if self.depth == 0 and values.get("id") == self.target:
            self.depth = 1
        elif self.depth and tag not in VOID:
            self.depth += 1
        if self.depth:
            if self.ignore_csrf_inputs and tag == "input" and values.get("name") == "authenticity_token":
                return
            normalized = []
            for key, value in attrs:
                if key == "value" and values.get("name") == "authenticity_token":
                    value = "<csrf>"
                elif self.normalize_times and key in {"data-refresh-room-loaded-at-value", "data-message-timestamp", "data-message-updated-at", "data-sort-value", "datetime"}:
                    assert value, (key, value)
                    value = "<generated-time>"
                elif value:
                    value = re.sub(r"http://127\.0\.0\.1(?::\d+)?", "<origin>", value)
                    if self.normalize_blob_paths:
                        value = re.sub(r"(/rails/active_storage/(?:blobs|representations)/redirect/)[A-Za-z0-9_-]+=*--[a-f0-9]{40}(?=/)", r"\1<signed-blob>", value)
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


def section(page, target, normalize_times=False, ignore_csrf_inputs=False, normalize_blob_paths=False):
    parser = Section(target, normalize_times, ignore_csrf_inputs, normalize_blob_paths)
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


class HeadMeta(HTMLParser):
    def __init__(self):
        super().__init__()
        self.in_head = False
        self.values = {}
        self.links = {}

    def handle_starttag(self, tag, attrs):
        if tag == "head":
            self.in_head = True
        elif self.in_head and tag == "meta":
            values = dict(attrs)
            if "name" in values:
                self.values[values["name"]] = values.get("content")
        elif self.in_head and tag == "link":
            values = dict(attrs)
            if values.get("rel") in {"icon", "apple-touch-icon"}:
                self.links[values["rel"]] = values.get("href")

    def handle_endtag(self, tag):
        if tag == "head":
            self.in_head = False


def assert_head_runtime_metadata(source, target):
    pages = []
    for page in (source, target):
        parser = HeadMeta()
        parser.feed(page.decode())
        pages.append(parser)
    original, rustfire = pages
    for name in ("csrf-param", "action-cable-url", "turbo-prefetch", "current-user-id", "current-user-name"):
        assert name in original.values and original.values[name] == rustfire.values.get(name), (name, original.values.get(name), rustfire.values.get(name))
    assert original.values.get("csrf-token") and rustfire.values.get("csrf-token"), "Missing CSRF token metadata"
    assert set(original.links) == set(rustfire.links) == {"icon", "apple-touch-icon"}, (original.links, rustfire.links)
    for name in original.links:
        source_link, target_link = original.links[name], rustfire.links[name]
        if source_link.startswith("/account/logo?v="):
            assert re.fullmatch(r"/account/logo\?v=\d+", source_link), source_link
            assert re.fullmatch(r"/account/logo\?v=\d+", target_link), target_link
        else:
            assert source_link == target_link, (name, source_link, target_link)


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


def raw_get(port, path, cookie, extra_headers=None):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    headers = {"Cookie": cookie, **(extra_headers or {})}
    try:
        connection.request("GET", path, headers=headers)
        response = connection.getresponse()
        return response.status, {key.lower(): value for key, value in response.getheaders()}, response.read()
    finally:
        connection.close()


def representation_routes(page, port, cookie, filename, expected):
    match = re.search(rb"/rails/active_storage/representations/redirect/[^'\" ]+/" + filename.encode(), page)
    assert match, ("representation URL", filename)
    redirect_path = match.group().decode()
    proxy_path = redirect_path.replace("/representations/redirect/", "/representations/proxy/", 1)
    redirect_status, redirect_headers, _ = raw_get(port, redirect_path, cookie)
    proxy_status, proxy_headers, proxy_body = raw_get(port, proxy_path, cookie)
    assert proxy_body == expected, (filename, "proxy bytes", proxy_status, len(proxy_body), len(expected))
    disk_path = urllib.parse.urlsplit(redirect_headers.get("location", "")).path
    assert redirect_status == 302 and disk_path.startswith("/rails/active_storage/disk/"), (filename, redirect_status, disk_path)
    disk_status, _, disk_body = raw_get(port, disk_path, cookie)
    assert (disk_status, disk_body) == (200, expected), (filename, "disk bytes", disk_status, len(disk_body))
    legacy_path = redirect_path.replace("/representations/redirect/", "/representations/", 1)
    legacy_status, legacy_headers, _ = raw_get(port, legacy_path, cookie)
    assert legacy_status == 302, (filename, "legacy redirect", legacy_status)
    legacy_disk_path = urllib.parse.urlsplit(legacy_headers.get("location", "")).path
    legacy_disk_status, _, legacy_disk_body = raw_get(port, legacy_disk_path, cookie)
    assert (legacy_disk_status, legacy_disk_body) == (200, expected), (filename, "legacy disk bytes", legacy_disk_status)
    assert proxy_headers.get("etag") and proxy_headers.get("last-modified"), (filename, proxy_headers)
    expected_etag = f'W/"{hashlib.sha256(proxy_path.encode()).hexdigest()[:32]}"'
    assert proxy_headers["etag"] == expected_etag, (filename, proxy_headers["etag"], expected_etag)
    etag_status, _, etag_body = raw_get(port, proxy_path, cookie, {"If-None-Match": proxy_headers["etag"]})
    date_status, _, date_body = raw_get(port, proxy_path, cookie, {"If-Modified-Since": proxy_headers["last-modified"]})
    assert (etag_status, etag_body, date_status, date_body) == (304, b"", 304, b""), filename
    range_status, range_headers, range_body = raw_get(port, proxy_path, cookie, {"Range": "bytes=0-5"})
    assert range_body in (expected, expected[:6]), (filename, "range", range_status, len(range_body))
    attachment_status, attachment_headers, attachment_body = raw_get(port, proxy_path + "?disposition=attachment", cookie)
    assert (attachment_status, attachment_body) == (200, expected), (filename, "attachment", attachment_status)
    assert attachment_headers.get("content-disposition", "").startswith("attachment;"), attachment_headers
    return redirect_status, proxy_status, {
        key: proxy_headers.get(key) for key in ("content-type", "content-disposition", "cache-control", "last-modified")
    }, range_status, range_headers.get("content-range"), attachment_headers.get("content-disposition")


def original_blob_routes(page, port, cookie, filename, expected):
    match = re.search(rb"/rails/active_storage/blobs/redirect/[^'\" ]+/" + re.escape(filename.encode()), page)
    assert match, ("original blob URL", filename)
    redirect_path = match.group().decode()
    proxy_path = redirect_path.replace("/blobs/redirect/", "/blobs/proxy/", 1)
    redirect_status, redirect_headers, _ = raw_get(port, redirect_path, cookie)
    disk_path = urllib.parse.urlsplit(redirect_headers.get("location", "")).path
    assert redirect_status == 302 and disk_path.startswith("/rails/active_storage/disk/"), (filename, redirect_status, disk_path)
    disk_status, disk_headers, disk_body = raw_get(port, disk_path, cookie)
    assert (disk_status, disk_body) == (200, expected), (filename, "disk bytes", disk_status, len(disk_body))
    proxy_status, proxy_headers, proxy_body = raw_get(port, proxy_path, cookie)
    assert (proxy_status, proxy_body) == (200, expected), (filename, "proxy bytes", proxy_status, len(proxy_body))
    return redirect_status, disk_status, proxy_status, tuple((key, proxy_headers.get(key)) for key in (
        "content-type", "content-disposition", "cache-control", "last-modified")), tuple(
            (key, disk_headers.get(key)) for key in ("content-type", "content-disposition"))


def post_message(port, cookie, csrf, body="Room page message check", client_id="room-page-1"):
    payload = urllib.parse.urlencode({
        "message[body]": body,
        "message[client_message_id]": client_id,
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


def post_file_message(port, cookie, csrf, filename, content_type, contents, client_id):
    boundary = "rustfire-room-page-file"
    payload = (
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"authenticity_token\"\r\n\r\n{csrf}\r\n"
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"message[client_message_id]\"\r\n\r\n{client_id}\r\n"
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"message[attachment]\"; filename=\"{filename}\"\r\nContent-Type: {content_type}\r\n\r\n"
    ).encode() + contents + f"\r\n--{boundary}--\r\n".encode()
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    try:
        connection.request("POST", "/rooms/1/messages", payload, {
            "Cookie": cookie,
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Accept": "text/vnd.turbo-stream.html, text/html",
            "User-Agent": AGENT,
        })
        response = connection.getresponse()
        body = response.read()
        assert response.status == 200 and response.getheader("Content-Type", "").startswith("text/vnd.turbo-stream.html"), (response.status, body[:200])
    finally:
        connection.close()


def minimal_pdf():
    parts = [b"%PDF-1.4\n"]
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 72 72] /Contents 4 0 R /Resources << >> >>",
        b"<< /Length 0 >>\nstream\n\nendstream",
    ]
    offsets = []
    for index, value in enumerate(objects, 1):
        offsets.append(sum(map(len, parts)))
        parts.append(f"{index} 0 obj\n".encode() + value + b"\nendobj\n")
    xref = sum(map(len, parts))
    parts.append(b"xref\n0 5\n0000000000 65535 f \n" + b"".join(f"{offset:010d} 00000 n \n".encode() for offset in offsets))
    parts.append(f"trailer\n<< /Size 5 /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    return b"".join(parts)


def post_account_logo(port, cookie, csrf, jpeg):
    payload, content_type = multipart(jpeg)
    status, location, body = request(port, "PATCH", "/account", cookie, csrf, payload, content_type)
    assert status in {302, 303} and location, (status, body[:200])


def measure_room_page(binary, port, cookie, clients, seconds, messages=1):
    result = subprocess.run([
        str(binary), "--base", f"http://127.0.0.1:{port}", "--path", "/rooms/1",
        "--cookie", cookie, "--accept", "text/html", "--expected-status", "200",
        "--expected-content-type", "text/html", "--expected-message-count", str(messages),
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


def compare_room(label, path, camp_port, camp_cookie, rust_port, rust_cookie, sample_dir, normalize_times=False, ignore_csrf_inputs=False, normalize_blob_paths=False):
    source = get_room(camp_port, camp_cookie, path)
    target = get_room(rust_port, rust_cookie, path)
    assert_head_runtime_metadata(source, target)
    if sample_dir:
        sample_dir.mkdir(parents=True, exist_ok=True)
        stem = label.replace(" ", "-")
        (sample_dir / f"campfire-{stem}.html").write_bytes(source)
        (sample_dir / f"rustfire-{stem}.html").write_bytes(target)
    for part in ("nav", "footer", "sidebar", "message-area"):
        assert_equal(f"{label} {part}", section(source, part, normalize_times, ignore_csrf_inputs, normalize_blob_paths), section(target, part, normalize_times, ignore_csrf_inputs, normalize_blob_paths))
    assert_equal(f"{label} message template", message_template(source), message_template(target))
    assert f'name="current-room-id" content="{path.rsplit("/", 1)[-1]}"' in target.decode()
    return source, target


def compare_message_page(message_id, action, camp_port, camp_cookie, rust_port, rust_cookie, sample_dir, ignore_csrf_inputs=False, normalize_blob_paths=False):
    path = f"/rooms/1/messages/{message_id}{action}"
    label = "edit" if action else "detail"
    source = get_room(camp_port, camp_cookie, path)
    target = get_room(rust_port, rust_cookie, path)
    if sample_dir:
        (sample_dir / f"campfire-message-{label}-{message_id}.html").write_bytes(source)
        (sample_dir / f"rustfire-message-{label}-{message_id}.html").write_bytes(target)
    for part in ("nav", "footer", "sidebar", "main-content"):
        assert_equal(f"message {message_id} {label} {part}", section(source, part, normalize_times=True, ignore_csrf_inputs=ignore_csrf_inputs, normalize_blob_paths=normalize_blob_paths), section(target, part, normalize_times=True, ignore_csrf_inputs=ignore_csrf_inputs, normalize_blob_paths=normalize_blob_paths))
    assert 'class="admin"' in target.decode()
    return source, target


def jpeg_representation(page, port, cookie, legacy_secret=None):
    match = re.search(rb"/rails/active_storage/representations/redirect/[^'\" ]+/moon\.jpg", page)
    assert match, "Missing signed JPEG representation"
    path = match.group().decode()
    if legacy_secret:
        key = hashlib.pbkdf2_hmac("sha256", legacy_secret.encode(), b"ActiveStorage", 1000, 64)
        payload = {"_rails": {"data": {"format": "jpeg", "resize_to_limit": [1200, 800]}, "pur": "variation"}}
        encoded = base64.b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode()
        token = f"{encoded}--{hmac.new(key, encoded.encode(), hashlib.sha1).hexdigest()}"
        parts = path.split("/")
        parts[-2] = token
        path = "/".join(parts)
    request = urllib.request.Request(f"http://127.0.0.1:{port}{path}", headers={"Cookie": cookie})
    with urllib.request.urlopen(request, timeout=15) as response:
        body = response.read()
        assert response.status == 200 and response.headers.get_content_type() == "image/jpeg", (response.status, response.headers)
        assert body.startswith(b"\xff\xd8\xff") and body.endswith(b"\xff\xd9"), body[:10]
        return body


def video_poster(page, port, cookie):
    match = re.search(rb"<video\b[^>]*poster=['\"]([^'\"]+)['\"]", page)
    assert match and match.group(1).startswith(b"/rails/active_storage/representations/redirect/"), "Missing signed WebP poster"
    request = urllib.request.Request(f"http://127.0.0.1:{port}{match.group(1).decode()}", headers={"Cookie": cookie})
    with urllib.request.urlopen(request, timeout=15) as response:
        body = response.read()
        assert response.status == 200 and response.headers.get_content_type() == "image/webp", (response.status, response.headers)
        assert body.startswith(b"RIFF") and body[8:12] == b"WEBP", body[:12]
        return body


def pdf_preview(page, port, cookie):
    match = re.search(rb"/rails/active_storage/representations/redirect/[^'\" ]+/page\.pdf", page)
    assert match, "Missing signed PDF preview"
    request = urllib.request.Request(f"http://127.0.0.1:{port}{match.group().decode()}", headers={"Cookie": cookie})
    with urllib.request.urlopen(request, timeout=15) as response:
        body = response.read()
        assert response.status == 200 and response.headers.get_content_type() == "image/png", (response.status, response.headers)
        assert body.startswith(b"\x89PNG\r\n\x1a\n"), body[:12]
        return body


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--campfire-repo", type=pathlib.Path, default=pathlib.Path("/tmp/once-campfire-reference"))
    parser.add_argument("--ruby", type=pathlib.Path, default=pathlib.Path("/tmp/rustfire-baseline/local/bin/ruby"))
    parser.add_argument("--bundle-path", type=pathlib.Path, default=pathlib.Path("/tmp/rustfire-baseline/bundle"))
    parser.add_argument("--sample-dir", type=pathlib.Path)
    parser.add_argument("--read-clients", type=int, nargs="*", default=[])
    parser.add_argument("--read-media-clients", type=int, nargs="*", default=[])
    parser.add_argument("--seconds", type=float, default=5.0)
    parser.add_argument("--campfire-workers", type=int, default=22)
    parser.add_argument("--rustfire-first", action="store_true")
    args = parser.parse_args()
    if any(value < 1 for value in args.read_clients + args.read_media_clients) or args.seconds <= 0 or args.campfire_workers < 1:
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
        env["WEB_CONCURRENCY"] = str(args.campfire_workers if args.read_clients or args.read_media_clients else 1)
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
                compare_message_page(1, "", camp_port, camp_cookie, rust_port, "session_token=benchmark-session", args.sample_dir)
                compare_message_page(1, "/edit", camp_port, camp_cookie, rust_port, "session_token=benchmark-session", args.sample_dir)
                jpeg = (repository / "test/fixtures/files/moon.jpg").read_bytes()
                post_account_logo(camp_port, camp_cookie, camp_csrf, jpeg)
                post_account_logo(rust_port, "session_token=benchmark-session", "benchmark-csrf", jpeg)
                logo_source, logo_page = compare_room("original with logo", "/rooms/1", camp_port, camp_cookie, rust_port, "session_token=benchmark-session", args.sample_dir, normalize_times=True)
                for page in (logo_source, logo_page):
                    assert 'class="sidebar admin account-has-logo"' in page.decode()
                if args.read_clients or args.read_media_clients:
                    binary = temp / "checked_get"
                    subprocess.run(["go", "build", "-o", str(binary), "bench/checked_get.go"], check=True)
                    applications = [("Rustfire", rust_port, "session_token=benchmark-session"), ("Campfire", camp_port, camp_cookie)]
                    if not args.rustfire_first:
                        applications.reverse()
                if args.read_clients:
                    for clients in args.read_clients:
                        for name, port, cookie in applications:
                            report = measure_room_page(binary, port, cookie, clients, args.seconds)
                            print(f"{name} one-message room {clients} clients: {report['rps']:.1f} rps, p95 {report['p95_ms']:.2f} ms, {report['errors']} errors")
                rich_body = "<div>Hi <strong>bold</strong><br>next</div>"
                post_message(camp_port, camp_cookie, camp_csrf, rich_body, "room-page-rich")
                post_message(rust_port, "session_token=benchmark-session", "benchmark-csrf", rich_body, "room-page-rich")
                compare_message_page(2, "", camp_port, camp_cookie, rust_port, "session_token=benchmark-session", args.sample_dir)
                compare_message_page(2, "/edit", camp_port, camp_cookie, rust_port, "session_token=benchmark-session", args.sample_dir)
                for port, cookie, csrf in ((camp_port, camp_cookie, camp_csrf), (rust_port, "session_token=benchmark-session", "benchmark-csrf")):
                    post_file_message(port, cookie, csrf, "note.txt", "text/plain", b"file contents", "room-page-file")
                source_file_page, target_file_page = compare_message_page(3, "", camp_port, camp_cookie, rust_port, "session_token=benchmark-session", args.sample_dir, ignore_csrf_inputs=True, normalize_blob_paths=True)
                source_file_routes = original_blob_routes(source_file_page, camp_port, camp_cookie, "note.txt", b"file contents")
                target_file_routes = original_blob_routes(target_file_page, rust_port, "session_token=benchmark-session", "note.txt", b"file contents")
                assert source_file_routes == target_file_routes, ("text blob routes", source_file_routes, target_file_routes)
                compare_message_page(3, "/edit", camp_port, camp_cookie, rust_port, "session_token=benchmark-session", args.sample_dir, normalize_blob_paths=True)
                for port, cookie, csrf in ((camp_port, camp_cookie, camp_csrf), (rust_port, "session_token=benchmark-session", "benchmark-csrf")):
                    post_file_message(port, cookie, csrf, "moon.jpg", "image/jpeg", jpeg, "room-page-image")
                source_image_page, target_image_page = compare_message_page(4, "", camp_port, camp_cookie, rust_port, "session_token=benchmark-session", args.sample_dir, ignore_csrf_inputs=True, normalize_blob_paths=True)
                source_jpeg = jpeg_representation(source_image_page, camp_port, camp_cookie)
                target_jpeg = jpeg_representation(target_image_page, rust_port, "session_token=benchmark-session")
                assert source_jpeg == target_jpeg, (len(source_jpeg), len(target_jpeg))
                source_image_routes = original_blob_routes(source_image_page, camp_port, camp_cookie, "moon.jpg", jpeg)
                target_image_routes = original_blob_routes(target_image_page, rust_port, "session_token=benchmark-session", "moon.jpg", jpeg)
                assert source_image_routes == target_image_routes, ("JPEG blob routes", source_image_routes, target_image_routes)
                assert jpeg_representation(target_image_page, rust_port, "session_token=benchmark-session", env["SECRET_KEY_BASE"]) == target_jpeg
                print(f"JPEG representations: {len(source_jpeg)} byte-identical bytes")
                source_route = representation_routes(source_image_page, camp_port, camp_cookie, "moon.jpg", source_jpeg)
                target_route = representation_routes(target_image_page, rust_port, "session_token=benchmark-session", "moon.jpg", target_jpeg)
                assert source_route == target_route, ("JPEG representation routes", source_route, target_route)
                compare_message_page(4, "/edit", camp_port, camp_cookie, rust_port, "session_token=benchmark-session", args.sample_dir, normalize_blob_paths=True)
                video_file = temp / "room-page-video.mp4"
                subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=c=red:s=16x16:r=5:d=1", "-c:v", "mpeg4", "-y", str(video_file)], check=True)
                for port, cookie, csrf in ((camp_port, camp_cookie, camp_csrf), (rust_port, "session_token=benchmark-session", "benchmark-csrf")):
                    post_file_message(port, cookie, csrf, "clip.mp4", "video/mp4", video_file.read_bytes(), "room-page-video")
                source_video_page, target_video_page = compare_message_page(5, "", camp_port, camp_cookie, rust_port, "session_token=benchmark-session", args.sample_dir, ignore_csrf_inputs=True, normalize_blob_paths=True)
                source_poster = video_poster(source_video_page, camp_port, camp_cookie)
                target_poster = video_poster(target_video_page, rust_port, "session_token=benchmark-session")
                assert source_poster == target_poster, (len(source_poster), len(target_poster))
                print(f"WebP posters: {len(source_poster)} byte-identical bytes")
                source_video_route = representation_routes(source_video_page, camp_port, camp_cookie, "clip.mp4", source_poster)
                target_video_route = representation_routes(target_video_page, rust_port, "session_token=benchmark-session", "clip.mp4", target_poster)
                assert source_video_route == target_video_route, ("video representation routes", source_video_route, target_video_route)
                compare_message_page(5, "/edit", camp_port, camp_cookie, rust_port, "session_token=benchmark-session", args.sample_dir, normalize_blob_paths=True)
                for port, cookie, csrf in ((camp_port, camp_cookie, camp_csrf), (rust_port, "session_token=benchmark-session", "benchmark-csrf")):
                    post_file_message(port, cookie, csrf, "page.pdf", "application/pdf", minimal_pdf(), "room-page-pdf")
                source_pdf_page, target_pdf_page = compare_message_page(6, "", camp_port, camp_cookie, rust_port, "session_token=benchmark-session", args.sample_dir, ignore_csrf_inputs=True, normalize_blob_paths=True)
                source_pdf_preview = pdf_preview(source_pdf_page, camp_port, camp_cookie)
                target_pdf_preview = pdf_preview(target_pdf_page, rust_port, "session_token=benchmark-session")
                assert source_pdf_preview == target_pdf_preview, (len(source_pdf_preview), len(target_pdf_preview))
                print(f"PDF PNG previews: {len(source_pdf_preview)} byte-identical bytes")
                source_pdf_route = representation_routes(source_pdf_page, camp_port, camp_cookie, "page.pdf", source_pdf_preview)
                target_pdf_route = representation_routes(target_pdf_page, rust_port, "session_token=benchmark-session", "page.pdf", target_pdf_preview)
                assert source_pdf_route == target_pdf_route, ("PDF representation routes", source_pdf_route, target_pdf_route)
                compare_message_page(6, "/edit", camp_port, camp_cookie, rust_port, "session_token=benchmark-session", args.sample_dir, normalize_blob_paths=True)
                compare_room("original with six mixed messages", "/rooms/1", camp_port, camp_cookie, rust_port, "session_token=benchmark-session", args.sample_dir, normalize_times=True, ignore_csrf_inputs=True, normalize_blob_paths=True)
                if args.read_media_clients:
                    for clients in args.read_media_clients:
                        for name, port, cookie in applications:
                            report = measure_room_page(binary, port, cookie, clients, args.seconds, messages=6)
                            print(f"{name} mixed-media room {clients} clients: {report['rps']:.1f} rps, p95 {report['p95_ms']:.2f} ms, {report['errors']} errors")
                for port, cookie, csrf in ((camp_port, camp_cookie, camp_csrf), (rust_port, "session_token=benchmark-session", "benchmark-csrf")):
                    post_file_message(port, cookie, csrf, "report:Q?.txt", "text/plain", b"filename check", "room-page-unsafe-filename")
                with sqlite3.connect(camp_db) as source_db, sqlite3.connect(rust_db) as target_db:
                    source_filename = source_db.execute("SELECT b.filename FROM active_storage_blobs b JOIN active_storage_attachments a ON a.blob_id=b.id WHERE a.record_type='Message' AND a.record_id=7 AND a.name='attachment'").fetchone()[0]
                    target_filename = target_db.execute("SELECT filename FROM attachments WHERE message_id=7").fetchone()[0]
                    assert (source_filename, target_filename) == ("report:Q?.txt", "report:Q?.txt"), (source_filename, target_filename)
                unsafe_source, unsafe_target = compare_message_page(7, "", camp_port, camp_cookie, rust_port, "session_token=benchmark-session", args.sample_dir, ignore_csrf_inputs=True, normalize_blob_paths=True)
                compare_message_page(7, "/edit", camp_port, camp_cookie, rust_port, "session_token=benchmark-session", args.sample_dir, normalize_blob_paths=True)
                compare_room("original with unsafe filename", "/rooms/1", camp_port, camp_cookie, rust_port, "session_token=benchmark-session", args.sample_dir, normalize_times=True, ignore_csrf_inputs=True, normalize_blob_paths=True)
                expected_name = "report-Q-.txt"
                source_unsafe_route = original_blob_routes(unsafe_source, camp_port, camp_cookie, expected_name, b"filename check")
                target_unsafe_route = original_blob_routes(unsafe_target, rust_port, "session_token=benchmark-session", expected_name, b"filename check")
                assert source_unsafe_route == target_unsafe_route, ("unsafe filename blob routes", source_unsafe_route, target_unsafe_route)
                print("PASS room shell across original, direct, and private rooms")
            finally:
                stop_server(rust)
                stop_server(camp)
                cleanup_campfire_uploads(repository / "storage/db/production.sqlite3", camp_db, repository)


if __name__ == "__main__":
    main()
