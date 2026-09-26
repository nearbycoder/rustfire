"""Compare live shared and direct room ordering in pinned Campfire and Rustfire."""

import http.client
import json
import pathlib
import shutil
import sqlite3
import subprocess
import tempfile
import time
import urllib.parse
import uuid

from direct_lookup import free_port, start_server, stop_server
from paired_banned_content import BUNDLE, REPOSITORY, REVISION, RUBY, start_redis
from paired_direct_lookup import login_campfire, seed_campfire, seed_rustfire, wait_for_server
from paired_reply_browser import browser


def post_direct_message(port, cookie, csrf):
    body = urllib.parse.urlencode({"message[body]": "Direct room activity", "message[client_message_id]": "sidebar-sort-activity", "authenticity_token": csrf})
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
    try:
        connection.request("POST", "/rooms/2/messages", body, {"Cookie": cookie, "X-CSRF-Token": csrf, "Content-Type": "application/x-www-form-urlencoded", "Accept": "text/vnd.turbo-stream.html, text/html"})
        response = connection.getresponse()
        payload = response.read()
        assert response.status in (200, 201), (response.status, payload[:300])
    finally:
        connection.close()


def check_browser(session, port, database, sender_cookie, sender_csrf):
    browser(session, "open", f"http://127.0.0.1:{port}/session/new")
    assert 'button "Go"' in browser(session, "snapshot", "-i")
    browser(session, "fill", 'input[name="email_address"]', "benchmark@example.invalid")
    browser(session, "fill", 'input[name="password"]', "benchmark-password")
    browser(session, "click", 'button[name="log_in"]')
    browser(session, "wait", "--url", "**/rooms/*")
    browser(session, "open", f"http://127.0.0.1:{port}/rooms/1")
    browser(session, "snapshot", "-i")
    browser(session, "wait", "#shared_rooms a[data-sorted-list-target]")
    browser(session, "wait", "--load", "networkidle")
    browser(session, "wait", "1200")
    direct_before = json.loads(browser(session, "eval", "[...document.querySelectorAll('#direct_rooms > a[data-sorted-list-number]')].map(link=>({room:link.dataset.roomId,number:Number(link.dataset.sortedListNumber)}))"))
    assert [item["room"] for item in direct_before] == ["3", "2"], direct_before
    result = json.loads(browser(session, "eval", """fetch('/rooms/opens', {
      method:'POST',headers:{'X-CSRF-Token':document.querySelector('meta[name="csrf-token"]').content,
      'Content-Type':'application/x-www-form-urlencoded',Accept:'text/html'},
      body:new URLSearchParams({'room[name]':'Zulu'})
    }).then(response=>({status:response.status,url:response.url}))"""))
    assert result["status"] == 200, result
    deadline = time.monotonic() + 8
    new_room_id = None
    while time.monotonic() < deadline:
        with sqlite3.connect(database) as db:
            row = db.execute("SELECT id FROM rooms WHERE name='Zulu'").fetchone()
        if row:
            new_room_id = row[0]
            break
        time.sleep(0.1)
    assert new_room_id is not None
    browser(session, "wait", '#shared_rooms a[data-sorted-list-name="Zulu"]')
    browser(session, "wait", "1200")
    created_order = json.loads(browser(session, "eval", "[...document.querySelectorAll('#shared_rooms > a[data-sorted-list-target]')].map(link=>link.dataset.sortedListName.toLowerCase())"))
    result = json.loads(browser(session, "eval", f"""fetch('/rooms/opens/{new_room_id}', {{
      method:'POST',headers:{{'X-CSRF-Token':document.querySelector('meta[name="csrf-token"]').content,
      'Content-Type':'application/x-www-form-urlencoded',Accept:'text/html'}},
      body:new URLSearchParams({{'_method':'patch','room[name]':'Aardvark'}})
    }}).then(response=>({{status:response.status,url:response.url}}))"""))
    assert result["status"] == 200, result
    browser(session, "wait", '#shared_rooms a[data-sorted-list-name="Aardvark"]')
    browser(session, "wait", "1200")
    renamed_order = json.loads(browser(session, "eval", "[...document.querySelectorAll('#shared_rooms > a[data-sorted-list-target]')].map(link=>link.dataset.sortedListName.toLowerCase())"))
    post_direct_message(port, sender_cookie, sender_csrf)
    browser(session, "wait", "#direct_rooms a[data-room-id='2'].unread")
    direct_after = json.loads(browser(session, "eval", "[...document.querySelectorAll('#direct_rooms > a[data-sorted-list-number]')].map(link=>({room:link.dataset.roomId,number:Number(link.dataset.sortedListNumber)}))"))
    assert not browser(session, "errors").strip(), browser(session, "errors")
    return {"shared_created": created_order, "shared_renamed": renamed_order, "direct_before": direct_before, "direct_after": direct_after}


def main():
    assert shutil.which("agent-browser"), "agent-browser CLI is required"
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPOSITORY, text=True).strip() == REVISION
    sessions = [f"sidebar-sort-rust-{uuid.uuid4().hex[:8]}", f"sidebar-sort-camp-{uuid.uuid4().hex[:8]}"]
    with tempfile.TemporaryDirectory(prefix="paired-sidebar-sort-browser-") as scratch:
        temp = pathlib.Path(scratch)
        rust_db, camp_db = temp / "rust.sqlite3", temp / "camp.sqlite3"
        rust_port, camp_port, redis_port = free_port(), free_port(), free_port()
        seed_rustfire(rust_db, rust_port, [[2], [3]])
        environment = seed_campfire(REPOSITORY, RUBY, BUNDLE, REPOSITORY / "storage/db/production.sqlite3", camp_db, [[2], [3]], camp_port, temp)
        environment["REDIS_URL"] = f"redis://127.0.0.1:{redis_port}"
        with sqlite3.connect(camp_db) as camp:
            password = camp.execute("SELECT password_digest FROM users WHERE id=1").fetchone()[0]
            room_name = camp.execute("SELECT name FROM rooms WHERE id=1").fetchone()[0]
            camp.execute("UPDATE users SET email_address='sender@example.invalid',password_digest=? WHERE id=2", [password])
            camp.execute("UPDATE rooms SET updated_at='2026-01-01 00:00:00.000000' WHERE id=2")
            camp.execute("UPDATE rooms SET updated_at='2026-01-02 00:00:00.000000' WHERE id=3")
        with sqlite3.connect(rust_db) as rust:
            rust.execute("UPDATE users SET email_address='benchmark@example.invalid',password_digest=? WHERE id=1", [password])
            rust.execute("UPDATE rooms SET name=? WHERE id=1", [room_name])
            rust.execute("UPDATE rooms SET updated_at='2026-01-01T00:00:00Z' WHERE id=2")
            rust.execute("UPDATE rooms SET updated_at='2026-01-02T00:00:00Z' WHERE id=3")
            rust.execute("INSERT INTO sessions(user_id,token,csrf_token,created_at,last_active_at) VALUES(2,'sender-session','sender-csrf','2026-01-01T00:00:00Z','2026-01-01T00:00:00Z')")
        redis, redis_log = start_redis(temp, redis_port)
        try:
            rust = start_server(rust_db, rust_port, {"RUSTFIRE_CAMPFIRE_SECRET_KEY_BASE": environment["SECRET_KEY_BASE"]})
            try:
                with open(temp / "puma.log", "w+") as log:
                    camp = subprocess.Popen([str(RUBY), str(RUBY.parent / "bundle"), "exec", "puma", "-C", "config/puma.rb"], cwd=REPOSITORY, env=environment, stdout=log, stderr=log)
                    try:
                        wait_for_server(camp_port, camp)
                        camp_sender_cookie, camp_sender_csrf = login_campfire(camp_port, "sender@example.invalid", "benchmark-password")
                        rust_result = check_browser(sessions[0], rust_port, rust_db, "session_token=sender-session", "sender-csrf")
                        camp_result = check_browser(sessions[1], camp_port, camp_db, camp_sender_cookie, camp_sender_csrf)
                        for key in ("shared_created", "shared_renamed"):
                            assert rust_result[key] == camp_result[key] == sorted(camp_result[key]), (rust_result, camp_result)
                        assert [item["room"] for item in rust_result["direct_after"]] == [item["room"] for item in camp_result["direct_after"]] == ["2", "3"], (rust_result, camp_result)
                        for result in (rust_result, camp_result):
                            before = next(item["number"] for item in result["direct_before"] if item["room"] == "2")
                            after = next(item["number"] for item in result["direct_after"] if item["room"] == "2")
                            assert after > before, (before, after, rust_result, camp_result)
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
    print("PASS live shared-room create/rename and unread direct-room ordering match Campfire in Chromium")


if __name__ == "__main__":
    main()
