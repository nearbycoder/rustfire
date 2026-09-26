"""Compare self-demotion and self-deactivation of the only administrator."""

import pathlib
import sqlite3
import subprocess
import tempfile
import urllib.parse

from direct_lookup import free_port, start_server, stop_server
from paired_banned_content import start_redis
from paired_bot_admin import request
from paired_direct_lookup import login_campfire, seed_campfire, seed_rustfire, wait_for_server


REPOSITORY = pathlib.Path("/tmp/once-campfire-reference")
RUBY = pathlib.Path("/tmp/rustfire-baseline/local/bin/ruby")
BUNDLE = pathlib.Path("/tmp/rustfire-baseline/bundle")
REVISION = "91d294f4a09f9bbe37f9548959bfcb43645678fb"


def state(database):
    with sqlite3.connect(database) as db:
        return (
            db.execute("SELECT role,status FROM users WHERE id=1").fetchone(),
            db.execute("SELECT count(*) FROM sessions WHERE user_id=1").fetchone()[0],
            db.execute("SELECT count(*) FROM memberships WHERE user_id=1 AND room_id=1").fetchone()[0],
        )


def exercise(port, cookie, csrf, database):
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT count(*) FROM users WHERE role=1 AND status=0").fetchone() == (1,)
    updates = {}
    for label, fields in (
        ("invalid_role", {"user[role]": "manager"}),
        ("missing_role", {"user[name]": "Ignored"}),
        ("top_level_role", {"role": "member"}),
        ("self_demote", {"user[role]": "member"}),
    ):
        body = urllib.parse.urlencode({**fields, "authenticity_token": csrf}).encode()
        status, location, _ = request(port, "PUT", "/account/users/1", cookie, csrf, body, "application/x-www-form-urlencoded")
        updates[label] = status, urllib.parse.urlsplit(location or "").path, state(database)
        if label == "self_demote":
            blocked_body = urllib.parse.urlencode({"user[role]": "administrator", "authenticity_token": csrf}).encode()
            blocked_status, _, _ = request(port, "PUT", "/account/users/2", cookie, csrf, blocked_body, "application/x-www-form-urlencoded")
            updates["demoted_admin_denied"] = blocked_status
        with sqlite3.connect(database) as db:
            db.execute("UPDATE users SET role=1 WHERE id=1")
    status, location, _ = request(port, "DELETE", "/account/users/1", cookie, csrf)
    deactivated = (status, urllib.parse.urlsplit(location or "").path, state(database))
    return updates, deactivated


def main():
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPOSITORY, text=True).strip() == REVISION
    with tempfile.TemporaryDirectory(prefix="paired-last-admin-") as scratch:
        temp = pathlib.Path(scratch)
        rust_db, camp_db = temp / "rust.sqlite3", temp / "camp.sqlite3"
        rust_port, camp_port, redis_port = free_port(), free_port(), free_port()
        seed_rustfire(rust_db, rust_port, [])
        camp_env = seed_campfire(REPOSITORY, RUBY, BUNDLE, REPOSITORY / "storage/db/production.sqlite3", camp_db, [], camp_port, temp)
        with sqlite3.connect(camp_db) as db:
            db.execute("DELETE FROM sessions")
        camp_env["REDIS_URL"] = f"redis://127.0.0.1:{redis_port}"
        camp_env["WEB_CONCURRENCY"] = "1"
        redis, redis_log = start_redis(temp, redis_port)
        try:
            rust = start_server(rust_db, rust_port, {"RUSTFIRE_CAMPFIRE_SECRET_KEY_BASE": camp_env["SECRET_KEY_BASE"]})
            try:
                rust_result = exercise(rust_port, "session_token=benchmark-session", "benchmark-csrf", rust_db)
            finally:
                stop_server(rust)
            with open(temp / "puma.log", "w+") as log:
                camp = subprocess.Popen([str(RUBY), str(RUBY.parent / "bundle"), "exec", "puma", "-C", "config/puma.rb"], cwd=REPOSITORY, env=camp_env, stdout=log, stderr=log)
                try:
                    wait_for_server(camp_port, camp)
                    cookie, csrf = login_campfire(camp_port)
                    camp_result = exercise(camp_port, cookie, csrf, camp_db)
                finally:
                    stop_server(camp)
        finally:
            redis.terminate()
            redis.wait(timeout=10)
            redis_log.close()
    assert rust_result == camp_result, (rust_result, camp_result)
    for label in ("invalid_role", "missing_role", "self_demote"):
        assert rust_result[0][label][:2] == (302, "/account/edit") and rust_result[0][label][2][0] == (0, 0), (label, rust_result)
    assert rust_result[0]["top_level_role"][:2] == (400, "") and rust_result[0]["top_level_role"][2][0] == (1, 0), rust_result
    assert rust_result[0]["demoted_admin_denied"] == 403, rust_result
    assert rust_result[1][:2] == (302, "/account/edit") and rust_result[1][2][0] == (1, 1), rust_result
    print("PASS paired only-administrator role inputs, self-demotion, and self-deactivation")


if __name__ == "__main__":
    main()
