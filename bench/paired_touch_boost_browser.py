"""Compare touch boost keyboard handoff with pinned Campfire in emulated Chromium."""

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
from paired_boost_draft_browser import seed_logins
from paired_direct_lookup import seed_campfire, seed_rustfire, wait_for_server
from paired_turbo_fanout import seed_boost_message

TOUCH_EMULATION = pathlib.Path(__file__).with_name("touch_emulation.js")


def browser(session, *arguments):
    result = subprocess.run(
        ["agent-browser", "--session", session, "--init-script", str(TOUCH_EMULATION), *arguments],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode:
        raise RuntimeError(f"agent-browser {arguments!r}: {result.stdout}\n{result.stderr}")
    return result.stdout


def check_app(name, port, session):
    browser(session, "set", "viewport", "390", "844", "3")
    browser(session, "open", f"http://127.0.0.1:{port}/session/new")
    browser(session, "wait", 'input[name="email_address"]')
    browser(session, "fill", 'input[name="email_address"]', "member@example.test")
    browser(session, "fill", 'input[name="password"]', "benchmark-password")
    browser(session, "click", 'button[name="log_in"]')
    browser(session, "wait", "--url", "**/rooms/1")
    browser(session, "wait", "--load", "networkidle")
    initial = json.loads(browser(session, "eval", """(() => {
      const action = document.querySelector('#message_boost-fixture .message__boost-btn');
      action.closest('details').querySelector('summary').click();
      window.__boostInputFocused = false;
      window.__boostScroll = null;
      const scrollIntoView = Element.prototype.scrollIntoView;
      Element.prototype.scrollIntoView = function(options) {
        if (this.matches('#message_boost-fixture [data-controller~="scroll-into-view"]'))
          window.__boostScroll = options;
        return scrollIntoView.call(this, options);
      };
      document.addEventListener('focusin', event => {
        if (event.target.matches('#message_boost-fixture input[name="boost[content]"]'))
          window.__boostInputFocused = true;
      });
      let during = null;
      document.addEventListener('click', () => {
        const fake = document.querySelector('#message_boost-fixture .input--invisible');
        during = {fake: !!fake, focused: document.activeElement === fake};
      }, {once: true});
      action.click();
      return {touch: 'ontouchstart' in window && navigator.maxTouchPoints > 0, during};
    })()"""))
    assert initial == {"touch": True, "during": {"fake": True, "focused": True}}, (name, initial)
    for _ in range(100):
        state = json.loads(browser(session, "eval", """(() => {
          const input = document.querySelector('#message_boost-fixture input[name="boost[content]"]');
          return {fake: !!document.querySelector('#message_boost-fixture .input--invisible'),
                  focused: document.activeElement === input,
                  focusedDuringLoad: window.__boostInputFocused,
                  scroll: window.__boostScroll,
                  form: !!input};
        })()"""))
        if state["form"] and not state["fake"] and state["scroll"]:
            break
        time.sleep(0.1)
    assert not state["fake"] and state["form"] and state["focusedDuringLoad"], (name, state)
    assert state["scroll"] == {"behavior": "smooth", "block": "center"}, (name, state)
    assert not browser(session, "errors").strip(), (name, browser(session, "errors"))
    return initial, state


def main():
    assert shutil.which("agent-browser"), "agent-browser CLI is required"
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPOSITORY, text=True).strip() == REVISION
    sessions = [f"touch-boost-rust-{uuid.uuid4().hex[:8]}", f"touch-boost-camp-{uuid.uuid4().hex[:8]}"]
    with tempfile.TemporaryDirectory(prefix="paired-touch-boost-") as scratch:
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
                rust_result = check_app("rustfire", rust_port, sessions[0])
            finally:
                stop_server(rust)
            with open(temp / "puma.log", "w+") as log:
                camp = subprocess.Popen([str(RUBY), str(RUBY.parent / "bundle"), "exec", "puma", "-C", "config/puma.rb"],
                                        cwd=REPOSITORY, env=environment, stdout=log, stderr=log)
                try:
                    wait_for_server(camp_port, camp)
                    camp_result = check_app("campfire", camp_port, sessions[1])
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
    print("PASS paired touch boost keyboard handoff")


if __name__ == "__main__":
    main()
