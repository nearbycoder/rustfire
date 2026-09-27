"""Check that a live message edit and boost leave another user's draft boost intact."""

import json
import pathlib
import shutil
import sqlite3
import subprocess
import tempfile
import time
import uuid

from direct_lookup import free_port, start_server, stop_server
from paired_banned_content import BUNDLE, REPOSITORY, REVISION, RUBY, start_redis
from paired_boost_controls import request
from paired_direct_lookup import login_campfire, seed_campfire, seed_rustfire, wait_for_server
from paired_reply_browser import browser
from paired_turbo_fanout import seed_boost_message


def seed_logins(rust_db, camp_db):
    with sqlite3.connect(camp_db) as camp:
        digest, name, updated_at = camp.execute(
            "SELECT password_digest,name,updated_at FROM users WHERE id=1"
        ).fetchone()
        member_name, member_updated_at = camp.execute(
            "SELECT name,updated_at FROM users WHERE id=2"
        ).fetchone()
        account = camp.execute("SELECT name,updated_at FROM accounts WHERE id=1").fetchone()
        room_name = camp.execute("SELECT name FROM rooms WHERE id=1").fetchone()[0]
        camp.execute(
            "UPDATE users SET email_address='member@example.test',password_digest=? WHERE id=2",
            (digest,),
        )
    with sqlite3.connect(rust_db) as rust:
        rust.execute(
            "UPDATE users SET email_address='benchmark@example.invalid',password_digest=?,name=?,updated_at=? WHERE id=1",
            (digest, name, updated_at),
        )
        rust.execute(
            "UPDATE users SET email_address='member@example.test',password_digest=?,name=?,updated_at=? WHERE id=2",
            (digest, member_name, member_updated_at),
        )
        rust.execute("UPDATE accounts SET name=?,updated_at=? WHERE id=1", account)
        rust.execute("UPDATE rooms SET name=? WHERE id=1", (room_name,))


def browser_state(session):
    return json.loads(browser(session, "eval", """(() => {
      const message = document.querySelector('#message_boost-fixture');
      const input = message?.querySelector('input[type="text"][name="boost[content]"]');
      return {body: message?.querySelector('[data-reply-target="body"]')?.textContent?.trim(),
              draft: input?.value ?? null,
              boosts: [...(message?.querySelectorAll('.boost-item [data-boost-delete-target="content"]') ?? [])]
                .map(node => node.textContent.trim())};
    })()"""))


def wait_for_state(session, predicate):
    for _ in range(60):
        value = browser_state(session)
        if predicate(value):
            return value
        time.sleep(0.2)
    raise AssertionError(f"Browser state did not arrive: {value}")


def check_app(name, port, database, admin_cookie, admin_csrf, session, screenshot):
    browser(session, "open", f"http://127.0.0.1:{port}/session/new")
    assert 'button "Go"' in browser(session, "snapshot", "-i")
    browser(session, "fill", 'input[name="email_address"]', "member@example.test")
    browser(session, "fill", 'input[name="password"]', "benchmark-password")
    browser(session, "click", 'button[name="log_in"]')
    browser(session, "wait", "--url", "**/rooms/1")
    browser(session, "wait", "#message_boost-fixture")
    browser(session, "wait", "--load", "networkidle")
    browser(session, "screenshot", str(screenshot))
    browser(session, "eval", "document.querySelector('#message_boost-fixture .boost__action').click()")
    for _ in range(50):
        if browser_state(session)["draft"] is not None:
            break
        time.sleep(0.1)
    else:
        raise AssertionError((name, browser(session, "snapshot", "-i")[-1800:]))
    browser(session, "fill", '#message_boost-fixture input[type="text"][name="boost[content]"]', "Hey!")
    initial = browser_state(session)
    assert initial["draft"] == "Hey!", (name, initial)

    status, _, _ = request(port, admin_cookie, "PATCH", "/rooms/1/messages/1", admin_csrf,
                           {"message[body]": "Edited by admin"})
    assert status == 302, (name, "edit", status)
    after_edit = wait_for_state(session, lambda state: state["body"] == "Edited by admin")
    assert after_edit["draft"] == "Hey!", (name, "edit cleared draft", after_edit)

    status, _, _ = request(port, admin_cookie, "POST", "/messages/1/boosts", admin_csrf,
                           {"boost[content]": "Morning"})
    assert status == 302, (name, "boost", status)
    after_boost = wait_for_state(session, lambda state: "Morning" in state["boosts"])
    assert after_boost["draft"] == "Hey!", (name, "boost cleared draft", after_boost)
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT content FROM boosts WHERE message_id=1").fetchall() == [("Morning",)]

    browser(session, "eval", "document.querySelector('#message_boost-fixture form.boost__form button[type=submit]').click()")
    submitted = wait_for_state(session, lambda state: "Hey!" in state["boosts"] and state["draft"] is None)
    assert json.loads(browser(session, "eval", "location.pathname")) == "/rooms/1", (name, "boost left room")
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT content FROM boosts WHERE message_id=1 ORDER BY id").fetchall() == [("Morning",), ("Hey!",)]
    assert not browser(session, "errors").strip(), (name, browser(session, "errors"))
    return initial, after_edit, after_boost, submitted


def main():
    assert shutil.which("agent-browser"), "agent-browser CLI is required"
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPOSITORY, text=True).strip() == REVISION
    sessions = [f"boost-draft-rust-{uuid.uuid4().hex[:8]}", f"boost-draft-camp-{uuid.uuid4().hex[:8]}"]
    with tempfile.TemporaryDirectory(prefix="paired-boost-draft-") as scratch:
        temp = pathlib.Path(scratch)
        rust_db, camp_db = temp / "rust.sqlite3", temp / "camp.sqlite3"
        rust_port, camp_port, redis_port = free_port(), free_port(), free_port()
        seed_rustfire(rust_db, rust_port, [])
        environment = seed_campfire(REPOSITORY, RUBY, BUNDLE, REPOSITORY / "storage/db/production.sqlite3", camp_db, [], camp_port, temp)
        environment["REDIS_URL"] = f"redis://127.0.0.1:{redis_port}"
        seed_boost_message(rust_db, camp_db)
        seed_logins(rust_db, camp_db)
        redis, redis_log = start_redis(temp, redis_port)
        try:
            rust = start_server(rust_db, rust_port, {"RUSTFIRE_CAMPFIRE_SECRET_KEY_BASE": environment["SECRET_KEY_BASE"]})
            try:
                rust_result = check_app("rustfire", rust_port, rust_db, "session_token=benchmark-session",
                                        "benchmark-csrf", sessions[0], temp / "rustfire.png")
            finally:
                stop_server(rust)
            with open(temp / "puma.log", "w+") as log:
                camp = subprocess.Popen([str(RUBY), str(RUBY.parent / "bundle"), "exec", "puma", "-C", "config/puma.rb"],
                                        cwd=REPOSITORY, env=environment, stdout=log, stderr=log)
                try:
                    wait_for_server(camp_port, camp)
                    camp_cookie, camp_csrf = login_campfire(camp_port)
                    camp_result = check_app("campfire", camp_port, camp_db, camp_cookie, camp_csrf,
                                            sessions[1], temp / "campfire.png")
                except Exception:
                    log.flush()
                    log.seek(0)
                    print(log.read()[-3000:])
                    raise
                finally:
                    stop_server(camp)
        finally:
            for session in sessions:
                subprocess.run(["agent-browser", "--session", session, "close"], capture_output=True, timeout=15)
            redis.terminate()
            redis.wait(timeout=10)
            redis_log.close()
    assert rust_result == camp_result, (rust_result, camp_result)
    print("PASS paired browser draft boost survives live edits and submits inline")


if __name__ == "__main__":
    main()
