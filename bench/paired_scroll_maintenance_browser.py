"""Compare room scroll position after a live edit above the viewport."""

import json
import pathlib
import shutil
import sqlite3
import subprocess
import tempfile
import time
import uuid

from direct_lookup import free_port, start_server, stop_server
from paired_banned_content import BUNDLE, REPOSITORY, REVISION, RUBY, isolated_campfire, start_redis
from paired_boost_controls import request
from paired_direct_lookup import seed_campfire, seed_rustfire, wait_for_server
from paired_message_multi import seed_fixture
from paired_reply_browser import browser


def room_state(session, anchor=None):
    argument = json.dumps(anchor)
    return json.loads(browser(session, "eval", f"""(() => {{
      const container = document.querySelector('#message-area .messages');
      if (!container) return null;
      const bounds = container.getBoundingClientRect();
      const messages = [...container.querySelectorAll('.message[data-message-id]')];
      const visible = messages.find(node => node.getBoundingClientRect().top >= bounds.top + 30);
      const anchor = document.getElementById({argument}) || visible;
      const target = document.getElementById('message_multi-seed-1-1');
      return {{anchor:anchor?.id,anchorTop:anchor?.getBoundingClientRect().top,
              scrollTop:container.scrollTop,scrollHeight:container.scrollHeight,
              containerTop:bounds.top,targetBottom:target?.getBoundingClientRect().bottom,
              edited:target?.textContent.includes('Expanded edit marker'),
              boosted:[...(target?.querySelectorAll('.boost-item') || [])].some(node => node.textContent.includes('Above-fold boost')),
              belowEdited:document.getElementById('message_multi-seed-1-40')?.textContent.includes('Below-fold edit marker')}};
    }})()"""))


def check_app(name, port, database, session, cookie, csrf):
    browser(session, "open", f"http://127.0.0.1:{port}/session/new")
    assert 'button "Go"' in browser(session, "snapshot", "-i")
    browser(session, "fill", 'input[name="email_address"]', "benchmark@example.invalid")
    browser(session, "fill", 'input[name="password"]', "benchmark-password")
    browser(session, "click", 'button[name="log_in"]')
    browser(session, "wait", "--url", "**/rooms/1")
    browser(session, "wait", "#message_multi-seed-1-40")
    browser(session, "wait", "--load", "networkidle")
    browser(session, "eval", "document.querySelector('#message-area .messages').scrollTop=1200")
    before = room_state(session)
    assert before["anchor"] and before["targetBottom"] < before["containerTop"], (name, before)

    body = "<div>Expanded edit marker " + ("long message text " * 150) + "</div>"
    status, location, _ = request(port, cookie, "PATCH", "/rooms/1/messages/1", csrf, {"message[body]": body})
    assert status == 302 and location.endswith("/rooms/1/messages/1"), (name, status, location)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        after = room_state(session, before["anchor"])
        if after["edited"]:
            break
        time.sleep(0.1)
    else:
        raise AssertionError((name, "live edit not received", after, browser(session, "errors")))
    assert after["scrollHeight"] > before["scrollHeight"] + 100, (name, before, after)
    edit_result = scroll_result(name, "edit", before, after)
    with sqlite3.connect(database) as db:
        saved = db.execute("SELECT body FROM action_text_rich_texts WHERE record_type='Message' AND record_id=1" if name == "campfire" else "SELECT body FROM messages WHERE id=1").fetchone()[0]
    assert "Expanded edit marker" in saved

    status, location, _ = request(port, cookie, "POST", "/messages/1/boosts", csrf, {"boost[content]": "Above-fold boost"})
    assert status == 302 and location.endswith("/messages/1/boosts"), (name, status, location)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        boosted = room_state(session, before["anchor"])
        if boosted["boosted"]:
            break
        time.sleep(0.1)
    else:
        raise AssertionError((name, "live boost not received", boosted, browser(session, "errors")))
    boost_result = scroll_result(name, "boost", after, boosted)
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT content FROM boosts WHERE message_id=1").fetchone() == ("Above-fold boost",)

    body = "<div>Below-fold edit marker " + ("more long text " * 150) + "</div>"
    status, location, _ = request(port, cookie, "PATCH", "/rooms/1/messages/40", csrf, {"message[body]": body})
    assert status == 302 and location.endswith("/rooms/1/messages/40"), (name, status, location)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        below = room_state(session, before["anchor"])
        if below["belowEdited"]:
            break
        time.sleep(0.1)
    else:
        raise AssertionError((name, "below-fold edit not received", below, browser(session, "errors")))
    below_result = scroll_result(name, "below-fold edit", boosted, below, above=False)
    assert not browser(session, "errors").strip(), (name, browser(session, "errors"))
    return {"edit": edit_result, "boost": boost_result, "below_fold_edit": below_result}


def scroll_result(name, operation, before, after, above=True):
    anchor_delta = after["anchorTop"] - before["anchorTop"]
    scroll_delta = after["scrollTop"] - before["scrollTop"]
    height_delta = after["scrollHeight"] - before["scrollHeight"]
    assert height_delta > 0, (name, operation, before, after)
    expected_scroll_delta = height_delta if above else 0
    assert abs(anchor_delta) <= 2 and abs(scroll_delta - expected_scroll_delta) <= 2, (name, operation, before, after)
    return {"anchor_delta": round(anchor_delta, 2), "scroll_delta": round(scroll_delta, 2), "height_delta": round(height_delta, 2)}


def main():
    assert shutil.which("agent-browser"), "agent-browser CLI is required"
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPOSITORY, text=True).strip() == REVISION
    sessions = [f"scroll-rust-{uuid.uuid4().hex[:8]}", f"scroll-camp-{uuid.uuid4().hex[:8]}"]
    with tempfile.TemporaryDirectory(prefix="paired-scroll-maintenance-") as scratch:
        temp = pathlib.Path(scratch)
        rust_db, camp_db = temp / "rust.sqlite3", temp / "camp.sqlite3"
        rust_port, camp_port, redis_port = free_port(), free_port(), free_port()
        seed_rustfire(rust_db, rust_port, [])
        camp_env = seed_campfire(REPOSITORY, RUBY, BUNDLE, REPOSITORY / "storage/db/production.sqlite3", camp_db, [], camp_port, temp)
        checkout = isolated_campfire(temp, redis_port)
        camp_env["REDIS_URL"] = f"redis://127.0.0.1:{redis_port}"
        seed_fixture(rust_db, camp_db, 1, 2)
        with sqlite3.connect(camp_db) as camp, sqlite3.connect(rust_db) as rust:
            digest, name, updated_at = camp.execute("SELECT password_digest,name,updated_at FROM users WHERE id=1").fetchone()
            rust.execute("UPDATE users SET email_address='benchmark@example.invalid',password_digest=?,name=?,updated_at=? WHERE id=1", (digest, name, updated_at))
        redis, redis_log = start_redis(temp, redis_port)
        try:
            rust = start_server(rust_db, rust_port, {"RUSTFIRE_CAMPFIRE_SECRET_KEY_BASE": camp_env["SECRET_KEY_BASE"]})
            try:
                rust_result = check_app("rustfire", rust_port, rust_db, sessions[0], "session_token=benchmark-session", "benchmark-csrf")
            finally:
                stop_server(rust)
            with open(temp / "puma.log", "w+") as log:
                camp = subprocess.Popen([str(RUBY), str(RUBY.parent / "bundle"), "exec", "puma", "-C", "config/puma.rb"], cwd=checkout, env=camp_env, stdout=log, stderr=log)
                try:
                    wait_for_server(camp_port, camp)
                    from paired_direct_lookup import login_campfire
                    cookie, csrf = login_campfire(camp_port)
                    camp_result = check_app("campfire", camp_port, camp_db, sessions[1], cookie, csrf)
                except Exception:
                    log.flush()
                    log.seek(0)
                    print(log.read()[-2000:])
                    raise
                finally:
                    stop_server(camp)
        finally:
            for session in sessions:
                subprocess.run(["agent-browser", "--session", session, "close"], capture_output=True, timeout=15)
            redis.terminate()
            redis.wait(timeout=10)
            redis_log.close()
    print(json.dumps({"rustfire": rust_result, "campfire": camp_result}, sort_keys=True))


if __name__ == "__main__":
    main()
