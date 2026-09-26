"""Compare the large-account room member filter in pinned Campfire and Rustfire."""

import json
import pathlib
import shutil
import sqlite3
import subprocess
import tempfile
import uuid

from direct_lookup import free_port, start_server, stop_server
from paired_banned_content import BUNDLE, REPOSITORY, REVISION, RUBY, start_redis
from paired_direct_lookup import seed_campfire, seed_rustfire, wait_for_server
from paired_reply_browser import browser


def seed_users(rust_db, camp_db):
    with sqlite3.connect(camp_db) as camp:
        password = camp.execute("SELECT password_digest FROM users WHERE id=1").fetchone()[0]
        for user_id, name in ((3, "Alpha"), (4, "Alpine"), (5, "Bruno")):
            camp.execute("UPDATE users SET name=? WHERE id=?", (name, user_id))
        names = camp.execute("SELECT id,name FROM users ORDER BY id").fetchall()
        camp.execute("UPDATE users SET email_address='benchmark@example.invalid' WHERE id=1")
    with sqlite3.connect(rust_db) as rust:
        for user_id, name in names:
            rust.execute("UPDATE users SET name=? WHERE id=?", (name, user_id))
        rust.execute("UPDATE users SET email_address='benchmark@example.invalid',password_digest=? WHERE id=1", [password])


def filter_states(session, port):
    browser(session, "open", f"http://127.0.0.1:{port}/session/new")
    assert 'button "Go"' in browser(session, "snapshot", "-i")
    browser(session, "fill", 'input[name="email_address"]', "benchmark@example.invalid")
    browser(session, "fill", 'input[name="password"]', "benchmark-password")
    browser(session, "click", 'button[name="log_in"]')
    browser(session, "wait", "--url", "**/rooms/1")
    def state():
        return json.loads(browser(session, "eval", """(() => {
          const list=document.querySelector('[data-filter-target="list"]');
          const rows=[...list.querySelectorAll('li[data-value]')];
          return {total:rows.length,active:list.classList.contains('filter--active'),
            selected:rows.filter(row=>row.classList.contains('selected')).map(row=>row.dataset.value),
            hidden:rows.filter(row=>row.hidden).map(row=>row.dataset.value),
            visible:rows.filter(row=>getComputedStyle(row).display!=='none').map(row=>row.dataset.value)};
        })()"""))
    result = {}
    for room_kind in ("closeds", "opens"):
        browser(session, "open", f"http://127.0.0.1:{port}/rooms/{room_kind}/new")
        browser(session, "snapshot", "-i")
        browser(session, "wait", '#search[data-action*="filter#filter"]')
        browser(session, "wait", "--load", "networkidle")
        states = {"initial": state()}
        assert states["initial"]["total"] > 20, states
        for label, value in (("match", "alp"), ("case", "ALP"), ("none", "nomatch"), ("clear", "")):
            browser(session, "fill", "#search", value)
            browser(session, "wait", "450")
            states[label] = state()
        result[room_kind] = states
    assert not browser(session, "errors").strip(), browser(session, "errors")
    return result


def main():
    assert shutil.which("agent-browser"), "agent-browser CLI is required"
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPOSITORY, text=True).strip() == REVISION
    sessions = [f"room-filter-rust-{uuid.uuid4().hex[:8]}", f"room-filter-camp-{uuid.uuid4().hex[:8]}"]
    with tempfile.TemporaryDirectory(prefix="paired-room-filter-browser-") as scratch:
        temp = pathlib.Path(scratch)
        rust_db, camp_db = temp / "rust.sqlite3", temp / "camp.sqlite3"
        rust_port, camp_port, redis_port = free_port(), free_port(), free_port()
        seed_rustfire(rust_db, rust_port, [])
        environment = seed_campfire(REPOSITORY, RUBY, BUNDLE, REPOSITORY / "storage/db/production.sqlite3", camp_db, [], camp_port, temp)
        environment["REDIS_URL"] = f"redis://127.0.0.1:{redis_port}"
        seed_users(rust_db, camp_db)
        redis, redis_log = start_redis(temp, redis_port)
        try:
            rust = start_server(rust_db, rust_port, {"RUSTFIRE_CAMPFIRE_SECRET_KEY_BASE": environment["SECRET_KEY_BASE"]})
            try:
                with open(temp / "puma.log", "w+") as log:
                    camp = subprocess.Popen([str(RUBY), str(RUBY.parent / "bundle"), "exec", "puma", "-C", "config/puma.rb"], cwd=REPOSITORY, env=environment, stdout=log, stderr=log)
                    try:
                        wait_for_server(camp_port, camp)
                        rust_states = filter_states(sessions[0], rust_port)
                        camp_states = filter_states(sessions[1], camp_port)
                        assert rust_states == camp_states, (rust_states, camp_states)
                    finally:
                        stop_server(camp)
            finally:
                stop_server(rust)
        finally:
            for session in sessions:
                subprocess.run(["agent-browser", "--session", session, "close"], capture_output=True, timeout=15)
            redis.terminate()
            redis.wait(timeout=10)
            redis_log.close()
    print("PASS large-account open/private room member filter classes and visible rows match Campfire in Chromium")


if __name__ == "__main__":
    main()
