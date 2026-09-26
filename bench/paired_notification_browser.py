"""Compare failed browser push opt-in behavior with pinned Campfire."""

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


def check_browser(session, port):
    browser(session, "open", f"http://127.0.0.1:{port}/session/new")
    browser(session, "fill", 'input[name="email_address"]', "benchmark@example.invalid")
    browser(session, "fill", 'input[name="password"]', "benchmark-password")
    browser(session, "click", 'button[name="log_in"]')
    browser(session, "wait", "--url", "**/rooms/*")
    browser(session, "wait", '.button_to_change_notifying [data-notifications-target="bell"]')
    browser(session, "wait", "--load", "networkidle")
    browser(session, "eval", """(() => {
      window.__pushEvents=[];
      const subscription={
        toJSON:()=>({endpoint:'https://fcm.googleapis.com/fcm/send/paired-browser',keys:{p256dh:'key',auth:'auth'}}),
        unsubscribe:async()=>{window.__pushEvents.push('unsubscribe');return true}
      };
      const registration={pushManager:{
        getSubscription:async()=>{window.__pushEvents.push('getSubscription');return null},
        subscribe:async()=>{window.__pushEvents.push('subscribe');return subscription}
      }};
      Object.defineProperty(window,'Notification',{configurable:true,value:{permission:'granted'}});
      Object.defineProperty(navigator.serviceWorker,'getRegistration',{configurable:true,value:async()=>{
        window.__pushEvents.push('getRegistration');return registration;
      }});
      const originalFetch=window.fetch.bind(window);
      window.fetch=(input,options)=>{
        if(String(input).includes('/users/me/push_subscriptions')){
          window.__pushEvents.push('post');
          return Promise.resolve(new Response('',{status:500}));
        }
        return originalFetch(input,options);
      };
    })()""")
    browser(session, "click", '.button_to_change_notifying [data-notifications-target="bell"]')
    browser(session, "wait", "--fn", "window.__pushEvents.includes('unsubscribe')")
    result = json.loads(browser(session, "eval", """(() => ({
      events:window.__pushEvents,
      dialogOpen:document.querySelector('[data-notifications-target="notAllowedNotice"]')?.open,
      firstRunSeen:document.cookie.includes('notifications-first-run-seen=true')
    }))()"""))
    return result


def main():
    assert shutil.which("agent-browser"), "agent-browser CLI is required"
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPOSITORY, text=True).strip() == REVISION
    sessions = [f"notification-rust-{uuid.uuid4().hex[:8]}", f"notification-camp-{uuid.uuid4().hex[:8]}"]
    with tempfile.TemporaryDirectory(prefix="paired-notification-browser-") as scratch:
        temp = pathlib.Path(scratch)
        rust_db, camp_db = temp / "rust.sqlite3", temp / "camp.sqlite3"
        rust_port, camp_port, redis_port = free_port(), free_port(), free_port()
        seed_rustfire(rust_db, rust_port, [])
        environment = seed_campfire(REPOSITORY, RUBY, BUNDLE, REPOSITORY / "storage/db/production.sqlite3", camp_db, [], camp_port, temp)
        environment["REDIS_URL"] = f"redis://127.0.0.1:{redis_port}"
        with sqlite3.connect(camp_db) as db:
            digest = db.execute("SELECT password_digest FROM users WHERE id=1").fetchone()[0]
        with sqlite3.connect(rust_db) as db:
            db.execute("UPDATE users SET email_address='benchmark@example.invalid',password_digest=? WHERE id=1", [digest])
        redis, redis_log = start_redis(temp, redis_port)
        try:
            rust = start_server(rust_db, rust_port, {"RUSTFIRE_CAMPFIRE_SECRET_KEY_BASE": environment["SECRET_KEY_BASE"]})
            try:
                with open(temp / "puma.log", "w+") as log:
                    camp = subprocess.Popen([str(RUBY), str(RUBY.parent / "bundle"), "exec", "puma", "-C", "config/puma.rb"], cwd=REPOSITORY, env=environment, stdout=log, stderr=log)
                    try:
                        wait_for_server(camp_port, camp)
                        rust_result = check_browser(sessions[0], rust_port)
                        camp_result = check_browser(sessions[1], camp_port)
                        print(json.dumps({"rust": rust_result, "camp": camp_result}, indent=2))
                        assert rust_result == camp_result, "Browser push opt-in differs from Campfire"
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
    print("PASS failed browser push opt-in matches Campfire")


if __name__ == "__main__":
    main()
