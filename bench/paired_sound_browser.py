"""Compare live sound-message presentation and playback with pinned Campfire."""

import http.client
import json
import pathlib
import re
import shutil
import sqlite3
import subprocess
import tempfile
import urllib.parse
import uuid

from direct_lookup import free_port, start_server, stop_server
from paired_banned_content import BUNDLE, REPOSITORY, REVISION, RUBY, start_redis
from paired_direct_lookup import login_campfire, seed_campfire, seed_rustfire, wait_for_server
from paired_reply_browser import browser


SOUNDS = ("bell", "56k", "deeper")


def post_sound(port, cookie, csrf, name):
    body = urllib.parse.urlencode({"message[body]": f"/play {name}", "message[client_message_id]": f"sound-{name}", "authenticity_token": csrf})
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
    try:
        connection.request("POST", "/rooms/1/messages", body, {"Cookie": cookie, "X-CSRF-Token": csrf, "Content-Type": "application/x-www-form-urlencoded", "Accept": "text/vnd.turbo-stream.html, text/html"})
        response = connection.getresponse()
        payload = response.read()
        assert response.status in (200, 201), (response.status, payload[:300])
    finally:
        connection.close()


def check_browser(session, port, sender_cookie, sender_csrf):
    browser(session, "open", f"http://127.0.0.1:{port}/session/new")
    browser(session, "fill", 'input[name="email_address"]', "benchmark@example.invalid")
    browser(session, "fill", 'input[name="password"]', "benchmark-password")
    browser(session, "click", 'button[name="log_in"]')
    browser(session, "wait", "--url", "**/rooms/1")
    browser(session, "wait", "#composer trix-editor")
    browser(session, "wait", "--load", "networkidle")
    browser(session, "eval", "window.__playedSounds=[];window.Audio=class{constructor(url){this.url=url}play(){window.__playedSounds.push(this.url);return Promise.resolve()}}")
    result = {}
    for name in SOUNDS:
        post_sound(port, sender_cookie, sender_csrf, name)
        selector = f"#message_sound-{name} .sound"
        browser(session, "wait", selector)
        browser(session, "wait", "300")
        markup = json.loads(browser(session, "eval", f"""(() => {{
          const root=document.querySelector({json.dumps(selector)});
          const tree=node=>node.nodeType===3?['text',node.textContent]:[
            node.tagName.toLowerCase(),[...node.attributes].map(attr=>[attr.name,attr.value]).sort((a,b)=>a[0].localeCompare(b[0])),
            [...node.childNodes].map(tree)];
          return tree(root);
        }})()"""))
        played = json.loads(browser(session, "eval", "window.__playedSounds"))
        assert len(played) == 2 * SOUNDS.index(name) + 1, (name, played)
        browser(session, "click", f"{selector} button")
        after_click = json.loads(browser(session, "eval", "window.__playedSounds"))
        assert len(after_click) == len(played) + 1 and after_click[-1] == played[-1], (name, played, after_click)
        image = json.loads(browser(session, "eval", f"document.querySelector({json.dumps(selector)})?.querySelector('img')?.getAttribute('src')??null"))
        result[name] = {"markup": markup, "url": played[-1], "image": image}
    post_sound(port, sender_cookie, sender_csrf, "unknown")
    browser(session, "wait", "#message_sound-unknown .trix-content")
    ordinary = json.loads(browser(session, "eval", "({text:document.querySelector('#message_sound-unknown .trix-content')?.textContent,sound:!!document.querySelector('#message_sound-unknown .sound'),played:window.__playedSounds.length})"))
    ordinary["text"] = ordinary["text"].strip()
    assert ordinary == {"text": "/play unknown", "sound": False, "played": 2 * len(SOUNDS)}, ordinary
    result["unknown"] = ordinary
    assert not browser(session, "errors").strip(), browser(session, "errors")
    return result


def fetch_asset(port, path):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        connection.request("GET", path)
        response = connection.getresponse()
        body = response.read()
        assert response.status == 200, (path, response.status)
        return response.getheader("Content-Type"), body
    finally:
        connection.close()


def check_asset_manifest():
    source_manifest = json.loads((REPOSITORY / "public/assets/.manifest.json").read_text())
    rust_manifest = json.loads((pathlib.Path(__file__).resolve().parents[1] / "static/sound-assets.json").read_text())
    model = (REPOSITORY / "app/models/sound.rb").read_text()
    expected = {}
    for line in model.splitlines():
        found = re.search(r'new\(name: "([^"]+)"', line)
        if not found:
            continue
        name = found.group(1)
        entry = {"audio": source_manifest[f"{name}.mp3"]["digested_path"]}
        image = re.search(r'image: \{ name: "([^"]+)", width: (\d+), height: (\d+) \}', line)
        if image:
            entry.update(image=source_manifest[f"sounds/{image.group(1)}"]["digested_path"], width=int(image.group(2)), height=int(image.group(3)))
        expected[name] = entry
    assert len(expected) == 56 and rust_manifest == expected, (len(expected), rust_manifest, expected)
    root = pathlib.Path(__file__).resolve().parents[1]
    for entry in expected.values():
        for path in (entry["audio"], entry.get("image")):
            if path:
                assert (root / "static/assets" / path).read_bytes() == (REPOSITORY / "public/assets" / path).read_bytes(), path


def main():
    assert shutil.which("agent-browser"), "agent-browser CLI is required"
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPOSITORY, text=True).strip() == REVISION
    check_asset_manifest()
    sessions = [f"sound-rust-{uuid.uuid4().hex[:8]}", f"sound-camp-{uuid.uuid4().hex[:8]}"]
    with tempfile.TemporaryDirectory(prefix="paired-sound-browser-") as scratch:
        temp = pathlib.Path(scratch)
        rust_db, camp_db = temp / "rust.sqlite3", temp / "camp.sqlite3"
        rust_port, camp_port, redis_port = free_port(), free_port(), free_port()
        seed_rustfire(rust_db, rust_port, [])
        environment = seed_campfire(REPOSITORY, RUBY, BUNDLE, REPOSITORY / "storage/db/production.sqlite3", camp_db, [], camp_port, temp)
        environment["REDIS_URL"] = f"redis://127.0.0.1:{redis_port}"
        with sqlite3.connect(camp_db) as db:
            digest = db.execute("SELECT password_digest FROM users WHERE id=1").fetchone()[0]
            db.execute("UPDATE users SET email_address='sender@example.invalid',password_digest=? WHERE id=2", [digest])
        with sqlite3.connect(rust_db) as db:
            db.execute("UPDATE users SET email_address='benchmark@example.invalid',password_digest=? WHERE id=1", [digest])
            db.execute("INSERT INTO sessions(user_id,token,csrf_token,created_at,last_active_at) VALUES(2,'sender-session','sender-csrf','2026-01-01T00:00:00Z','2026-01-01T00:00:00Z')")
        redis, redis_log = start_redis(temp, redis_port)
        try:
            rust = start_server(rust_db, rust_port, {"RUSTFIRE_CAMPFIRE_SECRET_KEY_BASE": environment["SECRET_KEY_BASE"]})
            try:
                with open(temp / "puma.log", "w+") as log:
                    camp = subprocess.Popen([str(RUBY), str(RUBY.parent / "bundle"), "exec", "puma", "-C", "config/puma.rb"], cwd=REPOSITORY, env=environment, stdout=log, stderr=log)
                    try:
                        wait_for_server(camp_port, camp)
                        camp_sender_cookie, camp_sender_csrf = login_campfire(camp_port, "sender@example.invalid", "benchmark-password")
                        rust_result = check_browser(sessions[0], rust_port, "session_token=sender-session", "sender-csrf")
                        camp_result = check_browser(sessions[1], camp_port, camp_sender_cookie, camp_sender_csrf)
                        assert rust_result == camp_result, (rust_result, camp_result)
                        for name in SOUNDS:
                            assert fetch_asset(rust_port, rust_result[name]["url"]) == fetch_asset(camp_port, camp_result[name]["url"]), name
                            if image := rust_result[name]["image"]:
                                assert fetch_asset(rust_port, image) == fetch_asset(camp_port, image), name
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
    print("PASS live sound markup, arrival and click playback, and source assets match Campfire")


if __name__ == "__main__":
    main()
