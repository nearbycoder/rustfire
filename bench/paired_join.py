"""Compare Campfire and Rustfire's invitation signup flow on disposable databases."""

import http.cookiejar
import pathlib
import re
import sqlite3
import subprocess
import tempfile
import urllib.error
import urllib.parse
import urllib.request

from direct_lookup import free_port, start_server, stop_server
from paired_banned_content import start_redis
from paired_direct_lookup import login_campfire, seed_campfire, seed_rustfire, wait_for_server
from paired_room_shell import HeadMeta, assert_equal, section
from paired_turbo_fanout import MessageTagSequence


ROOT = pathlib.Path(__file__).resolve().parents[1]
SOURCE = pathlib.Path("/tmp/once-campfire-reference")
RUBY = pathlib.Path("/tmp/rustfire-baseline/local/bin/ruby")
BUNDLE = pathlib.Path("/tmp/rustfire-baseline/bundle")
REVISION = "91d294f4a09f9bbe37f9548959bfcb43645678fb"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        return None


def browser():
    jar = http.cookiejar.CookieJar()
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar), NoRedirect())


def fetch(opener, port, path, fields=None, csrf=None, cookie=None):
    headers = {"Accept": "text/html"}
    if cookie:
        headers["Cookie"] = cookie
    if csrf:
        headers["X-CSRF-Token"] = csrf
    body = urllib.parse.urlencode(fields).encode() if fields is not None else None
    if body is not None:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    request = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=body, headers=headers)
    try:
        response = opener.open(request)
    except urllib.error.HTTPError as error:
        response = error
    with response:
        return response.status, urllib.parse.urlsplit(response.headers.get("Location", "")).path, response.read().decode(), response.headers.get("Location", "")


def csrf_token(page):
    match = re.search(r'<meta name=[\'\"]csrf-token[\'\"] content=[\'\"]([^\'\"]+)', page)
    assert match, "signup page has no CSRF token"
    return match.group(1)


def navigation_shape(page):
    nav = re.search(r'<nav id="nav">.*?</nav>', page, re.S)
    assert nav, "signup navigation missing"
    parser = MessageTagSequence()
    parser.feed(nav.group())
    return parser.tags, parser.attribute_keys, parser.text


def icon_links(page):
    parser = HeadMeta()
    parser.feed(page)
    assert set(parser.links) == {"icon", "apple-touch-icon"}, parser.links
    return parser.links


def check(port, database, signed_cookie):
    opener = browser()
    status, _, page, _ = fetch(opener, port, "/join/benchmark")
    assert status == 200, status
    initial_page = page
    for field in ("user[name]", "user[email_address]", "user[password]", "user[avatar]"):
        assert f'name="{field}"' in page, field
    assert 'enctype="multipart/form-data"' in page
    assert re.search(r'name=[\'\"]authenticity_token[\'\"] value=[\'\"][^\'\"]+', page)
    assert "Benchmark" in page and "Sign up" in page
    navigation = navigation_shape(page)
    icons = icon_links(page)
    assert fetch(opener, port, "/join/wrong")[0] == 404
    assert fetch(browser(), port, "/join/benchmark", cookie=signed_cookie)[0:2] == (302, "/")
    status, _, page, _ = fetch(opener, port, "/join/benchmark")
    assert status == 200
    token = csrf_token(page)

    duplicate = {"user[name]": "Duplicate", "user[email_address]": "benchmark@example.invalid", "user[password]": "benchmark-password"}
    status, path, _, location = fetch(opener, port, "/join/benchmark", duplicate, token)
    assert (status, path) == (302, "/session/new"), (status, location)
    assert urllib.parse.parse_qs(urllib.parse.urlsplit(location).query) == {"email_address": ["benchmark@example.invalid"]}
    status, _, login, _ = fetch(opener, port, "/session/new?email_address=benchmark%40example.invalid")
    assert status == 200 and ('value="benchmark@example.invalid"' in login or "value='benchmark@example.invalid'" in login)

    status, _, page, _ = fetch(opener, port, "/join/benchmark")
    assert status == 200
    token = csrf_token(page)

    user = {"user[name]": "New Member", "user[email_address]": "new-member@example.invalid", "user[password]": "signup-password"}
    status, path, _, location = fetch(opener, port, "/join/benchmark", user, token)
    assert (status, path) == (302, "/"), (status, location)
    with sqlite3.connect(database) as db:
        uid, name, email = db.execute("SELECT id,name,email_address FROM users WHERE email_address=?", [user["user[email_address]"]]).fetchone()
        memberships = db.execute("SELECT room_id FROM memberships WHERE user_id=? ORDER BY room_id", [uid]).fetchall()
        open_rooms = db.execute("SELECT id FROM rooms WHERE type='Rooms::Open' ORDER BY id").fetchall()
    assert (name, email, memberships) == ("New Member", "new-member@example.invalid", open_rooms)
    signup_result = (status, path, len(memberships))

    mixed = {"user[name]": "Mixed Case", "user[email_address]": "Mixed.Case@Example.invalid", "user[password]": "signup-password"}
    mixed_browser = browser()
    status, _, page, _ = fetch(mixed_browser, port, "/join/benchmark")
    assert status == 200
    assert fetch(mixed_browser, port, "/join/benchmark", mixed, csrf_token(page))[:2] == (302, "/")
    with sqlite3.connect(database) as db:
        stored_email = db.execute("SELECT email_address FROM users WHERE name='Mixed Case'").fetchone()[0]
    login_results = []
    for address in ("Mixed.Case@Example.invalid", "mixed.case@example.invalid"):
        login_browser = browser()
        status, _, login_page, _ = fetch(login_browser, port, "/session/new")
        assert status == 200
        login_results.append(fetch(login_browser, port, "/session", {"email_address": address, "password": "signup-password"}, csrf_token(login_page))[0])
    assert (stored_email, login_results) == ("Mixed.Case@Example.invalid", [302, 401]), (stored_email, login_results)
    return signup_result, navigation, icons, initial_page, login


def main():
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=SOURCE, text=True).strip() == REVISION
    with tempfile.TemporaryDirectory(prefix="paired-join-") as scratch:
        temp = pathlib.Path(scratch)
        rust_db, source_db = temp / "rust.sqlite3", temp / "camp.sqlite3"
        rust_port, source_port, redis_port = free_port(), free_port(), free_port()
        seed_rustfire(rust_db, rust_port, [])
        source_env = seed_campfire(SOURCE, RUBY, BUNDLE, SOURCE / "storage/db/production.sqlite3", source_db, [], source_port, temp)
        vapid_code = 'require "openssl"; require "base64"; key=OpenSSL::PKey::EC.new(File.binread(ARGV[0])); puts Base64.urlsafe_encode64(key.public_key.to_bn.to_s(2), padding: false); puts Base64.urlsafe_encode64(key.private_key.to_s(2).rjust(32,"\\0"), padding: false)'
        source_env["VAPID_PUBLIC_KEY"], source_env["VAPID_PRIVATE_KEY"] = subprocess.check_output(
            [str(RUBY), "-e", vapid_code, str(rust_db.with_suffix(".vapid.der"))], text=True,
        ).splitlines()
        source_env["REDIS_URL"] = f"redis://127.0.0.1:{redis_port}"
        with sqlite3.connect(source_db) as db:
            db.execute("UPDATE accounts SET name='Benchmark',join_code='benchmark' WHERE id=1")
            account_updated_at = db.execute("SELECT updated_at FROM accounts WHERE id=1").fetchone()[0]
            owner_name = db.execute("SELECT name FROM users WHERE id=1").fetchone()[0]
        with sqlite3.connect(rust_db) as db:
            db.execute("UPDATE accounts SET updated_at=? WHERE id=1", [account_updated_at])
            db.execute("UPDATE users SET name=?,email_address='benchmark@example.invalid' WHERE id=1", [owner_name])
        redis, redis_log = start_redis(temp, redis_port)
        try:
            rust = start_server(rust_db, rust_port, {"RUSTFIRE_CAMPFIRE_SECRET_KEY_BASE": source_env["SECRET_KEY_BASE"]})
            try:
                rust_result = check(rust_port, rust_db, "session_token=benchmark-session")
            finally:
                stop_server(rust)
            with open(temp / "puma.log", "w+") as log:
                source = subprocess.Popen([str(RUBY), str(RUBY.parent / "bundle"), "exec", "puma", "-C", "config/puma.rb"], cwd=SOURCE, env=source_env, stdout=log, stderr=log)
                try:
                    wait_for_server(source_port, source)
                    cookie, _ = login_campfire(source_port)
                    source_result = check(source_port, source_db, cookie)
                except Exception:
                    log.flush()
                    log.seek(0)
                    print(log.read()[-3000:])
                    raise
                finally:
                    stop_server(source)
            assert rust_result[:3] == source_result[:3], (rust_result[:3], source_result[:3])
            for label, source_page, target_page in (("invitation", source_result[3], rust_result[3]), ("duplicate-email sign-in", source_result[4], rust_result[4])):
                for part in ("head", "body"):
                    assert_equal(f"{label} complete {part}", section(source_page.encode(), part), section(target_page.encode(), part))
        finally:
            redis.terminate()
            redis.wait(timeout=10)
            redis_log.close()
    print("PASS paired invitation signup, duplicate email, and open-room membership")


if __name__ == "__main__":
    main()
