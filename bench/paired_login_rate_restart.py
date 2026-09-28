"""Compare sign-in rate limits before and after restarting each app."""

import pathlib
import sqlite3
import subprocess
import tempfile

from direct_lookup import free_port, start_server, stop_server
from paired_banned_content import BUNDLE, REPOSITORY, REVISION, RUBY, isolated_campfire, start_redis
from paired_direct_lookup import seed_campfire, seed_rustfire, wait_for_server
from paired_join import browser
from paired_session import request, token


def attempt(port):
    opener = browser()
    status, _, page = request(opener, port, "/session/new")
    assert status == 200
    status, location, _ = request(opener, port, "/session", "POST",
                                  {"email_address": "benchmark@example.invalid", "password": "wrong-password"},
                                  token(page))
    return status, location


def exercise(start, port):
    process = start()
    try:
        before = [attempt(port) for _ in range(11)]
    finally:
        stop_server(process)
    process = start()
    try:
        after = attempt(port)
    finally:
        stop_server(process)
    return before, after


def main():
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPOSITORY, text=True).strip() == REVISION
    with tempfile.TemporaryDirectory(prefix="paired-login-rate-restart-") as scratch:
        temp = pathlib.Path(scratch)
        rust_db, camp_db = temp / "rust.sqlite3", temp / "camp.sqlite3"
        rust_port, camp_port, redis_port = free_port(), free_port(), free_port()
        seed_rustfire(rust_db, rust_port, [])
        checkout = isolated_campfire(temp, redis_port)
        camp_env = seed_campfire(checkout, RUBY, BUNDLE, REPOSITORY / "storage/db/production.sqlite3", camp_db, [], camp_port, temp)
        camp_env["REDIS_URL"] = f"redis://127.0.0.1:{redis_port}"
        camp_env["WEB_CONCURRENCY"] = "1"
        with sqlite3.connect(camp_db) as db:
            digest = db.execute("SELECT password_digest FROM users WHERE id=1").fetchone()[0]
        with sqlite3.connect(rust_db) as db:
            db.execute("UPDATE users SET email_address='benchmark@example.invalid',password_digest=? WHERE id=1", [digest])
        redis, redis_log = start_redis(temp, redis_port)
        try:
            def start_rust():
                return start_server(rust_db, rust_port)
            rust = exercise(start_rust, rust_port)

            def start_camp():
                with open(temp / "puma.log", "a") as log:
                    process = subprocess.Popen([str(RUBY), str(RUBY.parent / "bundle"), "exec", "puma", "-C", "config/puma.rb"],
                                               cwd=checkout, env=camp_env, stdout=log, stderr=log)
                try:
                    wait_for_server(camp_port, process)
                except Exception:
                    stop_server(process)
                    raise
                return process
            camp = exercise(start_camp, camp_port)
        finally:
            redis.terminate()
            redis.wait(timeout=10)
            redis_log.close()
    print("Rustfire:", rust)
    print("Campfire:", camp)
    assert rust == camp, "Sign-in rate limit differs across restart"
    assert rust == ([(401, "")] * 10 + [(429, "")], (429, "")), rust
    print("PASS paired sign-in rate limit survives restart")


if __name__ == "__main__":
    main()
