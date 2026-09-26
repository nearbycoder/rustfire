"""Compare browser push opt-in, denial, and first-run behavior with Campfire."""

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


EXISTING_SUBSCRIPTION_SCRIPT = """(() => {
  window.__existingPushEvents=[];
  Object.defineProperty(window,'Notification',{configurable:true,value:{permission:'granted'}});
  Object.defineProperty(navigator.serviceWorker,'getRegistration',{configurable:true,value:async origin=>{
    window.__existingPushEvents.push(`getRegistration:${origin}`);
    return {pushManager:{getSubscription:async()=>{
      window.__existingPushEvents.push('getSubscription');
      return {endpoint:'https://fcm.googleapis.com/fcm/send/existing-browser'};
    }}};
  }});
})()"""
INSTALL_USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Safari/605.1.15"
STANDALONE_SCRIPT = """(() => {
  const originalMatchMedia=window.matchMedia.bind(window);
  window.matchMedia=query=>query==='(display-mode: standalone)'?{matches:true}:originalMatchMedia(query);
})()"""


def check_install_prompt(session, port, standalone, init_script):
    def install_browser(*arguments):
        result = subprocess.run(
            ["agent-browser", "--session", session, "--user-agent", INSTALL_USER_AGENT, *arguments],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode:
            raise RuntimeError(f"agent-browser install {arguments!r}: {result.stdout}\n{result.stderr}")
        return result.stdout

    command = ["agent-browser", "--session", session, "--user-agent", INSTALL_USER_AGENT]
    if standalone:
        command += ["--init-script", str(init_script)]
    command += ["open", f"http://127.0.0.1:{port}/session/new"]
    result = subprocess.run(command, capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise RuntimeError(f"agent-browser install setup: {result.stdout}\n{result.stderr}")
    install_browser("fill", 'input[name="email_address"]', "benchmark@example.invalid")
    install_browser("fill", 'input[name="password"]', "benchmark-password")
    install_browser("click", 'button[name="log_in"]')
    install_browser("wait", "--url", "**/rooms/*")
    install_browser("wait", "--fn", "!!document.querySelector('.button_to_change_notifying .pwa__instructions[data-controller=pwa-install]')")
    install_browser("wait", "--load", "networkidle")
    before = json.loads(install_browser("eval", """(() => {
      window.__installCalls=0;
      const event=new Event('beforeinstallprompt',{cancelable:true});
      event.prompt=()=>{window.__installCalls++;return Promise.resolve()};
      window.dispatchEvent(event);
      return {prevented:event.defaultPrevented,canInstall:document.querySelector('.button_to_change_notifying .pwa__instructions').classList.contains('pwa--can-install')};
    })()"""))
    if standalone:
        return {"before": before}
    install_browser("eval", """(() => {
      document.querySelector('.button_to_change_notifying [data-notifications-target=notAllowedNotice]').showModal();
      document.querySelector('.button_to_change_notifying .pwa__instructions').open=true;
    })()""")
    install_browser("click", '.button_to_change_notifying [data-action="pwa-install#promptInstall"]')
    install_browser("eval", "window.dispatchEvent(new Event('appinstalled'))")
    after = json.loads(install_browser("eval", """(() => ({
      promptCalls:window.__installCalls,
      canInstall:document.querySelector('.button_to_change_notifying .pwa__instructions').classList.contains('pwa--can-install')
    }))()"""))
    return {"before": before, "after": after}


def check_existing(session, port, init_script):
    result = subprocess.run(
        ["agent-browser", "--session", session, "--init-script", str(init_script), "open", f"http://127.0.0.1:{port}/session/new"],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode:
        raise RuntimeError(f"agent-browser init: {result.stdout}\n{result.stderr}")
    browser(session, "fill", 'input[name="email_address"]', "benchmark@example.invalid")
    browser(session, "fill", 'input[name="password"]', "benchmark-password")
    browser(session, "click", 'button[name="log_in"]')
    browser(session, "wait", "--url", "**/rooms/*")
    browser(session, "wait", "--fn", "!!document.querySelector('.button_to_change_notifying turbo-frame form')")
    state = json.loads(browser(session, "eval", """(() => ({
      events:window.__existingPushEvents,
      bellGone:!document.querySelector('.button_to_change_notifying [data-notifications-target=bell]'),
      settingsForm:!!document.querySelector('.button_to_change_notifying turbo-frame form')
    }))()"""))
    assert state["events"][0] == f"getRegistration:http://127.0.0.1:{port}", state
    state["events"][0] = "getRegistration:<origin>"
    return state


def check_browser(session, port, scenario):
    browser(session, "open", f"http://127.0.0.1:{port}/session/new")
    browser(session, "fill", 'input[name="email_address"]', "benchmark@example.invalid")
    browser(session, "fill", 'input[name="password"]', "benchmark-password")
    browser(session, "click", 'button[name="log_in"]')
    browser(session, "wait", "--url", "**/rooms/*")
    browser(session, "wait", '.button_to_change_notifying [data-notifications-target="bell"]')
    browser(session, "wait", "--load", "networkidle")
    native_registration = json.loads(browser(session, "eval", "(async()=>!!(await navigator.serviceWorker.getRegistration(location.origin)))()"))
    initial_bell = json.loads(browser(session, "eval", """(() => {
      const bell=document.querySelector('.button_to_change_notifying [data-notifications-target=bell]');
      return {pulsing:bell.classList.contains('btn--pulsing'),images:[...bell.querySelectorAll('img')].map(image=>image.hidden)};
    })()"""))
    browser(session, "eval", """(() => {
      window.__pushEvents=[];
      const scenario=%s;
      const subscription={
        toJSON:()=>({endpoint:'https://fcm.googleapis.com/fcm/send/paired-browser',keys:{p256dh:'key',auth:'auth'}}),
        unsubscribe:async()=>{window.__pushEvents.push('unsubscribe');return true}
      };
      const registration={pushManager:{
        getSubscription:async()=>{window.__pushEvents.push('getSubscription');return null},
        subscribe:async()=>{window.__pushEvents.push('subscribe');return subscription}
      }};
      const promptScenario=scenario.startsWith('prompt-');
      Object.defineProperty(window,'Notification',{configurable:true,value:{
        permission:scenario==='pwa-denied'?'denied':promptScenario?'default':'granted',
        requestPermission:async()=>{
          window.__pushEvents.push('requestPermission');
          return scenario==='prompt-denied'?'denied':'granted';
        }
      }});
      if(scenario==='pwa-denied'){
        const originalMatchMedia=window.matchMedia.bind(window);
        window.matchMedia=query=>query==='(display-mode: standalone)'?{matches:true}:originalMatchMedia(query);
      }
      Object.defineProperty(navigator.serviceWorker,'getRegistration',{configurable:true,value:async()=>{
        window.__pushEvents.push('getRegistration');return promptScenario?null:registration;
      }});
      Object.defineProperty(navigator.serviceWorker,'register',{configurable:true,value:async path=>{
        window.__pushEvents.push(`register:${path}`);return registration;
      }});
      const originalFetch=window.fetch.bind(window);
      window.fetch=(input,options)=>{
        if(String(input).includes('/users/me/push_subscriptions')){
          window.__pushEvents.push('post');
          if(scenario==='rejected')return Promise.resolve(new Response('',{status:500}));
        }
        return originalFetch(input,options);
      };
    })()""" % json.dumps(scenario))
    browser(session, "click", '.button_to_change_notifying [data-notifications-target="bell"]')
    if scenario == "rejected":
        browser(session, "wait", "--fn", "window.__pushEvents.includes('unsubscribe')")
    elif scenario in ("success", "prompt-granted"):
        browser(session, "wait", "--fn", "window.__pushEvents.includes('post') && !document.querySelector('.button_to_change_notifying [data-notifications-target=bell]')")
    elif scenario == "pwa-denied":
        browser(session, "wait", "--fn", "document.querySelector('[data-notifications-target=notAllowedNotice]').open")
    else:
        browser(session, "wait", "--fn", "window.__pushEvents.includes('requestPermission') && document.cookie.includes('notifications-first-run-seen=true')")
        browser(session, "wait", "100")
    result = json.loads(browser(session, "eval", """(() => ({
      events:window.__pushEvents,
      dialogOpen:document.querySelector('[data-notifications-target="notAllowedNotice"]')?.open,
      firstRunSeen:document.cookie.includes('notifications-first-run-seen=true'),
      pwaFirstRunSeen:document.cookie.includes('notifications-pwa-first-run-seen=true'),
      bellGone:!document.querySelector('.button_to_change_notifying [data-notifications-target=bell]')
    }))()"""))
    result["nativeRegistration"] = native_registration
    result["initialBell"] = initial_bell
    return result


def main():
    assert shutil.which("agent-browser"), "agent-browser CLI is required"
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPOSITORY, text=True).strip() == REVISION
    scenarios = ("rejected", "success", "pwa-denied", "prompt-granted", "prompt-denied")
    sessions = [f"notification-{scenario}-{app}-{uuid.uuid4().hex[:8]}" for scenario in scenarios for app in ("rust", "camp")]
    sessions += [f"notification-existing-{app}-{uuid.uuid4().hex[:8]}" for app in ("rust", "camp")]
    sessions += [f"notification-install-{mode}-{app}-{uuid.uuid4().hex[:8]}" for mode in ("browser", "standalone") for app in ("rust", "camp")]
    with tempfile.TemporaryDirectory(prefix="paired-notification-browser-") as scratch:
        temp = pathlib.Path(scratch)
        init_script = temp / "existing-push.js"
        init_script.write_text(EXISTING_SUBSCRIPTION_SCRIPT)
        standalone_script = temp / "standalone.js"
        standalone_script.write_text(STANDALONE_SCRIPT)
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
                        for index, scenario in enumerate(scenarios):
                            rust_result = check_browser(sessions[2 * index], rust_port, scenario)
                            camp_result = check_browser(sessions[2 * index + 1], camp_port, scenario)
                            print(json.dumps({"scenario": scenario, "rust": rust_result, "camp": camp_result}, indent=2))
                            assert rust_result == camp_result, f"Browser push {scenario} differs from Campfire"
                        rust_existing = check_existing(sessions[10], rust_port, init_script)
                        camp_existing = check_existing(sessions[11], camp_port, init_script)
                        print(json.dumps({"scenario": "existing", "rust": rust_existing, "camp": camp_existing}, indent=2))
                        assert rust_existing == camp_existing, "Existing browser subscription differs from Campfire"
                        for index, mode in enumerate(("browser", "standalone")):
                            rust_install = check_install_prompt(sessions[12 + index * 2], rust_port, mode == "standalone", standalone_script)
                            camp_install = check_install_prompt(sessions[13 + index * 2], camp_port, mode == "standalone", standalone_script)
                            print(json.dumps({"scenario": f"install-{mode}", "rust": rust_install, "camp": camp_install}, indent=2))
                            assert rust_install == camp_install, f"Install prompt in {mode} differs from Campfire"
                        with sqlite3.connect(rust_db) as db:
                            rust_rows = db.execute("SELECT user_id,endpoint,p256dh_key,auth_key FROM push_subscriptions").fetchall()
                        with sqlite3.connect(camp_db) as db:
                            camp_rows = db.execute("SELECT user_id,endpoint,p256dh_key,auth_key FROM push_subscriptions").fetchall()
                        assert rust_rows == camp_rows == [(1, "https://fcm.googleapis.com/fcm/send/paired-browser", "key", "auth")], (rust_rows, camp_rows)
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
    print("PASS browser push opt-in, existing-subscription loading, and PWA install prompts match Campfire")


if __name__ == "__main__":
    main()
