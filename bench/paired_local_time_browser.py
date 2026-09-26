"""Compare rendered message dates, times, and tooltips with pinned Campfire."""

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


TIMES = (
    (1, "2026-01-01 13:45:12.123456", "2026-01-01T13:45:12.123456Z"),
    (2, "2026-01-02 00:05:06.123456", "2026-01-02T00:05:06.123456Z"),
)


def seed_messages(rust_db, camp_db):
    with sqlite3.connect(camp_db) as camp:
        password, name = camp.execute("SELECT password_digest,name FROM users WHERE id=1").fetchone()
        account = camp.execute("SELECT name,updated_at FROM accounts WHERE id=1").fetchone()
        room_name = camp.execute("SELECT name FROM rooms WHERE id=1").fetchone()[0]
        for message_id, camp_time, _ in TIMES:
            client_id = f"local-time-{message_id}"
            camp.execute("INSERT INTO messages(id,room_id,creator_id,client_message_id,created_at,updated_at) VALUES(?,1,1,?,?,?)", (message_id, client_id, camp_time, camp_time))
            camp.execute("INSERT INTO action_text_rich_texts(name,body,record_type,record_id,created_at,updated_at) VALUES('body',?,'Message',?,?,?)", (f"Time fixture {message_id}", message_id, camp_time, camp_time))
    with sqlite3.connect(rust_db) as rust:
        rust.execute("UPDATE users SET email_address='benchmark@example.invalid',password_digest=?,name=? WHERE id=1", (password, name))
        rust.execute("UPDATE accounts SET name=?,updated_at=? WHERE id=1", account)
        rust.execute("UPDATE rooms SET name=? WHERE id=1", [room_name])
        for message_id, _, rust_time in TIMES:
            client_id = f"local-time-{message_id}"
            rust.execute("INSERT INTO messages(id,room_id,creator_id,body,client_message_id,created_at,updated_at) VALUES(?,1,1,?,?,?,?)", (message_id, f"Time fixture {message_id}", client_id, rust_time, rust_time))


def rendered_times(session, port):
    browser(session, "open", f"http://127.0.0.1:{port}/session/new")
    assert 'button "Go"' in browser(session, "snapshot", "-i")
    browser(session, "fill", 'input[name="email_address"]', "benchmark@example.invalid")
    browser(session, "fill", 'input[name="password"]', "benchmark-password")
    browser(session, "click", 'button[name="log_in"]')
    browser(session, "wait", "--url", "**/rooms/1")
    browser(session, "snapshot", "-i")
    browser(session, "wait", '#message_local-time-1 [data-local-time-target="time"]')
    browser(session, "wait", "--load", "networkidle")
    result = json.loads(browser(session, "eval", """(() => [1,2].map(id => {
      const message=document.querySelector(`#message_local-time-${id}`);
      return Object.fromEntries(['date','time'].map(kind => {
        const node=message?.querySelector(`[data-local-time-target="${kind}"]`);
        return [kind,{text:node?.textContent,title:node?.title,datetime:node?.getAttribute('datetime')}];
      }));
    }))()"""))
    assert all(entry[kind]["text"] and entry[kind]["title"] for entry in result for kind in ("date", "time")), result
    assert not browser(session, "errors").strip(), browser(session, "errors")
    return result


def main():
    assert shutil.which("agent-browser"), "agent-browser CLI is required"
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPOSITORY, text=True).strip() == REVISION
    sessions = [f"local-time-rust-{uuid.uuid4().hex[:8]}", f"local-time-camp-{uuid.uuid4().hex[:8]}"]
    with tempfile.TemporaryDirectory(prefix="paired-local-time-browser-") as scratch:
        temp = pathlib.Path(scratch)
        rust_db, camp_db = temp / "rust.sqlite3", temp / "camp.sqlite3"
        rust_port, camp_port, redis_port = free_port(), free_port(), free_port()
        seed_rustfire(rust_db, rust_port, [])
        environment = seed_campfire(REPOSITORY, RUBY, BUNDLE, REPOSITORY / "storage/db/production.sqlite3", camp_db, [], camp_port, temp)
        environment["REDIS_URL"] = f"redis://127.0.0.1:{redis_port}"
        seed_messages(rust_db, camp_db)
        redis, redis_log = start_redis(temp, redis_port)
        try:
            rust = start_server(rust_db, rust_port, {"RUSTFIRE_CAMPFIRE_SECRET_KEY_BASE": environment["SECRET_KEY_BASE"]})
            try:
                with open(temp / "puma.log", "w+") as log:
                    camp = subprocess.Popen([str(RUBY), str(RUBY.parent / "bundle"), "exec", "puma", "-C", "config/puma.rb"], cwd=REPOSITORY, env=environment, stdout=log, stderr=log)
                    try:
                        wait_for_server(camp_port, camp)
                        rust_times = rendered_times(sessions[0], rust_port)
                        camp_times = rendered_times(sessions[1], camp_port)
                        assert rust_times == camp_times, (rust_times, camp_times)
                        assert all(row["time"]["text"] != row["time"]["title"] for row in rust_times), rust_times
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
    print("PASS two message dates, short times, and date-time tooltips match Campfire in Chromium")


if __name__ == "__main__":
    main()
