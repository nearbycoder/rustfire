"""Compare account-settings update edge inputs with pinned Campfire."""

import hashlib
import json
import pathlib
import sqlite3
import subprocess
import tempfile
import urllib.parse

from direct_lookup import free_port, start_server, stop_server
from paired_banned_content import BUNDLE, REPOSITORY, REVISION, RUBY, isolated_campfire, start_redis
from paired_bot_admin import request
from paired_direct_lookup import login_campfire, seed_campfire, seed_rustfire, wait_for_server


SETTING = "account[settings][restrict_room_creation_to_administrators]"
CASES = (
    ("padded name", {"account[name]": "  Team Name  "}),
    ("blank name", {"account[name]": ""}),
    ("setting yes", {SETTING: "yes"}),
    ("setting blank", {SETTING: ""}),
    ("setting arbitrary", {SETTING: "something"}),
    ("setting uppercase false", {SETTING: "FALSE"}),
    ("setting false", {SETTING: "false"}),
    ("setting one", {SETTING: "1"}),
    ("setting zero", {SETTING: "0"}),
    ("setting off", {SETTING: "off"}),
    ("unknown field", {"account[unknown]": "ignored"}),
    ("flat name", {"name": "Ignored"}),
    ("flat setting", {"restrict_room_creation": "1"}),
    ("blank account group", {"account": ""}),
    ("scalar account group", {"account": "unexpected"}),
    ("missing account", {}),
)


def state(database, campfire):
    with sqlite3.connect(database) as db:
        name = db.execute("SELECT name FROM accounts WHERE id=1").fetchone()[0]
        if campfire:
            settings = json.loads(db.execute("SELECT settings FROM accounts WHERE id=1").fetchone()[0])
            restriction = bool(settings.get("restrict_room_creation_to_administrators", False))
        else:
            restriction = bool(db.execute("SELECT restrict_room_creation FROM account_settings WHERE id=1").fetchone()[0])
    return name, restriction


def update(port, cookie, csrf, database, campfire, fields):
    body = urllib.parse.urlencode(fields).encode()
    status, location, response = request(port, "PATCH", "/account", cookie, csrf, body,
                                         "application/x-www-form-urlencoded")
    response_body = (len(response), hashlib.sha256(response).hexdigest()) if response else None
    return status, urllib.parse.urlsplit(location).path if location else None, response_body, state(database, campfire)


def main():
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPOSITORY, text=True).strip() == REVISION
    with tempfile.TemporaryDirectory(prefix="paired-account-update-edges-") as scratch:
        temp = pathlib.Path(scratch)
        rust_db, camp_db = temp / "rust.sqlite3", temp / "camp.sqlite3"
        rust_port, camp_port, redis_port = free_port(), free_port(), free_port()
        seed_rustfire(rust_db, rust_port, [])
        camp_env = seed_campfire(REPOSITORY, RUBY, BUNDLE, REPOSITORY / "storage/db/production.sqlite3", camp_db, [], camp_port, temp)
        with sqlite3.connect(camp_db) as source, sqlite3.connect(rust_db) as target:
            target.execute("UPDATE accounts SET name=? WHERE id=1", [source.execute("SELECT name FROM accounts WHERE id=1").fetchone()[0]])
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
                    for label, fields in CASES:
                        rust_result = update(rust_port, "session_token=benchmark-session", "benchmark-csrf", rust_db, False, fields)
                        camp_result = update(camp_port, cookie, csrf, camp_db, True, fields)
                        results.append((label, rust_result, camp_result))
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
    assert not mismatches, f"{len(mismatches)} account update edge cases differ"
    print(f"PASS {len(CASES)} paired account update edge cases match Campfire")


if __name__ == "__main__":
    main()
