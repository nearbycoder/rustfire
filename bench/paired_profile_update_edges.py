"""Compare profile update edge inputs with pinned Campfire and disposable accounts."""

import hashlib
import pathlib
import sqlite3
import subprocess
import tempfile
import urllib.parse

from direct_lookup import free_port, start_server, stop_server
from paired_banned_content import BUNDLE, REPOSITORY, REVISION, RUBY, isolated_campfire, start_redis
from paired_bot_admin import request
from paired_direct_lookup import login_campfire, seed_campfire, seed_rustfire, wait_for_server
from paired_join import browser
from paired_session import request as session_request, token as session_token


CASES = (
    ("padded name", {"user[name]": "  Changed Name  "}),
    ("non-email address", {"user[email_address]": "not-an-email"}),
    ("short password", {"user[password]": "x"}),
    ("blank password", {"user[password]": ""}),
    ("padded email", {"user[email_address]": "  padded@example.invalid  "}),
    ("long bio", {"user[bio]": "long bio " * 30}),
    ("blank name", {"user[name]": ""}),
    ("blank email", {"user[email_address]": ""}),
    ("duplicate email", {"user[email_address]": "collision@example.invalid"}),
    ("method only", {}),
    ("unknown top-level", {"foo": "bar"}),
    ("unknown user field", {"user[unknown]": "bar"}),
    ("scalar user", {"user": "bar"}),
    ("array user", {"user[]": "bar"}),
    ("query user only", {}, {"user[name]": "Query Name"}),
    ("query and body user names", {"user[name]": "Body Name"}, {"user[name]": "Query Name"}),
    ("query user scalar", {"user[name]": "Body Name"}, {"user": "scalar"}),
    ("body user scalar with query group", {"user": "scalar"}, {"user[name]": "Query Name"}),
    ("query unknown user field", {"user[name]": "Body Name"}, {"user[unknown]": "bar"}),
    ("scalar avatar field", {"user[avatar]": "bogus"}),
    ("query scalar avatar field", {"user[name]": "Body Name"}, {"user[avatar]": "bogus"}),
    ("blank avatar field", {"user[avatar]": ""}),
    ("query blank avatar field", {"user[name]": "Body Name"}, {"user[avatar]": ""}),
)


def profile_row(database):
    with sqlite3.connect(database) as db:
        return db.execute("SELECT name,email_address,bio,password_digest,updated_at FROM users WHERE id=1").fetchone()


def update(port, cookie, csrf, database, fields, query=None):
    before = profile_row(database)
    body = urllib.parse.urlencode({"_method": "patch", **fields}).encode()
    path = "/users/me/profile" + ("?" + urllib.parse.urlencode(query) if query else "")
    status, location, response = request(port, "POST", path, cookie, csrf, body,
                                         "application/x-www-form-urlencoded")
    after = profile_row(database)
    response_body = (len(response), hashlib.sha256(response).hexdigest()) if response else None
    return (status, urllib.parse.urlsplit(location).path if location else None,
            response_body, after[:3], after[3] != before[3], after[3] is not None, after[4] != before[4])


def verify_short_password_sign_in(port):
    opener = browser()
    status, _, page = session_request(opener, port, "/session/new")
    assert status == 200
    status, _, _ = session_request(opener, port, "/session", "POST",
                                   {"email_address": "not-an-email", "password": "x"}, session_token(page))
    assert status == 302, status
    assert session_request(opener, port, "/rooms/1")[0] == 200


def main():
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPOSITORY, text=True).strip() == REVISION
    with tempfile.TemporaryDirectory(prefix="paired-profile-update-edges-") as scratch:
        temp = pathlib.Path(scratch)
        rust_db, camp_db = temp / "rust.sqlite3", temp / "camp.sqlite3"
        rust_port, camp_port, redis_port = free_port(), free_port(), free_port()
        seed_rustfire(rust_db, rust_port, [])
        camp_env = seed_campfire(REPOSITORY, RUBY, BUNDLE, REPOSITORY / "storage/db/production.sqlite3", camp_db, [], camp_port, temp)
        with sqlite3.connect(camp_db) as source, sqlite3.connect(rust_db) as target:
            email, digest = source.execute("SELECT email_address,password_digest FROM users WHERE id=1").fetchone()
            target.execute("UPDATE users SET email_address=?,password_digest=? WHERE id=1", (email, digest))
            source.execute("UPDATE users SET email_address='collision@example.invalid' WHERE id=2")
            target.execute("UPDATE users SET email_address='collision@example.invalid' WHERE id=2")
        checkout = isolated_campfire(temp, redis_port)
        camp_env["REDIS_URL"] = f"redis://127.0.0.1:{redis_port}"
        redis, redis_log = start_redis(temp, redis_port)
        try:
            rust = start_server(rust_db, rust_port)
            with open(temp / "puma.log", "w+") as log:
                camp = subprocess.Popen([str(RUBY), str(RUBY.parent / "bundle"), "exec", "puma", "-C", "config/puma.rb"],
                                        cwd=checkout, env=camp_env, stdout=log, stderr=log)
                try:
                    wait_for_server(camp_port, camp)
                    cookie, csrf = login_campfire(camp_port)
                    results = []
                    for label, fields, *query in CASES:
                        rust_result = update(rust_port, "session_token=benchmark-session", "benchmark-csrf", rust_db, fields, query[0] if query else None)
                        camp_result = update(camp_port, cookie, csrf, camp_db, fields, query[0] if query else None)
                        results.append((label, rust_result, camp_result))
                        if label == "short password":
                            for port in (rust_port, camp_port):
                                verify_short_password_sign_in(port)
                except Exception:
                    log.flush()
                    log.seek(0)
                    print(log.read()[-2500:])
                    raise
                finally:
                    stop_server(camp)
                    stop_server(rust)
        finally:
            redis.terminate()
            redis.wait(timeout=10)
            redis_log.close()
    mismatches = [(label, rust, camp) for label, rust, camp in results if rust != camp]
    for label, rust, camp in mismatches:
        print(f"{label}: Rustfire={rust}, Campfire={camp}")
    assert not mismatches, f"{len(mismatches)} profile update edge cases differ"
    print(f"PASS {len(CASES)} paired profile edge updates match Campfire's responses and saved fields")


if __name__ == "__main__":
    main()
