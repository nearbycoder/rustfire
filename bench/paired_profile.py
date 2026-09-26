"""Compare profile behavior and read throughput with pinned ONCE Campfire.

Run after ``cargo build --release`` with the pinned Ruby bundle and Redis available.
The disposable databases contain matched users and shared/direct memberships.
"""

import argparse
import concurrent.futures
import html
import http.client
import http.cookiejar
from html.parser import HTMLParser
import json
import math
import pathlib
import re
import sqlite3
import statistics
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from direct_lookup import free_port, start_server, stop_server
from paired_direct_lookup import login_campfire, seed_campfire, seed_rustfire, wait_for_server
from paired_bot_admin import request


TRANSFER = re.compile(r"/session/transfers/[A-Za-z0-9_-]+--[0-9a-f]{64}")
PROFILE_FIELDS = ("user[avatar]", "user[name]", "user[email_address]", "user[password]", "user[bio]")
VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}


class ProfilePanel(HTMLParser):
    def __init__(self, target="panel"):
        super().__init__(convert_charrefs=True)
        self.target = target
        self.depth = 0
        self.tokens = []

    def handle_starttag(self, tag, attrs):
        if self.depth == 0 and ((self.target == "panel" and tag == "section" and "panel" in dict(attrs).get("class", "").split()) or (self.target == "nav" and tag == "nav" and ("id", "nav") in attrs)):
            self.depth = 1
        elif self.depth and tag not in VOID_TAGS:
            self.depth += 1
        if self.depth:
            normalized = []
            for key, value in attrs:
                if key == "src" and value and "/avatar" in value:
                    value = "<signed-avatar>"
                elif key == "value" and ("name", "authenticity_token") in attrs:
                    value = "<csrf>"
                elif key in {"value", "data-copy-to-clipboard-content-value", "data-web-share-url-value"} and value and "/session/transfers/" in value:
                    value = "<transfer-url>"
                elif key in {"value", "data-copy-to-clipboard-content-value", "data-web-share-url-value"} and value and "/join/" in value:
                    value = "<join-url>"
                elif key == "src" and value and value.startswith("/account/logo?"):
                    value = "<logo-url>"
                elif key == "action" and value and value.startswith("/account/logo?v="):
                    value = "<versioned-logo-action>"
                elif key in {"href", "data-lightbox-url-value"} and value and value.startswith("/qr_code/"):
                    value = "<qr-url>"
                normalized.append((key, value))
            self.tokens.append(("start", tag, tuple(sorted(normalized))))

    def handle_endtag(self, tag):
        if self.depth and tag not in VOID_TAGS:
            self.tokens.append(("end", tag))
            self.depth -= 1

    def handle_data(self, data):
        if self.depth and data.strip():
            self.tokens.append(("text", " ".join(data.split()).replace("Campfire", "Rustfire")))


def compare_profile_panel(source, target, fragment="panel"):
    panels = []
    for page in (source, target):
        parser = ProfilePanel(fragment)
        parser.feed(page.decode())
        assert parser.tokens, f"Missing profile {fragment}"
        panels.append(parser.tokens)
    for index, (expected, actual) in enumerate(zip(*panels)):
        assert expected == actual, (index, expected, actual)
    assert len(panels[0]) == len(panels[1]), (len(panels[0]), len(panels[1]))
    return len(panels[0])


def read_manifest(port):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    try:
        connection.request("GET", "/webmanifest", headers={"Accept": "application/json"})
        response = connection.getresponse()
        payload = response.read()
        assert response.status == 200, (response.status, payload[:200])
        return json.loads(payload)
    finally:
        connection.close()


def verify_manifest(source, target):
    assert set(source) == set(target)
    for key in ("name", "start_url", "display", "scope", "categories", "theme_color", "background_color"):
        assert source[key] == target[key], (key, source[key], target[key])
    assert source["description"] and target["description"]
    for expected, actual in zip(source["icons"], target["icons"], strict=True):
        assert (expected["sizes"], expected["type"], expected.get("purpose")) == (actual["sizes"], actual["type"], actual.get("purpose"))
        expected_url = urllib.parse.urlsplit(html.unescape(expected["src"]))
        actual_url = urllib.parse.urlsplit(actual["src"])
        assert expected_url.path == actual_url.path == "/account/logo"
        assert urllib.parse.parse_qs(expected_url.query).get("size") == urllib.parse.parse_qs(actual_url.query).get("size")
    for expected, actual in zip(source["shortcuts"], target["shortcuts"], strict=True):
        assert expected["name"] == actual["name"] and expected["url"] == actual["url"]
        assert urllib.parse.urlsplit(expected["icons"][0]["src"]).path == urllib.parse.urlsplit(actual["icons"][0]["src"]).path
    for expected, actual in zip(source["screenshots"], target["screenshots"], strict=True):
        for key in ("sizes", "form_factor"):
            assert expected[key] == actual[key]
        assert urllib.parse.urlsplit(expected["src"]).path == urllib.parse.urlsplit(actual["src"]).path


def transfer_path(page):
    match = TRANSFER.search(html.unescape(page.decode()))
    assert match, "Profile has no signed device-transfer link"
    return match.group()


def upload_avatar(port, cookie, csrf, database, campfire):
    boundary = "rustfire-paired-profile-avatar"
    image = pathlib.Path("static/icons/app-icon-192.png").read_bytes()
    payload = b"".join((
        f'--{boundary}\r\nContent-Disposition: form-data; name="_method"\r\n\r\npatch\r\n'.encode(),
        f'--{boundary}\r\nContent-Disposition: form-data; name="authenticity_token"\r\n\r\n{csrf}\r\n'.encode(),
        f'--{boundary}\r\nContent-Disposition: form-data; name="user[avatar]"; filename="avatar.png"\r\nContent-Type: image/png\r\n\r\n'.encode(),
        image, b"\r\n", f"--{boundary}--\r\n".encode(),
    ))
    status, location, response = request(port, "POST", "/users/me/profile", cookie, csrf, payload, f"multipart/form-data; boundary={boundary}")
    assert status == 302 and urllib.parse.urlsplit(location).path == "/users/me/profile", (status, location, response[:200])
    with sqlite3.connect(database) as db:
        count = db.execute(
            "SELECT COUNT(*) FROM active_storage_attachments WHERE record_type='User' AND name='avatar' AND record_id=1" if campfire
            else "SELECT COUNT(*) FROM avatars WHERE user_id=1"
        ).fetchone()[0]
    assert count == 1, count


def delete_avatar(port, cookie, csrf, database, campfire):
    payload = urllib.parse.urlencode({"_method": "delete", "authenticity_token": csrf}).encode()
    status, location, response = request(port, "POST", "/users/1/avatar", cookie, csrf, payload, "application/x-www-form-urlencoded")
    assert status == 302 and urllib.parse.urlsplit(location).path == "/users/me/profile", (status, location, response[:200])
    with sqlite3.connect(database) as db:
        count = db.execute(
            "SELECT COUNT(*) FROM active_storage_attachments WHERE record_type='User' AND name='avatar' AND record_id=1" if campfire
            else "SELECT COUNT(*) FROM avatars WHERE user_id=1"
        ).fetchone()[0]
    assert count == 0, count


def use_transfer(port, path, method="POST"):
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, request, response, code, message, headers, destination):
            return None

    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()), NoRedirect())
    base = f"http://127.0.0.1:{port}"
    with opener.open(base + path) as response:
        page = response.read().decode()
        assert response.status == 200
    assert "auto-submit" in page and ('name="_method"' in page or "name='_method'" in page)
    token = re.search(r'name=["\']authenticity_token["\'] value=["\']([^"\']+)', page)
    assert token, "Device-transfer form has no CSRF token"
    csrf = html.unescape(token.group(1))
    if method != "POST":
        meta = re.search(r'<meta name=["\']csrf-token["\'] content=["\']([^"\']+)', page)
        assert meta, "Device-transfer page has no global CSRF token"
        csrf = html.unescape(meta.group(1))
    fields = {"authenticity_token": csrf}
    if method == "POST":
        fields["_method"] = "put"
    body = urllib.parse.urlencode(fields).encode()
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    if method != "POST":
        headers["X-CSRF-Token"] = csrf
    try:
        opener.open(urllib.request.Request(base + path, data=body, method=method,
            headers=headers))
        raise AssertionError("Device transfer did not redirect")
    except urllib.error.HTTPError as response:
        assert response.code == 302 and urllib.parse.urlsplit(response.headers["Location"]).path == "/", (response.code, response.headers["Location"])
    with opener.open(base + "/rooms/1") as response:
        assert response.status == 200 and response.url.endswith("/rooms/1")
        response.read()


def measure(port, cookie, concurrency, requests):
    assert requests % concurrency == 0
    barrier = threading.Barrier(concurrency)

    def worker():
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
        samples = []
        lengths = set()
        try:
            for _ in range(2):
                connection.request("GET", "/users/me/profile", headers={"Cookie": cookie, "Accept": "text/html"})
                response = connection.getresponse()
                response.read()
                assert response.status == 200
            barrier.wait(timeout=30)
            run_start = time.perf_counter()
            for _ in range(requests // concurrency):
                started = time.perf_counter()
                connection.request("GET", "/users/me/profile", headers={"Cookie": cookie, "Accept": "text/html"})
                response = connection.getresponse()
                payload = response.read()
                samples.append((time.perf_counter() - started) * 1000)
                assert response.status == 200, response.status
                lengths.add(len(payload))
            run_end = time.perf_counter()
        finally:
            connection.close()
        return samples, lengths, run_start, run_end

    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = [pool.submit(worker) for _ in range(concurrency)]
        results = [future.result() for future in futures]
    elapsed = max(end for _, _, _, end in results) - min(start for _, _, start, _ in results)
    samples = [value for batch, _, _, _ in results for value in batch]
    lengths = sorted({length for _, batch, _, _ in results for length in batch})
    assert len(samples) == requests
    return {
        "rps": round(requests / elapsed, 1),
        "median_ms": round(statistics.median(samples), 2),
        "p95_ms": round(sorted(samples)[math.ceil(.95 * len(samples)) - 1], 2),
        "bytes": lengths,
    }


def verify_mutations(port, cookie, csrf, database):
    body = urllib.parse.urlencode({
        "_method": "patch",
        "user[name]": "Paired Admin",
        "user[email_address]": "Paired@Example.invalid",
        "user[bio]": "Profile parity check",
    }).encode()
    status, location, payload = request(port, "POST", "/users/me/profile", cookie, csrf, body, "application/x-www-form-urlencoded")
    assert status == 302 and urllib.parse.urlsplit(location).path == "/users/me/profile", (status, location, payload[:200])
    with sqlite3.connect(database) as db:
        profile = db.execute("SELECT name,email_address,bio FROM users WHERE id=1").fetchone()
    assert profile == ("Paired Admin", "Paired@Example.invalid", "Profile parity check"), profile
    status, _, page = request(port, "GET", "/users/me/profile", cookie, csrf)
    assert status == 200 and b"Profile parity check" in page
    body = urllib.parse.urlencode({"_method": "put"}).encode()
    status, location, payload = request(port, "POST", "/rooms/1/involvement?involvement=everything", cookie, csrf, body, "application/x-www-form-urlencoded")
    assert status == 302 and urllib.parse.urlsplit(location).path == "/rooms/1/involvement", (status, location, payload[:200])
    with sqlite3.connect(database) as db:
        involvement = db.execute("SELECT involvement FROM memberships WHERE room_id=1 AND user_id=1").fetchone()[0]
    assert involvement == "everything", involvement
    return profile, involvement


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--requests", type=int, default=160)
    parser.add_argument("--campfire-repo", type=pathlib.Path, default=pathlib.Path("/tmp/once-campfire-reference"))
    parser.add_argument("--ruby", type=pathlib.Path, default=pathlib.Path("/tmp/rustfire-baseline/local/bin/ruby"))
    parser.add_argument("--bundle-path", type=pathlib.Path, default=pathlib.Path("/tmp/rustfire-baseline/bundle"))
    args = parser.parse_args()
    if args.requests < 32 or args.requests % 32:
        parser.error("--requests must be a multiple of 32")
    repository, ruby, bundle_path = args.campfire_repo.resolve(), args.ruby.resolve(), args.bundle_path.resolve()
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repository, text=True).strip()
    assert revision == "91d294f4a09f9bbe37f9548959bfcb43645678fb", revision
    with tempfile.TemporaryDirectory(prefix="paired-profile-") as directory:
        temp = pathlib.Path(directory)
        rust_db, camp_db = temp / "rustfire.sqlite3", temp / "campfire.sqlite3"
        rust_port, camp_port = free_port(), free_port()
        seed_rustfire(rust_db, rust_port, [[2]])
        env = seed_campfire(repository, ruby, bundle_path, repository / "storage/db/production.sqlite3", camp_db, [[2]], camp_port, temp)
        env["WEB_CONCURRENCY"] = "4"
        with sqlite3.connect(rust_db) as db:
            db.execute("UPDATE users SET name='Test Admin',email_address='benchmark@example.invalid' WHERE id=1")
            db.execute("UPDATE rooms SET name='All Talk' WHERE id=1")
        with sqlite3.connect(camp_db) as db:
            db.execute("UPDATE accounts SET name='Benchmark' WHERE id=1")
            db.execute("UPDATE users SET name='Test Admin' WHERE id=1")
            db.execute("UPDATE users SET name='User 2' WHERE id=2")
        rust = start_server(rust_db, rust_port, {"RUSTFIRE_CAMPFIRE_SECRET_KEY_BASE": env["SECRET_KEY_BASE"]})
        log = open(temp / "puma.log", "w+")
        camp = subprocess.Popen([str(ruby), str(ruby.parent / "bundle"), "exec", "puma", "-C", "config/puma.rb"], cwd=repository, env=env, stdout=log, stderr=log)
        try:
            wait_for_server(camp_port, camp)
            verify_manifest(read_manifest(camp_port), read_manifest(rust_port))
            camp_cookie, camp_csrf = login_campfire(camp_port)
            rust_cookie, rust_csrf = "session_token=benchmark-session", "benchmark-csrf"
            camp_status, _, camp_page = request(camp_port, "GET", "/users/me/profile", camp_cookie, camp_csrf)
            rust_status, _, rust_page = request(rust_port, "GET", "/users/me/profile", rust_cookie, rust_csrf)
            assert (camp_status, rust_status) == (200, 200)
            for field in PROFILE_FIELDS:
                assert field.encode() in camp_page and field.encode() in rust_page, field
            print(f"parsed profile nav: {compare_profile_panel(camp_page, rust_page, 'nav')} matching tokens")
            matched_profile_tokens = compare_profile_panel(camp_page, rust_page)
            print(f"parsed profile panel: {matched_profile_tokens} matching tokens")
            for user_id in ("2", "999"):
                camp_status, _, camp_alias = request(camp_port, "GET", f"/users/{user_id}/profile", camp_cookie, camp_csrf)
                rust_status, _, rust_alias = request(rust_port, "GET", f"/users/{user_id}/profile", rust_cookie, rust_csrf)
                assert (camp_status, rust_status) == (200, 200)
                assert compare_profile_panel(camp_alias, rust_alias, "nav") == compare_profile_panel(camp_page, rust_page, "nav")
                assert compare_profile_panel(camp_alias, rust_alias) == matched_profile_tokens
            upload_avatar(camp_port, camp_cookie, camp_csrf, camp_db, True)
            upload_avatar(rust_port, rust_cookie, rust_csrf, rust_db, False)
            camp_status, _, camp_page = request(camp_port, "GET", "/users/me/profile", camp_cookie, camp_csrf)
            rust_status, _, rust_page = request(rust_port, "GET", "/users/me/profile", rust_cookie, rust_csrf)
            assert (camp_status, rust_status) == (200, 200)
            print(f"profile with avatar: {compare_profile_panel(camp_page, rust_page)} matching tokens")
            with sqlite3.connect(rust_db) as db:
                transfers_before = db.execute("SELECT COUNT(*) FROM session_transfers").fetchone()[0]
            for _ in range(3):
                request(rust_port, "GET", "/users/me/profile", rust_cookie, rust_csrf)
            with sqlite3.connect(rust_db) as db:
                assert db.execute("SELECT COUNT(*) FROM session_transfers").fetchone()[0] == transfers_before
            use_transfer(rust_port, transfer_path(camp_page))
            use_transfer(camp_port, transfer_path(rust_page))
            use_transfer(rust_port, transfer_path(camp_page), method="PATCH")
            use_transfer(camp_port, transfer_path(rust_page), method="PATCH")
            use_transfer(rust_port, transfer_path(camp_page), method="PUT")
            use_transfer(camp_port, transfer_path(rust_page), method="PUT")
            print("manifest shape, profile fields, stateless reads, and two-way transfer by form POST, PATCH, and PUT: passed")
            print("GET /users/me/profile; 4 Puma workers; release Rustfire; requests per run:", args.requests)
            for concurrency in (1, 8, 32):
                source = measure(camp_port, camp_cookie, concurrency, args.requests)
                target = measure(rust_port, rust_cookie, concurrency, args.requests)
                print(f"concurrency={concurrency}: Campfire={source}; Rustfire={target}; throughput_ratio={target['rps']/source['rps']:.2f}x")
            source_mutations = verify_mutations(camp_port, camp_cookie, camp_csrf, camp_db)
            target_mutations = verify_mutations(rust_port, rust_cookie, rust_csrf, rust_db)
            assert source_mutations == target_mutations
            print("profile fields and involvement mutation: passed", source_mutations)
            delete_avatar(camp_port, camp_cookie, camp_csrf, camp_db, True)
            delete_avatar(rust_port, rust_cookie, rust_csrf, rust_db, False)
            camp_status, _, camp_page = request(camp_port, "GET", "/users/me/profile", camp_cookie, camp_csrf)
            rust_status, _, rust_page = request(rust_port, "GET", "/users/me/profile", rust_cookie, rust_csrf)
            assert (camp_status, rust_status) == (200, 200)
            print(f"profile after avatar deletion: {compare_profile_panel(camp_page, rust_page)} matching tokens")
            alias_body = urllib.parse.urlencode({"_method": "patch", "user[name]": "Alias Admin"}).encode()
            for port, cookie, csrf, database in ((camp_port, camp_cookie, camp_csrf, camp_db), (rust_port, rust_cookie, rust_csrf, rust_db)):
                status, location, payload = request(port, "POST", "/users/999/profile", cookie, csrf, alias_body, "application/x-www-form-urlencoded")
                assert status == 302 and urllib.parse.urlsplit(location).path == "/users/me/profile", (status, location, payload[:200])
                with sqlite3.connect(database) as db:
                    assert db.execute("SELECT name FROM users WHERE id=1").fetchone() == ("Alias Admin",)
                    assert db.execute("SELECT name FROM users WHERE id=2").fetchone() == ("User 2",)
            print("explicit user profile aliases: paired GET and PATCH passed")
        except Exception:
            log.flush()
            log.seek(0)
            print(log.read()[-3000:])
            raise
        finally:
            stop_server(camp)
            stop_server(rust)
            log.close()


if __name__ == "__main__":
    main()
