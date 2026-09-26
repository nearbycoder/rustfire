"""Compare Action Cable presence state against pinned ONCE Campfire.

Run after `cargo build --release`. Each server uses a disposable SQLite database.
"""

import json
import pathlib
import sqlite3
import subprocess
import tempfile
from datetime import datetime, timezone

from direct_lookup import ROOT, free_port, start_server, stop_server
from paired_banned_content import REPOSITORY, RUBY, BUNDLE, REVISION, start_redis
from paired_direct_lookup import login_campfire, seed_campfire, seed_rustfire, wait_for_server


def reset_membership(database):
    with sqlite3.connect(database) as db:
        db.execute(
            "UPDATE memberships SET connections=0,connected_at=NULL,unread_at=NULL WHERE room_id=1 AND user_id=1"
        )


def seed_active_membership(database, campfire):
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S" if campfire else "%Y-%m-%dT%H:%M:%SZ")
    with sqlite3.connect(database) as db:
        db.execute("UPDATE memberships SET connections=3,connected_at=? WHERE room_id=1 AND user_id=1", [stamp])


def membership_state(database):
    with sqlite3.connect(database) as db:
        return db.execute("SELECT connections,connected_at FROM memberships WHERE room_id=1 AND user_id=1").fetchone()


def capture(port, cookie, database, mode=None):
    command = [
        "node", "bench/presence_probe.mjs", "--base", f"http://127.0.0.1:{port}",
        "--cookie", cookie, "--database", str(database),
    ]
    if mode:
        command.extend(("--mode", mode))
    process = subprocess.run(
        command,
        cwd=ROOT, text=True, capture_output=True, timeout=30,
    )
    if process.returncode:
        raise RuntimeError(f"Presence probe failed:\n{process.stdout}\n{process.stderr}")
    return json.loads(process.stdout.strip().splitlines()[-1])


def main():
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPOSITORY, text=True).strip()
    assert revision == REVISION, revision
    with tempfile.TemporaryDirectory(prefix="paired-presence-") as scratch:
        temp = pathlib.Path(scratch)
        rust_db, camp_db = temp / "rust.sqlite3", temp / "camp.sqlite3"
        rust_port, camp_port, redis_port = free_port(), free_port(), free_port()
        seed_rustfire(rust_db, rust_port, [])
        camp_env = seed_campfire(REPOSITORY, RUBY, BUNDLE, REPOSITORY / "storage/db/production.sqlite3", camp_db, [], camp_port, temp)
        camp_env["REDIS_URL"] = f"redis://127.0.0.1:{redis_port}"
        seed_active_membership(rust_db, False)
        seed_active_membership(camp_db, True)
        redis, redis_log = start_redis(temp, redis_port)
        try:
            rust = start_server(rust_db, rust_port)
            try:
                rust_startup = membership_state(rust_db)
                reset_membership(rust_db)
                rust_result = capture(rust_port, "session_token=benchmark-session", rust_db)
                rust_stale = capture(rust_port, "session_token=benchmark-session", rust_db, "stale")
            finally:
                stop_server(rust)
            with open(temp / "puma.log", "w+") as log:
                camp = subprocess.Popen(
                    [str(RUBY), str(RUBY.parent / "bundle"), "exec", "puma", "-C", "config/puma.rb"],
                    cwd=REPOSITORY, env=camp_env, stdout=log, stderr=log,
                )
                try:
                    wait_for_server(camp_port, camp)
                    camp_startup = membership_state(camp_db)
                    reset_membership(camp_db)
                    cookie, _ = login_campfire(camp_port)
                    camp_result = capture(camp_port, cookie, camp_db)
                    camp_stale = capture(camp_port, cookie, camp_db, "stale")
                except Exception:
                    log.flush()
                    log.seek(0)
                    print(log.read()[-3000:])
                    raise
                finally:
                    stop_server(camp)
        finally:
            redis.terminate()
            redis.wait(timeout=10)
            redis_log.close()
        assert rust_startup == camp_startup, (rust_startup, camp_startup)
        assert rust_result == camp_result, (rust_result, camp_result)
        assert rust_stale == camp_stale, (rust_stale, camp_stale)
        print("PASS paired Action Cable startup reset, presence subscription, refresh, and stale connection behavior")
        print(json.dumps({"startup": rust_startup, "fresh": rust_result, "stale": rust_stale}, sort_keys=True))


if __name__ == "__main__":
    main()
