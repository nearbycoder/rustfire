"""Race five first-run requests against fresh Campfire and Rustfire databases."""

import concurrent.futures
import pathlib
import re
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import urllib.parse

from direct_lookup import free_port, start_server, stop_server
from paired_banned_content import start_redis
from paired_direct_lookup import campfire_env, wait_for_server
from paired_first_run import BUNDLE, REVISION, RUBY, SOURCE, fetch, fresh_campfire_database
from paired_join import browser


def race(port, database):
    participants = []
    for index in range(5):
        opener = browser()
        status, _, page = fetch(opener, port, "/first_run")
        assert status == 200, status
        match = re.search(r'name=[\'\"]authenticity_token[\'\"] value=[\'\"]([^\'\"]+)', page)
        assert match, "first-run form missing CSRF token"
        participants.append((opener, match.group(1), index))

    barrier = threading.Barrier(len(participants))

    def submit(participant):
        opener, csrf, index = participant
        body = urllib.parse.urlencode({
            "authenticity_token": csrf,
            "user[name]": f"Attacker {index}",
            "user[email_address]": f"attacker{index}@example.invalid",
            "user[password]": "password123",
        }).encode()
        barrier.wait(timeout=10)
        return fetch(opener, port, "/first_run", body, "application/x-www-form-urlencoded")[:2]

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        responses = list(executor.map(submit, participants))

    with sqlite3.connect(database) as db:
        counts = tuple(db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                       for table in ("accounts", "users", "rooms", "memberships", "sessions"))
        admins = db.execute("SELECT count(*) FROM users WHERE role=1").fetchone()[0]
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
    assert counts == (1, 1, 1, 1, 1) and admins == 1, (counts, admins, responses)
    assert all(status == 302 and path == "/" for status, path in responses), responses
    return counts, admins, responses


def main():
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=SOURCE, text=True).strip() == REVISION
    with tempfile.TemporaryDirectory(prefix="paired-first-run-race-") as scratch:
        temp = pathlib.Path(scratch)
        source_root = temp / "source"
        shutil.copytree(SOURCE, source_root, ignore=shutil.ignore_patterns(".git", "storage", "tmp", "log"))
        (source_root / "storage/files").mkdir(parents=True)
        (source_root / "tmp").mkdir()
        rust_db, camp_db = temp / "rust.sqlite3", temp / "camp.sqlite3"
        rust_port, camp_port, redis_port = free_port(), free_port(), free_port()
        fresh_campfire_database(camp_db)
        env = campfire_env(source_root, RUBY, BUNDLE, camp_db, camp_port, temp)
        env["REDIS_URL"] = f"redis://127.0.0.1:{redis_port}"
        env["WEB_CONCURRENCY"] = "1"
        redis, redis_log = start_redis(temp, redis_port)
        try:
            rust = start_server(rust_db, rust_port, {"RUSTFIRE_CAMPFIRE_SECRET_KEY_BASE": env["SECRET_KEY_BASE"]})
            try:
                rust_result = race(rust_port, rust_db)
            finally:
                stop_server(rust)
            with open(temp / "puma.log", "w+") as log:
                camp = subprocess.Popen([str(RUBY), str(RUBY.parent / "bundle"), "exec", "puma", "-C", "config/puma.rb"], cwd=source_root, env=env, stdout=log, stderr=log)
                try:
                    wait_for_server(camp_port, camp)
                    camp_result = race(camp_port, camp_db)
                except Exception:
                    log.flush()
                    log.seek(0)
                    print(log.read()[-3000:])
                    raise
                finally:
                    stop_server(camp)
            assert rust_result == camp_result, (rust_result, camp_result)
        finally:
            redis.terminate()
            redis.wait(timeout=10)
            redis_log.close()
    print("PASS paired five-request first-run race: one account, admin, room, membership, and session")


if __name__ == "__main__":
    main()
