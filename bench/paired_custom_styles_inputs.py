"""Compare custom CSS form parameter shapes and saved state with Campfire."""

import hashlib
import pathlib
import sqlite3
import subprocess
import tempfile
import urllib.parse

from direct_lookup import free_port, start_server, stop_server
from paired_banned_content import BUNDLE, REPOSITORY, REVISION, RUBY, isolated_campfire, start_redis
from paired_custom_styles import request, saved_styles
from paired_direct_lookup import login_campfire, seed_campfire, seed_rustfire, wait_for_server


CASES = (
    ("nested", "PATCH", {"account[custom_styles]": ":root { color: red }"}, None),
    ("flat_only", "PATCH", {"custom_styles": "body { color: blue }"}, None),
    ("flat_conflicts", "PATCH", {"custom_styles": "flat", "account[custom_styles]": "nested"}, None),
    ("unknown_nested", "PATCH", {"account[unknown]": "value"}, None),
    ("empty_body", "PATCH", {}, None),
    ("scalar_account", "PATCH", {"account": "value"}, None),
    ("blank_scalar_account", "PATCH", {"account": ""}, None),
    ("array_account", "PATCH", {"account[]": "value"}, None),
    ("blank_css", "PATCH", {"account[custom_styles]": ""}, None),
    ("query_css", "PATCH", {}, {"account[custom_styles]": "query"}),
    ("query_over_body", "PATCH", {"account[custom_styles]": "body"}, {"account[custom_styles]": "query"}),
    ("query_unknown_over_body", "PATCH", {"account[custom_styles]": "body"}, {"account[unknown]": "x"}),
    ("post_override_query", "POST", {"_method": "patch", "account[custom_styles]": "body"}, {"account[custom_styles]": "query-post"}),
)


def exercise(port, database, cookie, csrf):
    results = {}
    for label, method, fields, query in CASES:
        before = saved_styles(database)
        path = "/account/custom_styles" + ("?" + urllib.parse.urlencode(query) if query else "")
        body = urllib.parse.urlencode(fields).encode()
        status, location, response = request(port, method, path, cookie, csrf, body,
                                             "application/x-www-form-urlencoded")
        after = saved_styles(database)
        results[label] = (status, urllib.parse.urlsplit(location).path if location else None,
                          after[0], after[1] != before[1],
                          hashlib.sha256(response).hexdigest() if status != 302 else None)
    return results


def main():
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPOSITORY, text=True).strip() == REVISION
    with tempfile.TemporaryDirectory(prefix="paired-custom-styles-inputs-") as scratch:
        temp = pathlib.Path(scratch)
        rust_db, camp_db = temp / "rust.sqlite3", temp / "camp.sqlite3"
        rust_port, camp_port, redis_port = free_port(), free_port(), free_port()
        seed_rustfire(rust_db, rust_port, [])
        checkout = isolated_campfire(temp, redis_port)
        camp_env = seed_campfire(checkout, RUBY, BUNDLE, REPOSITORY / "storage/db/production.sqlite3", camp_db, [], camp_port, temp)
        camp_env["REDIS_URL"] = f"redis://127.0.0.1:{redis_port}"
        camp_env["WEB_CONCURRENCY"] = "1"
        for database in (rust_db, camp_db):
            with sqlite3.connect(database) as db:
                db.execute("UPDATE accounts SET custom_styles='baseline' WHERE id=1")
        redis, redis_log = start_redis(temp, redis_port)
        try:
            rust = start_server(rust_db, rust_port)
            try:
                rust_results = exercise(rust_port, rust_db, "session_token=benchmark-session", "benchmark-csrf")
            finally:
                stop_server(rust)
            with open(temp / "puma.log", "w+") as log:
                camp = subprocess.Popen([str(RUBY), str(RUBY.parent / "bundle"), "exec", "puma", "-C", "config/puma.rb"],
                                        cwd=checkout, env=camp_env, stdout=log, stderr=log)
                try:
                    wait_for_server(camp_port, camp)
                    cookie, csrf = login_campfire(camp_port)
                    camp_results = exercise(camp_port, camp_db, cookie, csrf)
                finally:
                    stop_server(camp)
        finally:
            redis.terminate()
            redis.wait(timeout=10)
            redis_log.close()
    mismatches = [(label, rust_results[label], camp_results[label]) for label in rust_results
                  if rust_results[label] != camp_results[label]]
    for label, rust, camp in mismatches:
        print(f"{label}: Rustfire={rust} Campfire={camp}")
    assert not mismatches, f"{len(mismatches)} custom CSS input cases differ"
    print(f"PASS {len(CASES)} paired custom CSS input cases")


if __name__ == "__main__":
    main()
