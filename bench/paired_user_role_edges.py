"""Compare account-user role form parameters with pinned Campfire."""

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


CASES = (
    ("administrator", {"user[role]": "administrator"}),
    ("member", {"user[role]": "member"}),
    ("unknown role", {"user[role]": "owner"}),
    ("blank role", {"user[role]": ""}),
    ("unknown nested field", {"user[other]": "ignored"}),
    ("flat role", {"role": "administrator"}),
    ("flat overrides nested", {"role": "member", "user[role]": "administrator"}),
    ("nested overrides flat", {"role": "administrator", "user[role]": "member"}),
    ("empty body", {}),
    ("scalar user", {"user": "administrator"}),
    ("blank scalar user", {"user": ""}),
    ("array user", {"user[]": "administrator"}),
    ("query role", {}, {"user[role]": "administrator"}),
    ("query replaces body role", {"user[role]": "administrator"}, {"user[role]": "member"}),
    ("query unknown user field", {"user[role]": "administrator"}, {"user[other]": "x"}),
    ("query scalar user", {"user[role]": "administrator"}, {"user": "scalar"}),
)


def state(database):
    with sqlite3.connect(database) as db:
        role, stamp = db.execute("SELECT role,updated_at FROM users WHERE id=2").fetchone()
    return role, stamp


def update(port, cookie, csrf, database, fields, query=None):
    before = state(database)
    path = "/account/users/2" + ("?" + urllib.parse.urlencode(query) if query else "")
    body = urllib.parse.urlencode(fields).encode()
    status, location, response = request(port, "PATCH", path, cookie, csrf, body,
                                         "application/x-www-form-urlencoded")
    after = state(database)
    return (status, urllib.parse.urlsplit(location).path if location else None,
            (len(response), hashlib.sha256(response).hexdigest()) if response else None,
            after[0], after[1] != before[1])


def main():
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPOSITORY, text=True).strip() == REVISION
    with tempfile.TemporaryDirectory(prefix="paired-user-role-edges-") as scratch:
        temp = pathlib.Path(scratch)
        rust_db, camp_db = temp / "rust.sqlite3", temp / "camp.sqlite3"
        rust_port, camp_port, redis_port = free_port(), free_port(), free_port()
        seed_rustfire(rust_db, rust_port, [])
        camp_env = seed_campfire(REPOSITORY, RUBY, BUNDLE, REPOSITORY / "storage/db/production.sqlite3", camp_db, [], camp_port, temp)
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
                        actual = update(rust_port, "session_token=benchmark-session", "benchmark-csrf", rust_db,
                                        fields, query[0] if query else None)
                        expected = update(camp_port, cookie, csrf, camp_db,
                                          fields, query[0] if query else None)
                        results.append((label, actual, expected))
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
    mismatches = [(label, actual, expected) for label, actual, expected in results if actual != expected]
    for label, actual, expected in mismatches:
        print(f"{label}: Rustfire={actual}, Campfire={expected}")
    assert not mismatches, f"{len(mismatches)} account-user role forms differ"
    print(f"PASS {len(CASES)} paired account-user role forms match Campfire")


if __name__ == "__main__":
    main()
