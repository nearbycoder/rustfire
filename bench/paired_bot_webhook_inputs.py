"""Compare bot name and webhook URL persistence for direct administrator forms."""

import hashlib
import json
import pathlib
import sqlite3
import subprocess
import tempfile
import urllib.parse

from direct_lookup import free_port, start_server, stop_server
from paired_banned_content import REPOSITORY, RUBY, BUNDLE, REVISION, isolated_campfire, start_redis
from paired_bot_admin import request
from paired_direct_lookup import login_campfire, seed_campfire, seed_rustfire, wait_for_server


CREATE_CASES = [
    ("spaced", "  Spaced Bot  ", " https://example.com/hook "),
    ("blank_hook", "No Hook", ""),
    ("invalid_hook", "Invalid Hook", "not-a-url"),
    ("whitespace_hook", "Whitespace Hook", "   "),
    ("ftp_hook", "FTP Hook", "ftp://example.com/path"),
    ("long_hook", "Long Hook", "https://example.com/" + "a" * 2050),
    ("blank_name", "", "https://example.com/hook"),
    ("missing_hook", "Missing Hook", None),
    ("missing_name", None, "https://example.com/hook"),
]


def saved(database, bot_id):
    with sqlite3.connect(database) as db:
        return db.execute(
            "SELECT u.name,(SELECT url FROM webhooks WHERE user_id=u.id),"
            "(SELECT COUNT(*) FROM webhooks WHERE user_id=u.id) FROM users u WHERE u.id=? AND u.role=2",
            [bot_id],
        ).fetchone()


def submit(port, cookie, csrf, method, path, name, webhook):
    fields = []
    if name is not None:
        fields.append(("user[name]", name))
    if webhook is not None:
        fields.append(("user[webhook_url]", webhook))
    body = urllib.parse.urlencode(fields).encode()
    status, location, response = request(port, method, path, cookie, csrf, body,
                                         "application/x-www-form-urlencoded")
    return status, urllib.parse.urlsplit(location or "").path, response


def exercise(port, database, cookie, csrf):
    cases = {}
    for label, name, webhook in CREATE_CASES:
        with sqlite3.connect(database) as db:
            previous_id = db.execute("SELECT COALESCE(MAX(id),0) FROM users").fetchone()[0]
        status, location, body = submit(port, cookie, csrf, "POST", "/account/bots", name, webhook)
        with sqlite3.connect(database) as db:
            bot_id = db.execute("SELECT MAX(id) FROM users").fetchone()[0]
        row = saved(database, bot_id) if bot_id > previous_id else None
        cases[label] = {"status": status, "location": location, "saved": row,
                        "error_sha256": hashlib.sha256(body).hexdigest() if status != 302 else None}
    with sqlite3.connect(database) as db:
        bot_id = db.execute("SELECT id FROM users WHERE name LIKE '%Spaced Bot%' ORDER BY id LIMIT 1").fetchone()[0]
    for label, name, webhook in [
        ("update_spaced", "  Renamed Bot  ", " ftp://example.com/new "),
        ("update_blank", "  Renamed Bot  ", "   "),
        ("update_missing_name", None, "https://example.com/new"),
    ]:
        status, location, body = submit(port, cookie, csrf, "PATCH", f"/account/bots/{bot_id}", name, webhook)
        cases[label] = {"status": status, "location": location, "saved": saved(database, bot_id),
                        "error_sha256": hashlib.sha256(body).hexdigest() if status != 302 else None}
    return cases


def main():
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPOSITORY, text=True).strip()
    assert revision == REVISION, revision
    with tempfile.TemporaryDirectory(prefix="paired-bot-webhook-inputs-") as temporary:
        temp = pathlib.Path(temporary)
        rust_db, camp_db = temp / "rustfire.sqlite3", temp / "campfire.sqlite3"
        rust_port, camp_port, redis_port = free_port(), free_port(), free_port()
        checkout = isolated_campfire(temp, redis_port)
        seed_rustfire(rust_db, rust_port, [])
        camp_env = seed_campfire(checkout, RUBY, BUNDLE, REPOSITORY / "storage/db/production.sqlite3", camp_db, [], camp_port, temp)
        camp_env["REDIS_URL"] = f"redis://127.0.0.1:{redis_port}"
        camp_env["WEB_CONCURRENCY"] = "1"

        rust = start_server(rust_db, rust_port)
        try:
            rust_cases = exercise(rust_port, rust_db, "session_token=benchmark-session", "benchmark-csrf")
        finally:
            stop_server(rust)

        redis, redis_log = start_redis(temp, redis_port)
        log = open(temp / "puma.log", "w+")
        camp = None
        try:
            camp = subprocess.Popen([str(RUBY), str(RUBY.parent / "bundle"), "exec", "puma", "-C", "config/puma.rb"],
                                    cwd=checkout, env=camp_env, stdout=log, stderr=log)
            wait_for_server(camp_port, camp)
            cookie, csrf = login_campfire(camp_port)
            camp_cases = exercise(camp_port, camp_db, cookie, csrf)
        finally:
            if camp is not None:
                stop_server(camp)
            redis.terminate()
            redis.wait(timeout=10)
            redis_log.close()
            log.close()
    if rust_cases != camp_cases:
        differences = {
            label: {"rustfire": rust_cases[label], "campfire": camp_cases[label]}
            for label in rust_cases if rust_cases[label] != camp_cases[label]
        }
        print(json.dumps(differences, sort_keys=True))
        raise AssertionError("Bot form persistence differs from pinned Campfire")
    print(f"PASS {len(rust_cases)} paired bot name and webhook URL input cases")


if __name__ == "__main__":
    main()
