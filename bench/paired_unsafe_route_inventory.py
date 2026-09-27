"""Compare every pinned Campfire application write route with Rustfire.

Requests use an invalid CSRF token and disposable databases. Bot-key paths use
an invalid key. This checks route recognition and rejection, not valid writes.
"""

import argparse
import hashlib
import http.client
import pathlib
import re
import sqlite3
import subprocess
import tempfile
import urllib.parse

from direct_lookup import free_port, start_server, stop_server
from paired_banned_content import BUNDLE, REPOSITORY, REVISION, RUBY, isolated_campfire, start_redis
from paired_direct_lookup import login_campfire, seed_campfire, seed_rustfire, wait_for_server


VERBS = {"POST", "PATCH", "PUT", "DELETE"}
ERROR_STATUSES = {403, 404, 406, 422, 500}


def inventory(checkout, environment):
    routes = subprocess.check_output(
        [str(RUBY), str(RUBY.parent / "bundle"), "exec", "bin/rails", "routes"],
        cwd=checkout, env=environment, text=True,
    )
    cases = set()
    for line in routes.splitlines():
        match = re.search(r"\b(POST|PATCH|PUT|DELETE)\s+(\S+)\s+\S+#\S+", line)
        if not match:
            continue
        method, path = match.groups()
        if method not in VERBS or path.startswith("/rails/"):
            continue
        path = path.replace("(.:format)", "")
        for name, value in (
            (":user_id", "1"), (":room_id", "1"),
            (":message_id", "1"), (":push_subscription_id", "1"), (":join_code", "invalid"),
            (":bot_key", "invalid"), (":bot_id", "1"), (":id", "1"),
        ):
            path = path.replace(name, value)
        cases.add((method, path))
    return sorted(cases)


def request(port, cookie, method, path, accept, compare_error_bodies):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
    try:
        connection.request(method, path, body=b"", headers={
            "Cookie": cookie,
            "Accept": accept,
            "Content-Type": "application/x-www-form-urlencoded",
            "X-CSRF-Token": "invalid-csrf-token",
        })
        response = connection.getresponse()
        body = response.read()
        kind = (response.getheader("Content-Type") or "").split(";", 1)[0]
        location = response.getheader("Location")
        target = urllib.parse.urlsplit(location) if location else None
        redirect = target.path + ("?" + target.query if target.query else "") if target else None
        result = (response.status, kind, redirect)
        if compare_error_bodies:
            result += ((len(body), hashlib.sha256(body).hexdigest()) if response.status in ERROR_STATUSES else None,)
        return result
    finally:
        connection.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--all-accepts", action="store_true")
    parser.add_argument("--compare-error-bodies", action="store_true")
    parser.add_argument("--role", choices=("admin", "member", "anonymous"), default="admin")
    args = parser.parse_args()
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPOSITORY, text=True).strip() == REVISION
    with tempfile.TemporaryDirectory(prefix="paired-unsafe-route-inventory-") as scratch:
        temp = pathlib.Path(scratch)
        rust_db, camp_db = temp / "rust.sqlite3", temp / "camp.sqlite3"
        rust_port, camp_port, redis_port = free_port(), free_port(), free_port()
        seed_rustfire(rust_db, rust_port, [])
        camp_env = seed_campfire(REPOSITORY, RUBY, BUNDLE, REPOSITORY / "storage/db/production.sqlite3", camp_db, [], camp_port, temp)
        if args.role == "member":
            with sqlite3.connect(rust_db) as db:
                db.execute("DELETE FROM sessions")
                db.execute("INSERT INTO sessions(user_id,token,csrf_token,created_at,last_active_at) VALUES(2,'benchmark-member-session','benchmark-member-csrf','2026-01-01T00:00:00Z','2026-01-01T00:00:00Z')")
            with sqlite3.connect(camp_db) as db:
                db.execute("UPDATE users SET email_address='member@example.invalid',password_digest=(SELECT password_digest FROM users WHERE id=1) WHERE id=2")
        checkout = isolated_campfire(temp, redis_port)
        camp_env["REDIS_URL"] = f"redis://127.0.0.1:{redis_port}"
        routes = inventory(checkout, camp_env)
        accepts = ("text/html", "application/json", "text/vnd.turbo-stream.html", "*/*") if args.all_accepts else ("text/html",)
        cases = [(method, path, accept) for method, path in routes for accept in accepts]
        redis, redis_log = start_redis(temp, redis_port)
        try:
            rust = start_server(rust_db, rust_port)
            try:
                rust_cookie = {
                    "admin": "session_token=benchmark-session",
                    "member": "session_token=benchmark-member-session",
                    "anonymous": "",
                }[args.role]
                rust_results = {case: request(rust_port, rust_cookie, *case, args.compare_error_bodies) for case in cases}
            finally:
                stop_server(rust)
            with open(temp / "puma.log", "w+") as log:
                camp = subprocess.Popen(
                    [str(RUBY), str(RUBY.parent / "bundle"), "exec", "puma", "-C", "config/puma.rb"],
                    cwd=checkout, env=camp_env, stdout=log, stderr=log,
                )
                try:
                    wait_for_server(camp_port, camp)
                    cookie = login_campfire(camp_port, email="member@example.invalid" if args.role == "member" else "benchmark@example.invalid")[0] if args.role != "anonymous" else ""
                    camp_results = {case: request(camp_port, cookie, *case, args.compare_error_bodies) for case in cases}
                finally:
                    stop_server(camp)
        finally:
            redis.terminate()
            redis.wait(timeout=10)
            redis_log.close()
    mismatches = [(case, rust_results[case], camp_results[case]) for case in cases if rust_results[case] != camp_results[case]]
    for case, rust_result, camp_result in mismatches:
        print(f"{case}: Rustfire {rust_result}, Campfire {camp_result}")
    print(f"Matched {len(cases) - len(mismatches)}/{len(cases)} {args.role} invalid-CSRF write-route cases")
    if mismatches:
        raise AssertionError(f"{len(mismatches)} route cases differ")


if __name__ == "__main__":
    main()
