"""Compare live Action Cable revocation on ban, deactivation, sign-out, and membership removal.

Requires the release Rustfire binary and pinned Campfire bundle. Each action
starts disposable accounts; Campfire uses an isolated Redis server.
"""

import json
import base64
import http.client
import os
import pathlib
import sqlite3
import subprocess
import tempfile
import time
import urllib.parse

from direct_lookup import ROOT, free_port, start_server, stop_server
from paired_banned_content import REPOSITORY, RUBY, BUNDLE, REVISION, isolated_campfire, start_redis
from paired_bot_admin import request
from paired_direct_lookup import login_campfire, seed_campfire, seed_rustfire, wait_for_server


def capture(port, cookie, observe_ms=0):
    command = ["node", "bench/capture_revoked_socket.mjs", "--base", f"http://127.0.0.1:{port}",
               "--cookie", cookie, "--browser-channels", "true"]
    if observe_ms:
        command.extend(("--observe-ms", str(observe_ms)))
    process = subprocess.Popen(
        command,
        cwd=ROOT, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    ready = process.stdout.readline().strip()
    if ready != "READY":
        output, error = process.communicate(timeout=10)
        raise AssertionError(f"Revocation capture did not subscribe: {ready}\n{output}\n{error}")
    return process


def captured(process):
    output, error = process.communicate(timeout=18)
    assert process.returncode == 0, (output, error)
    observed = json.loads(output.strip().splitlines()[-1])
    assert observed["ready"], observed
    return observed


def reconnect_status(port, cookie):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        connection.request("GET", "/cable", headers={
            "Host": f"127.0.0.1:{port}", "Origin": f"http://127.0.0.1:{port}",
            "Upgrade": "websocket", "Connection": "Upgrade",
            "Sec-WebSocket-Version": "13", "Sec-WebSocket-Key": base64.b64encode(os.urandom(16)).decode(),
            "Sec-WebSocket-Protocol": "actioncable-v1-json", "Cookie": cookie,
        })
        response = connection.getresponse()
        return response.status
    finally:
        connection.close()


def rejected_reconnect(port, cookie):
    result = subprocess.run(
        ["node", "bench/capture_revoked_socket.mjs", "--base", f"http://127.0.0.1:{port}",
         "--cookie", cookie, "--unauthorized", "true"],
        cwd=ROOT, text=True, capture_output=True, timeout=20,
    )
    assert result.returncode == 0, (result.stdout, result.stderr)
    return json.loads(result.stdout.strip().splitlines()[-1])


def run(port, database, admin_cookie, admin_csrf, member_cookie, member_csrf, other_cookie, action):
    with sqlite3.connect(database) as db:
        initial_sessions = db.execute("SELECT COUNT(*) FROM sessions WHERE user_id=2").fetchone()[0]
    processes = [capture(port, cookie) for cookie in (member_cookie, other_cookie)]
    admin_monitor = capture(port, admin_cookie, observe_ms=2500)
    try:
        # Action Cable installs its remote-disconnect subscriber asynchronously.
        time.sleep(0.5)
        if action == "ban":
            status, location, response = request(port, "POST", "/users/2/ban", admin_cookie, admin_csrf)
            expected = "/users/2"
        elif action == "deactivate":
            status, location, response = request(port, "DELETE", "/account/users/2", admin_cookie, admin_csrf)
            expected = "/account/edit"
        elif action == "signout":
            status, location, response = request(port, "DELETE", "/session", member_cookie, member_csrf)
            expected = "/"
        else:
            body = urllib.parse.urlencode([("room[name]", "Campfire"), ("user_ids[]", "1")]).encode()
            status, location, response = request(port, "PATCH", "/rooms/closeds/1", admin_cookie, admin_csrf,
                                                 body, "application/x-www-form-urlencoded")
            expected = "/rooms/1"
        assert status == 302 and urllib.parse.urlsplit(location).path == expected, (status, location, response[:200])
        observed = {"member_sockets": [captured(process) for process in processes]}
        assert captured(admin_monitor)["monitor_passed"], "Unrevoked administrator socket closed"
        assert observed["member_sockets"][0] == observed["member_sockets"][1], observed
        reconnect = action in ("signout", "membership")
        for socket in observed["member_sockets"]:
            assert socket["subscribed_channels"] == 4 and socket["messages"] == [
                {"type": "disconnect", "reason": "remote", "reconnect": reconnect}
            ] and socket["close_code"] == 1000 and socket["close_reason"] == "" and socket["tcp_ended"] and socket["socket_error"] is None, socket
        with sqlite3.connect(database) as db:
            saved_status = db.execute("SELECT status FROM users WHERE id=2").fetchone()[0]
            sessions = db.execute("SELECT COUNT(*) FROM sessions WHERE user_id=2").fetchone()[0]
        expected_status = 2 if action == "ban" else 1 if action == "deactivate" else 0
        expected_sessions = initial_sessions if action == "membership" else initial_sessions - 1 if action == "signout" else 0
        assert saved_status == expected_status and sessions == expected_sessions, (saved_status, sessions)
        if action == "membership":
            with sqlite3.connect(database) as db:
                assert db.execute("SELECT COUNT(*) FROM memberships WHERE room_id=1 AND user_id=2").fetchone()[0] == 0
        observed["reconnect"] = [({"http_status": reconnect_status(port, cookie)}
                                  if action == "membership" or (action == "signout" and cookie == other_cookie)
                                  else rejected_reconnect(port, cookie))
                                 for cookie in (member_cookie, other_cookie)]
        observed["admin_stayed_connected"] = True
        return observed
    finally:
        for process in (*processes, admin_monitor):
            if process.poll() is None:
                process.kill()
                process.communicate()


def paired_action(action):
    with tempfile.TemporaryDirectory(prefix=f"paired-{action}-socket-") as temporary:
        temp = pathlib.Path(temporary)
        rust_db, camp_db = temp / "rustfire.sqlite3", temp / "campfire.sqlite3"
        rust_port, camp_port, redis_port = free_port(), free_port(), free_port()
        checkout = isolated_campfire(temp, redis_port)
        seed_rustfire(rust_db, rust_port, [])
        with sqlite3.connect(rust_db) as db:
            db.execute("INSERT INTO sessions(user_id,token,csrf_token,ip_address,created_at,last_active_at) VALUES(2,'paired-member-session','paired-member-csrf','203.0.113.10','2026-01-01T00:00:00Z','2026-01-01T00:00:00Z')")
            db.execute("INSERT INTO sessions(user_id,token,csrf_token,ip_address,created_at,last_active_at) VALUES(2,'paired-other-session','paired-other-csrf','203.0.113.11','2026-01-01T00:00:00Z','2026-01-01T00:00:00Z')")
        camp_env = seed_campfire(checkout, RUBY, BUNDLE, REPOSITORY / "storage/db/production.sqlite3", camp_db, [], camp_port, temp)
        camp_env["WEB_CONCURRENCY"] = "1"
        camp_env["REDIS_URL"] = f"redis://127.0.0.1:{redis_port}"
        with sqlite3.connect(camp_db) as db:
            digest = db.execute("SELECT password_digest FROM users WHERE id=1").fetchone()[0]
            db.execute("UPDATE users SET email_address='member@example.invalid',password_digest=? WHERE id=2", [digest])
            db.execute("DELETE FROM sessions WHERE user_id=2")

        rust = start_server(rust_db, rust_port)
        try:
            rust_observed = run(rust_port, rust_db, "session_token=benchmark-session", "benchmark-csrf",
                                "session_token=paired-member-session", "paired-member-csrf",
                                "session_token=paired-other-session", action)
        finally:
            stop_server(rust)

        redis, redis_log = start_redis(temp, redis_port)
        log = open(temp / "puma.log", "w+")
        camp = None
        try:
            camp = subprocess.Popen([str(RUBY), str(RUBY.parent / "bundle"), "exec", "puma", "-C", "config/puma.rb"],
                                    cwd=checkout, env=camp_env, stdout=log, stderr=log)
            wait_for_server(camp_port, camp)
            member_cookie, member_csrf = login_campfire(camp_port, "member@example.invalid")
            other_cookie, _ = login_campfire(camp_port, "member@example.invalid")
            with sqlite3.connect(camp_db) as db:
                db.execute("UPDATE sessions SET ip_address='203.0.113.10' WHERE user_id=2")
            admin_cookie, admin_csrf = login_campfire(camp_port)
            camp_observed = run(camp_port, camp_db, admin_cookie, admin_csrf, member_cookie, member_csrf,
                                other_cookie, action)
        finally:
            if camp is not None:
                stop_server(camp)
            redis.terminate()
            redis.wait(timeout=10)
            redis_log.close()
            log.close()
    return rust_observed, camp_observed


def main():
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPOSITORY, text=True).strip()
    assert revision == REVISION, revision
    for action in ("ban", "deactivate", "signout", "membership"):
        rust, camp = paired_action(action)
        print(action, json.dumps({"rustfire": rust, "campfire": camp}, sort_keys=True))
        assert rust == camp, f"{action} socket revocation differs"
    print("PASS paired ban, deactivate, sign-out, and membership Action Cable revocation")


if __name__ == "__main__":
    main()
