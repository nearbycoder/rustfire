"""Compare existing direct-room reuse, saved rows, and both participants' live sidebar events.

Requires a release Rustfire build and the pinned Campfire checkout/bundle.
Each app uses a disposable SQLite database; Campfire also uses isolated Redis.
"""

import json
import pathlib
import sqlite3
import subprocess
import tempfile

from direct_lookup import ROOT, free_port, start_server, stop_server
from paired_banned_content import REPOSITORY, RUBY, BUNDLE, REVISION, isolated_campfire, start_redis
from paired_direct_lookup import login_campfire, seed_campfire, seed_rustfire, wait_for_server
from paired_direct_sidebar import SECRET, get_without_redirect


def saved_state(database):
    with sqlite3.connect(database) as db:
        rooms = db.execute("SELECT id,type,name,creator_id,created_at,updated_at FROM rooms ORDER BY id").fetchall()
        memberships = db.execute("SELECT room_id,user_id,involvement FROM memberships ORDER BY room_id,user_id").fetchall()
        indexes = db.execute("SELECT room_id,member_ids FROM direct_room_sets ORDER BY room_id").fetchall() if db.execute("SELECT name FROM sqlite_master WHERE name='direct_room_sets'").fetchone() else None
    return rooms, memberships, indexes


def capture(port, cookie, path):
    return subprocess.Popen(
        ["node", "bench/capture_direct_reuse.mjs", "--base", f"http://127.0.0.1:{port}",
         "--cookie", cookie, "--count", "3", "--output", str(path)],
        cwd=ROOT, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )


def run(port, database, author_cookie, author_csrf, member_cookie, prefix):
    before = saved_state(database)
    assert len(before[0]) == 2 and len(before[1]) == 4, before
    captures = [capture(port, cookie, prefix.with_name(prefix.name + f"-{uid}.json"))
                for uid, cookie in ((1, author_cookie), (2, member_cookie))]
    try:
        for process in captures:
            ready = process.stdout.readline().strip()
            if ready != "READY":
                output, error = process.communicate(timeout=10)
                raise AssertionError(f"Sidebar capture failed: {ready}\n{output}\n{error}")
        from paired_direct_sidebar import create_room_from_form
        redirects = [create_room_from_form(port, "directs", author_cookie, author_csrf, "", (2,)) for _ in range(3)]
        assert redirects == [(302, "/rooms/2")] * 3, redirects
        events = []
        for uid, process in enumerate(captures, 1):
            output, error = process.communicate(timeout=35)
            assert process.returncode == 0, (uid, output, error)
            result = json.loads(prefix.with_name(prefix.name + f"-{uid}.json").read_text())
            assert result["unexpected"] == 0 and len(result["events"]) == 3, result
            events.append(result["events"])
        after = saved_state(database)
        assert after == before, (before, after)
        sidebars = [get_without_redirect(port, "/users/me/sidebar", cookie)[2] for cookie in (author_cookie, member_cookie)]
        assert all(sidebar.count('id="list_rooms_direct_2"') == 1 for sidebar in sidebars)
        return events, redirects
    finally:
        for process in captures:
            if process.poll() is None:
                process.kill()
                process.communicate()


def main():
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPOSITORY, text=True).strip()
    assert revision == REVISION, revision
    with tempfile.TemporaryDirectory(prefix="paired-direct-reuse-") as temporary:
        temp = pathlib.Path(temporary)
        rust_db, camp_db = temp / "rustfire.sqlite3", temp / "campfire.sqlite3"
        rust_port, camp_port, redis_port = free_port(), free_port(), free_port()
        checkout = isolated_campfire(temp, redis_port)
        seed_rustfire(rust_db, rust_port, [(2,)])
        with sqlite3.connect(rust_db) as db:
            db.execute("INSERT INTO sessions(user_id,token,csrf_token,created_at,last_active_at) VALUES(2,'benchmark-member-session','benchmark-member-csrf','2026-01-01T00:00:00Z','2026-01-01T00:00:00Z')")
        camp_env = seed_campfire(checkout, RUBY, BUNDLE, REPOSITORY / "storage/db/production.sqlite3", camp_db, [(2,)], camp_port, temp)
        camp_env["WEB_CONCURRENCY"] = "2"
        camp_env["REDIS_URL"] = f"redis://127.0.0.1:{redis_port}"
        camp_env["SECRET_KEY_BASE"] = SECRET
        with sqlite3.connect(camp_db) as db:
            digest = db.execute("SELECT password_digest FROM users WHERE id=1").fetchone()[0]
            db.execute("UPDATE users SET name='User '||id,updated_at='2026-01-01 00:00:00.000000' WHERE id IN (1,2)")
            db.execute("UPDATE users SET email_address='member@example.invalid',password_digest=? WHERE id=2", [digest])

        rust = start_server(rust_db, rust_port, {"RUSTFIRE_CAMPFIRE_SECRET_KEY_BASE": SECRET})
        try:
            rust_result = run(rust_port, rust_db, "session_token=benchmark-session", "benchmark-csrf",
                              "session_token=benchmark-member-session", temp / "rust")
        finally:
            stop_server(rust)

        redis, redis_log = start_redis(temp, redis_port)
        log = open(temp / "puma.log", "w+")
        camp = None
        try:
            camp = subprocess.Popen([str(RUBY), str(RUBY.parent / "bundle"), "exec", "puma", "-C", "config/puma.rb"],
                                    cwd=checkout, env=camp_env, stdout=log, stderr=log)
            wait_for_server(camp_port, camp)
            author_cookie, author_csrf = login_campfire(camp_port)
            member_cookie, _ = login_campfire(camp_port, "member@example.invalid")
            camp_result = run(camp_port, camp_db, author_cookie, author_csrf, member_cookie, temp / "camp")
        finally:
            if camp is not None:
                stop_server(camp)
            redis.terminate()
            redis.wait(timeout=10)
            redis_log.close()
            log.close()

        assert rust_result == camp_result, "Direct-room reuse events or redirects differ"
        assert all(len(set(user_events)) == 1 for user_events in rust_result[0]), "Repeated event markup changed"
        print("PASS existing direct room reused three times: matching redirects, unchanged rooms and memberships, one sidebar link per member, and six matching Turbo prepends")
        print(json.dumps({"room_id": 2, "requests": 3, "subscribed_members": 2, "events_per_app": 6,
                          "event_bytes": [len(rust_result[0][0][0]), len(rust_result[0][1][0])]}, sort_keys=True))


if __name__ == "__main__":
    main()
