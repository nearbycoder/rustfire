"""Compare rendered signed mentions on matched disposable Campfire fixtures."""

import html
import http.client
import json
import pathlib
import sqlite3
import subprocess
import tempfile
import urllib.parse

from direct_lookup import free_port, start_server, stop_server
from paired_banned_content import BUNDLE, REPOSITORY, REVISION, RUBY, start_redis
from paired_direct_lookup import login_campfire, seed_campfire, seed_rustfire, wait_for_server
from paired_link_preview import Presentation


def mention_sgid(port, cookie, campfire):
    path = "/autocompletable/users.json?query=" if campfire else "/autocompletable/users?query="
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
    try:
        connection.request("GET", path, headers={"Cookie": cookie, "Accept": "application/json"})
        response = connection.getresponse()
        body = response.read()
        assert response.status == 200, (response.status, body[:300])
        return next(user["sgid"] for user in json.loads(body) if user["value"] == 2)
    finally:
        connection.close()


def post(port, cookie, csrf, sgid, case):
    client_id = f"paired-mention-{case}"
    content = ' content="&lt;span&gt;Untrusted name&lt;/span&gt;"' if case == "embedded-content" else ""
    attachment = f'<action-text-attachment sgid="{html.escape(sgid, quote=True)}" content-type="application/vnd.campfire.mention"{content}></action-text-attachment>'
    message = f"<div>Hello {attachment}!</div>"
    body = urllib.parse.urlencode({"message[body]": message, "message[client_message_id]": client_id, "authenticity_token": csrf})
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
    try:
        connection.request("POST", "/rooms/1/messages", body, {"Cookie": cookie, "Content-Type": "application/x-www-form-urlencoded", "Accept": "text/vnd.turbo-stream.html, text/html"})
        response = connection.getresponse()
        result = response.read()
        assert response.status == 200, (response.status, result[:300])
        parsed = Presentation(client_id)
        parsed.feed(result.decode())
        assert parsed.structure, result[:300]
        return parsed.structure
    finally:
        connection.close()


def main():
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPOSITORY, text=True).strip() == REVISION
    with tempfile.TemporaryDirectory(prefix="paired-mention-") as scratch:
        temp = pathlib.Path(scratch)
        rust_db, camp_db = temp / "rust.sqlite3", temp / "camp.sqlite3"
        rust_port, camp_port, redis_port = free_port(), free_port(), free_port()
        seed_rustfire(rust_db, rust_port, [])
        camp_env = seed_campfire(REPOSITORY, RUBY, BUNDLE, REPOSITORY / "storage/db/production.sqlite3", camp_db, [], camp_port, temp)
        camp_env["REDIS_URL"] = f"redis://127.0.0.1:{redis_port}"
        with sqlite3.connect(camp_db) as camp, sqlite3.connect(rust_db) as rust:
            rust.executemany("UPDATE users SET name=?2,updated_at=?3 WHERE id=?1", camp.execute("SELECT id,name,updated_at FROM users WHERE id IN(1,2)").fetchall())
            rust.execute("UPDATE rooms SET name=? WHERE id=1", [camp.execute("SELECT name FROM rooms WHERE id=1").fetchone()[0]])
            camp.execute("UPDATE users SET bio='Team lead' WHERE id=2")
            rust.execute("UPDATE users SET bio='Team lead' WHERE id=2")
        redis, redis_log = start_redis(temp, redis_port)
        try:
            rust = start_server(rust_db, rust_port, {"RUSTFIRE_CAMPFIRE_SECRET_KEY_BASE": camp_env["SECRET_KEY_BASE"]})
            try:
                rust_cookie, rust_csrf = "session_token=benchmark-session", "benchmark-csrf"
                rust_sgid = mention_sgid(rust_port, rust_cookie, False)
                rust_markup = {case: post(rust_port, rust_cookie, rust_csrf, rust_sgid, case) for case in ("bare", "embedded-content")}
            finally:
                stop_server(rust)
            with open(temp / "puma.log", "w+") as log:
                camp = subprocess.Popen([str(RUBY), str(RUBY.parent / "bundle"), "exec", "puma", "-C", "config/puma.rb"], cwd=REPOSITORY, env=camp_env, stdout=log, stderr=log)
                try:
                    wait_for_server(camp_port, camp)
                    camp_cookie, camp_csrf = login_campfire(camp_port)
                    camp_sgid = mention_sgid(camp_port, camp_cookie, True)
                    camp_markup = {case: post(camp_port, camp_cookie, camp_csrf, camp_sgid, case) for case in ("bare", "embedded-content")}
                finally:
                    stop_server(camp)
        finally:
            redis.terminate()
            redis.wait(timeout=10)
            redis_log.close()
    assert rust_sgid == camp_sgid, (rust_sgid, camp_sgid)
    for case in rust_markup:
        assert rust_markup[case] == camp_markup[case], (case, rust_markup[case], camp_markup[case])
        assert ("a", (("class", "btn avatar"), ("href", "/users/2"), ("title", "Rustfire Compare – Team lead"))) in rust_markup[case]
    print("PASS signed mention SGID and parsed ActionText presentation match pinned Campfire for bare and embedded-content attachments, including user bio")


if __name__ == "__main__":
    main()
