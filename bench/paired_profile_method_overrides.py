"""Compare profile multipart method overrides and saved state with Campfire."""

import hashlib
import http.client
import pathlib
import sqlite3
import subprocess
import tempfile
import urllib.parse

from direct_lookup import free_port, start_server, stop_server
from paired_banned_content import BUNDLE, REPOSITORY, REVISION, RUBY, isolated_campfire, start_redis
from paired_bot_admin import PNG
from paired_direct_lookup import login_campfire, seed_campfire, seed_rustfire, wait_for_server


def multipart(fields):
    boundary = "rustfire-profile-method-check"
    pieces = []
    for name, value in fields:
        if isinstance(value, tuple):
            filename, content_type, data = value
            pieces.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; filename="{filename}"\r\nContent-Type: {content_type}\r\n\r\n'.encode() + data + b'\r\n')
        else:
            pieces.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
    pieces.append(f"--{boundary}--\r\n".encode())
    return b"".join(pieces), f"multipart/form-data; boundary={boundary}"


def profile_row(database):
    with sqlite3.connect(database) as db:
        return db.execute("SELECT name,email_address,bio FROM users WHERE id=1").fetchone()


def avatar_identity(database):
    with sqlite3.connect(database) as db:
        if db.execute("SELECT EXISTS(SELECT 1 FROM sqlite_master WHERE name='active_storage_attachments')").fetchone()[0]:
            row = db.execute("""SELECT blob_id FROM active_storage_attachments
                WHERE record_type='User' AND record_id=1 AND name='avatar'""").fetchone()
        else:
            row = db.execute("SELECT stored_name FROM avatars WHERE user_id=1").fetchone()
        return row[0] if row else None


def request(port, database, cookie, csrf, fields, valid_csrf, encoding, method_header=None, query=None):
    body, content_type = (
        multipart(fields) if encoding == "multipart"
        else (urllib.parse.urlencode(fields).encode(), "application/x-www-form-urlencoded")
    )
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
    try:
        before = profile_row(database)
        avatar_before = avatar_identity(database)
        headers = {
            "Cookie": cookie,
            "Accept": "text/html",
            "Content-Type": content_type,
            "X-CSRF-Token": csrf if valid_csrf else "invalid-csrf",
        }
        if method_header:
            headers["X-HTTP-Method-Override"] = method_header
        path = "/users/me/profile" + ("?" + urllib.parse.urlencode(query) if query else "")
        connection.request("POST", path, body=body, headers=headers)
        response = connection.getresponse()
        data = response.read()
        location = response.getheader("Location")
        return (
            response.status,
            (response.getheader("Content-Type") or "").split(";", 1)[0],
            urllib.parse.urlsplit(location).path if location else None,
            (len(data), hashlib.sha256(data).hexdigest()) if data else None,
            profile_row(database) != before,
            profile_row(database),
            avatar_identity(database) != avatar_before,
        )
    finally:
        connection.close()


CASES = (
    ("plain valid CSRF", [("user[name]", "Plain should not save")], True, "multipart"),
    ("plain invalid CSRF", [("user[name]", "Plain should not save")], False, "multipart"),
    ("delete override valid CSRF", [("_method", "delete"), ("user[name]", "Delete should not save")], True, "multipart"),
    ("patch override invalid CSRF", [("_method", "patch"), ("user[name]", "Invalid should not save")], False, "multipart"),
    ("patch override valid CSRF", [("_method", "patch"), ("user[name]", "Multipart method works")], True, "multipart"),
    ("patch override after field", [("user[name]", "Late method works"), ("_method", "patch")], True, "multipart"),
    ("duplicate valid then invalid", [("_method", "patch"), ("_method", "delete"), ("user[name]", "Duplicate should not save")], True, "multipart"),
    ("duplicate invalid then valid", [("_method", "delete"), ("_method", "patch"), ("user[name]", "Last method works")], True, "multipart"),
    ("uppercase method", [("_method", "PATCH"), ("user[name]", "Uppercase method works")], True, "multipart"),
    ("urlencoded duplicate valid then invalid", [("_method", "patch"), ("_method", "delete"), ("user[name]", "URL duplicate should not save")], True, "urlencoded"),
    ("urlencoded duplicate invalid then valid", [("_method", "delete"), ("_method", "patch"), ("user[name]", "URL last method works")], True, "urlencoded"),
    ("urlencoded uppercase method", [("_method", "PATCH"), ("user[name]", "URL uppercase works")], True, "urlencoded"),
    ("header patch override", [("user[name]", "Header method works")], True, "urlencoded", "PATCH"),
    ("form method takes precedence", [("_method", "delete"), ("user[name]", "Header should not save")], True, "urlencoded", "PATCH"),
    ("multipart missing user group", [("_method", "patch"), ("foo", "bar")], True, "multipart"),
    ("multipart unknown nested user field", [("_method", "patch"), ("user[unknown]", "bar")], True, "multipart"),
    ("multipart scalar user", [("_method", "patch"), ("user", "bar")], True, "multipart"),
    ("multipart array user", [("_method", "patch"), ("user[]", "bar")], True, "multipart"),
    ("multipart unscoped avatar", [("_method", "patch"), ("avatar", ("avatar.png", "image/png", PNG))], True, "multipart"),
    ("query suppresses multipart avatar", [("_method", "patch"), ("user[avatar]", ("avatar.png", "image/png", PNG))], True, "multipart", None, {"user[unknown]": "bar"}),
    ("query scalar overrides multipart avatar", [("_method", "patch"), ("user[avatar]", ("avatar.png", "image/png", PNG))], True, "multipart", None, {"user": "scalar"}),
)


def main():
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPOSITORY, text=True).strip() == REVISION
    with tempfile.TemporaryDirectory(prefix="paired-profile-multipart-methods-") as scratch:
        temp = pathlib.Path(scratch)
        rust_db, camp_db = temp / "rust.sqlite3", temp / "camp.sqlite3"
        rust_port, camp_port, redis_port = free_port(), free_port(), free_port()
        seed_rustfire(rust_db, rust_port, [])
        camp_env = seed_campfire(REPOSITORY, RUBY, BUNDLE, REPOSITORY / "storage/db/production.sqlite3", camp_db, [], camp_port, temp)
        with sqlite3.connect(camp_db) as camp, sqlite3.connect(rust_db) as rust:
            rust.execute("UPDATE users SET name=?,email_address=?,bio=? WHERE id=1", profile_row(camp_db))
        checkout = isolated_campfire(temp, redis_port)
        camp_env["REDIS_URL"] = f"redis://127.0.0.1:{redis_port}"
        redis, redis_log = start_redis(temp, redis_port)
        try:
            rust = start_server(rust_db, rust_port)
            with open(temp / "puma.log", "w+") as log:
                camp = subprocess.Popen(
                    [str(RUBY), str(RUBY.parent / "bundle"), "exec", "puma", "-C", "config/puma.rb"],
                    cwd=checkout, env=camp_env, stdout=log, stderr=log,
                )
                try:
                    wait_for_server(camp_port, camp)
                    cookie, csrf = login_campfire(camp_port)
                    comparisons = []
                    for label, fields, valid_csrf, encoding, *extra in CASES:
                        method_header = extra[0] if extra else None
                        query = extra[1] if len(extra) > 1 else None
                        rust_result = request(rust_port, rust_db, "session_token=benchmark-session", "benchmark-csrf", fields, valid_csrf, encoding, method_header, query)
                        camp_result = request(camp_port, camp_db, cookie, csrf, fields, valid_csrf, encoding, method_header, query)
                        comparisons.append((label, rust_result, camp_result))
                finally:
                    stop_server(camp)
                    stop_server(rust)
        finally:
            redis.terminate()
            redis.wait(timeout=10)
            redis_log.close()
    mismatches = [(label, rust, camp) for label, rust, camp in comparisons if rust != camp]
    for label, rust, camp in mismatches:
        print(f"{label}: Rustfire={rust}, Campfire={camp}")
    print(f"Matched {len(CASES) - len(mismatches)}/{len(CASES)} profile multipart route cases")
    if mismatches:
        raise AssertionError(f"{len(mismatches)} profile multipart cases differ")


if __name__ == "__main__":
    main()
