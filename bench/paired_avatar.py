"""Compare avatar uploads, fallback rendering, and cache behavior with Campfire."""

import http.client
import pathlib
import re
import sqlite3
import subprocess
import tempfile
import urllib.parse
import xml.etree.ElementTree as ElementTree

from direct_lookup import free_port, start_server, stop_server
from paired_banned_content import start_redis
from paired_bot_admin import PNG, cleanup_campfire_uploads
from paired_direct_lookup import login_campfire, seed_campfire, seed_rustfire, wait_for_server
from paired_user_profiles import avatar_path as public_avatar_path


REPOSITORY = pathlib.Path("/tmp/once-campfire-reference")
RUBY = pathlib.Path("/tmp/rustfire-baseline/local/bin/ruby")
BUNDLE = pathlib.Path("/tmp/rustfire-baseline/bundle")
REVISION = "91d294f4a09f9bbe37f9548959bfcb43645678fb"


def request(port, method, path, cookie, csrf="", body=b"", content_type=None, etag=None):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    headers = {"Cookie": cookie, "X-CSRF-Token": csrf, "Accept": "text/html"}
    if content_type:
        headers["Content-Type"] = content_type
    if etag:
        headers["If-None-Match"] = etag
    try:
        connection.request(method, path, body, headers)
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        connection.close()


def signed_path(port, cookie):
    status, _, body = request(port, "GET", "/users/me/profile", cookie)
    assert status == 200, (status, body[:200])
    path = re.search(rb'["\'](/users/[^"\']+/avatar(?:\?[^"\']*)?)["\']', body)
    assert path, body[:300]
    return path.group(1).decode()


def multipart_avatar(filename, content_type, data):
    boundary = b"paired-avatar-upload"
    body = (b"--" + boundary + b"\r\nContent-Disposition: form-data; name=\"_method\"\r\n\r\npatch\r\n"
            + b"--" + boundary + b"\r\nContent-Disposition: form-data; name=\"user[avatar]\"; filename=\""
            + filename.encode() + b"\"\r\nContent-Type: " + content_type.encode() + b"\r\n\r\n"
            + data + b"\r\n--" + boundary + b"--\r\n")
    return body, "multipart/form-data; boundary=" + boundary.decode()


def avatar_state(port, cookie):
    path = signed_path(port, cookie)
    status, headers, body = request(port, "GET", path, cookie)
    assert status == 200, (path, status, body[:200])
    etag = headers.get("etag")
    if etag:
        cached_status, cached_headers, cached_body = request(port, "GET", path, cookie, etag=etag)
        assert cached_status == 304 and cached_body == b"", (path, cached_status, cached_body[:100])
        assert cached_headers.get("etag") == etag
        assert cached_headers.get("cache-control") == "no-cache", cached_headers
    return path, headers.get("content-type"), etag, body, {key: headers.get(key) for key in ("cache-control", "last-modified", "content-disposition")}


def workflow(port, cookie, csrf, jpeg, bmp):
    states = [avatar_state(port, cookie)]
    for filename, content_type, data in (("moon.jpg", "image/jpeg", jpeg), ("pixel.bmp", "image/bmp", bmp)):
        body, request_type = multipart_avatar(filename, content_type, data)
        status, headers, response = request(port, "POST", "/users/me/profile", cookie, csrf, body, request_type)
        assert status == 302, (filename, status, headers.get("location"), response[:200])
        assert urllib.parse.urlsplit(headers["location"]).path == "/users/me/profile"
        states.append(avatar_state(port, cookie))
        status, _, _ = request(port, "GET", states[-1][0], cookie, etag=states[-2][2])
        assert status == 200, (filename, "old avatar validator accepted", status)
    return states


def spoofed_mime_bot(port, cookie, csrf, database, campfire, name, filename, data):
    boundary = b"paired-bot-avatar"
    body = (b"--" + boundary + b"\r\nContent-Disposition: form-data; name=\"user[name]\"\r\n\r\n" + name.encode() + b"\r\n"
            + b"--" + boundary + b"\r\nContent-Disposition: form-data; name=\"user[avatar]\"; filename=\"" + filename.encode() + b"\"\r\nContent-Type: text/plain\r\n\r\n"
            + data + b"\r\n--" + boundary + b"--\r\n")
    status, headers, response = request(port, "POST", "/account/bots", cookie, csrf, body, "multipart/form-data; boundary=" + boundary.decode())
    assert status == 302, (status, headers.get("location"), response[:200])
    with sqlite3.connect(database) as db:
        bot_id = db.execute("SELECT id FROM users WHERE name=?", [name]).fetchone()[0]
        if campfire:
            stored_type = db.execute("SELECT b.content_type FROM active_storage_attachments a JOIN active_storage_blobs b ON b.id=a.blob_id WHERE a.record_type='User' AND a.record_id=? AND a.name='avatar'", [bot_id]).fetchone()[0]
        else:
            stored_type = db.execute("SELECT content_type FROM avatars WHERE user_id=?", [bot_id]).fetchone()[0]
    status, _, page = request(port, "GET", f"/users/{bot_id}", cookie)
    assert status == 200
    path = public_avatar_path(page)
    avatar_status, avatar_headers, avatar_body = request(port, "GET", path, cookie)
    assert avatar_status == 200
    return stored_type, avatar_headers.get("content-type"), avatar_body


def main():
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPOSITORY, text=True).strip()
    assert revision == REVISION, revision
    with tempfile.TemporaryDirectory(prefix="paired-avatar-") as scratch:
        temp = pathlib.Path(scratch)
        rust_db, camp_db = temp / "rust.sqlite3", temp / "camp.sqlite3"
        rust_port, camp_port, redis_port = free_port(), free_port(), free_port()
        seed_rustfire(rust_db, rust_port, [])
        source_database = REPOSITORY / "storage/db/production.sqlite3"
        camp_env = seed_campfire(REPOSITORY, RUBY, BUNDLE, source_database, camp_db, [], camp_port, temp)
        camp_env["REDIS_URL"] = f"redis://127.0.0.1:{redis_port}"
        camp_env["WEB_CONCURRENCY"] = "1"
        for database in (rust_db, camp_db):
            with sqlite3.connect(database) as db:
                db.execute("UPDATE users SET name='Avatar Probe',updated_at='2026-01-01T00:00:00Z' WHERE id=1")
        jpeg = (REPOSITORY / "test/fixtures/files/moon.jpg").read_bytes()
        bmp = (REPOSITORY / "test/fixtures/files/pixel.bmp").read_bytes()
        redis, redis_log = start_redis(temp, redis_port)
        try:
            with open(temp / "puma.log", "w+") as log:
                camp = subprocess.Popen([str(RUBY), str(RUBY.parent / "bundle"), "exec", "puma", "-C", "config/puma.rb"], cwd=REPOSITORY, env=camp_env, stdout=log, stderr=log)
                try:
                    wait_for_server(camp_port, camp)
                    camp_cookie, camp_csrf = login_campfire(camp_port)
                    expected = workflow(camp_port, camp_cookie, camp_csrf, jpeg, bmp)
                    expected_bots = [spoofed_mime_bot(camp_port, camp_cookie, camp_csrf, camp_db, True, name, filename, data)
                                     for name, filename, data in (("Spoofed JPEG Bot", "moon.jpg", jpeg), ("Spoofed PNG Bot", "pixel.png", PNG))]
                except Exception:
                    log.flush()
                    log.seek(0)
                    print(log.read()[-2000:])
                    raise
                finally:
                    stop_server(camp)
            rust = start_server(rust_db, rust_port, {"RUSTFIRE_UPLOAD_DIR": str(temp / "uploads")})
            try:
                actual = workflow(rust_port, "session_token=benchmark-session", "benchmark-csrf", jpeg, bmp)
                actual_bots = [spoofed_mime_bot(rust_port, "session_token=benchmark-session", "benchmark-csrf", rust_db, False, name, filename, data)
                               for name, filename, data in (("Spoofed JPEG Bot", "moon.jpg", jpeg), ("Spoofed PNG Bot", "pixel.png", PNG))]
            finally:
                stop_server(rust)
            for label, rust_state, camp_state in zip(("stock", "JPEG", "BMP fallback"), actual, expected):
                assert rust_state[1] == camp_state[1], (label, rust_state[1], camp_state[1])
                if label == "JPEG":
                    assert rust_state[1] == "image/webp" and rust_state[3][:4] == camp_state[3][:4] == b"RIFF"
                    assert rust_state[3] == camp_state[3], (label, len(rust_state[3]), len(camp_state[3]))
                else:
                    assert rust_state[1] == "image/svg+xml; charset=utf-8"
                    actual_svg = ElementTree.canonicalize(rust_state[3].decode(), strip_text=True)
                    expected_svg = ElementTree.canonicalize(camp_state[3].decode(), strip_text=True)
                    assert actual_svg == expected_svg, (label, actual_svg[:300], expected_svg[:300])
                assert rust_state[4]["cache-control"] == camp_state[4]["cache-control"]
            assert len({stage[2] for stage in actual}) == len(actual)
            for actual_bot, expected_bot in zip(actual_bots, expected_bots):
                assert actual_bot[:2] == expected_bot[:2], (actual_bot[:2], expected_bot[:2])
                assert actual_bot[2] == expected_bot[2], (len(actual_bot[2]), len(expected_bot[2]))
            print("PASS paired JPEG and BMP avatar uploads, spoofed-mime bot avatars, exact WebP bytes, initials SVG, and conditional cache behavior")
        finally:
            redis.terminate()
            redis.wait(timeout=10)
            redis_log.close()
            cleanup_campfire_uploads(source_database, camp_db, REPOSITORY)


if __name__ == "__main__":
    main()
