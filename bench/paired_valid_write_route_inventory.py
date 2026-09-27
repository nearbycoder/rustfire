"""Compare empty-body writes on every Campfire application route with valid CSRF.

Each method/path pair gets fresh disposable databases and servers. This keeps
destructive writes, login/logout, and background side effects from affecting
other cases. The probe checks response shape and common table-count deltas;
it does not assert full semantics for meaningful submitted form fields.
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
from paired_banned_content import BUNDLE, REDIS_CLI, REPOSITORY, REVISION, RUBY, isolated_campfire, start_redis
from paired_direct_lookup import login_campfire, seed_campfire, seed_rustfire, wait_for_server
from paired_unsafe_route_inventory import ERROR_STATUSES, inventory


TABLES = (
    "accounts", "users", "rooms", "memberships", "messages", "boosts",
    "searches", "sessions", "push_subscriptions", "bans",
)


def clone_database(source, destination):
    with sqlite3.connect(source) as original, sqlite3.connect(destination) as copy:
        original.backup(copy)


def counts(database):
    with sqlite3.connect(database) as db:
        return tuple(db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in TABLES)


def request(port, database, cookie, csrf, method, path, accept):
    before = counts(database)
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
    try:
        connection.request(method, path, body=b"", headers={
            "Cookie": cookie,
            "Accept": accept,
            "Content-Type": "application/x-www-form-urlencoded",
            "X-CSRF-Token": csrf,
        })
        response = connection.getresponse()
        body = response.read()
        location = response.getheader("Location")
        target = urllib.parse.urlsplit(location) if location else None
        redirect = target.path + ("?" + target.query if target.query else "") if target else None
        after = counts(database)
        return (
            response.status,
            (response.getheader("Content-Type") or "").split(";", 1)[0],
            redirect,
            (len(body), hashlib.sha256(body).hexdigest()) if response.status in ERROR_STATUSES else None,
            tuple(end - start for start, end in zip(before, after)),
        )
    finally:
        connection.close()


def run_case(case, index, temp, rust_base, camp_base, checkout, base_env, redis_port):
    method, path, accept = case
    subprocess.run([str(REDIS_CLI), "-p", str(redis_port), "FLUSHDB"], check=True, capture_output=True)
    rust_db, camp_db = temp / f"rust-{index}.sqlite3", temp / f"camp-{index}.sqlite3"
    clone_database(rust_base, rust_db)
    clone_database(camp_base, camp_db)
    rust_port, camp_port = free_port(), free_port()
    camp_env = dict(base_env, DATABASE_URL=f"sqlite3:{camp_db}", PORT=str(camp_port), PIDFILE=str(temp / f"puma-{index}.pid"))
    rust = start_server(rust_db, rust_port)
    try:
        with open(temp / f"puma-{index}.log", "w+") as log:
            camp = subprocess.Popen(
                [str(RUBY), str(RUBY.parent / "bundle"), "exec", "puma", "-C", "config/puma.rb"],
                cwd=checkout, env=camp_env, stdout=log, stderr=log,
            )
            try:
                wait_for_server(camp_port, camp)
                camp_cookie, camp_csrf = login_campfire(camp_port)
                rust_result = request(rust_port, rust_db, "session_token=benchmark-session", "benchmark-csrf", method, path, accept)
                camp_result = request(camp_port, camp_db, camp_cookie, camp_csrf, method, path, accept)
                return rust_result, camp_result
            except Exception:
                log.flush()
                log.seek(0)
                print(log.read()[-1800:])
                raise
            finally:
                stop_server(camp)
    finally:
        stop_server(rust)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--all-accepts", action="store_true")
    parser.add_argument("--filter", help="regular expression for METHOD PATH")
    parser.add_argument("--limit", type=int, help="maximum number of selected cases")
    args = parser.parse_args()
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPOSITORY, text=True).strip() == REVISION
    with tempfile.TemporaryDirectory(prefix="paired-valid-write-routes-") as scratch:
        temp = pathlib.Path(scratch)
        rust_base, camp_base = temp / "rust-base.sqlite3", temp / "camp-base.sqlite3"
        seed_rustfire(rust_base, free_port(), [])
        base_env = seed_campfire(REPOSITORY, RUBY, BUNDLE, REPOSITORY / "storage/db/production.sqlite3", camp_base, [], free_port(), temp)
        with sqlite3.connect(camp_base) as db:
            db.execute("DELETE FROM sessions")
        redis_port = free_port()
        checkout = isolated_campfire(temp, redis_port)
        base_env["REDIS_URL"] = f"redis://127.0.0.1:{redis_port}"
        routes = inventory(checkout, base_env)
        if args.filter:
            routes = [(method, path) for method, path in routes if re.search(args.filter, f"{method} {path}")]
        accepts = ("text/html", "application/json", "text/vnd.turbo-stream.html", "*/*") if args.all_accepts else ("text/html",)
        cases = [(method, path, accept) for method, path in routes for accept in accepts]
        if args.limit:
            cases = cases[:args.limit]
        redis, redis_log = start_redis(temp, redis_port)
        try:
            comparisons = []
            for index, case in enumerate(cases):
                rust_result, camp_result = run_case(case, index, temp, rust_base, camp_base, checkout, base_env, redis_port)
                comparisons.append((case, rust_result, camp_result))
                if rust_result != camp_result:
                    print(f"{case}: Rustfire {rust_result}, Campfire {camp_result}", flush=True)
                elif (index + 1) % 10 == 0:
                    print(f"Checked {index + 1}/{len(cases)}", flush=True)
        finally:
            redis.terminate()
            redis.wait(timeout=10)
            redis_log.close()
    mismatches = [item for item in comparisons if item[1] != item[2]]
    print(f"Matched {len(cases) - len(mismatches)}/{len(cases)} valid-CSRF empty-body route cases")
    if mismatches:
        raise AssertionError(f"{len(mismatches)} valid-CSRF routes differ")


if __name__ == "__main__":
    main()
