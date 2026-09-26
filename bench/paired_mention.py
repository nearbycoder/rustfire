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


def mention_sgid(port, cookie, campfire, user_id=2):
    path = "/autocompletable/users.json?query=" if campfire else "/autocompletable/users?query="
    if user_id == 3:
        path += "User%203"
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
    try:
        connection.request("GET", path, headers={"Cookie": cookie, "Accept": "application/json"})
        response = connection.getresponse()
        body = response.read()
        assert response.status == 200, (response.status, body[:300])
        return next(user["sgid"] for user in json.loads(body) if user["value"] == user_id)
    finally:
        connection.close()


def post(port, cookie, csrf, sgid, case):
    client_id = f"paired-mention-{case}"
    if case == "trix-figure":
        data = html.escape(json.dumps({"contentType": "application/vnd.campfire.mention", "sgid": sgid, "content": "<span>Untrusted name</span>"}), quote=True)
        attachment = f'<figure data-trix-attachment="{data}"><span>Untrusted name</span></figure>'
    else:
        content = ' content="&lt;span&gt;Untrusted name&lt;/span&gt;"' if case == "embedded-content" else ""
        attachment = f'<action-text-attachment sgid="{html.escape(sgid, quote=True)}" content-type="application/vnd.campfire.mention"{content}></action-text-attachment>'
    message = f"<div>Hello {attachment} {attachment}!</div>" if case == "duplicate" else f"<div>Hello {attachment}!</div>"
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
                rust_nonmember_sgid = mention_sgid(rust_port, rust_cookie, False, 3)
                rust_markup = {case: post(rust_port, rust_cookie, rust_csrf, rust_nonmember_sgid if case == "nonmember" else rust_sgid, case) for case in ("bare", "embedded-content", "trix-figure", "duplicate", "nonmember")}
            finally:
                stop_server(rust)
            with open(temp / "puma.log", "w+") as log:
                camp = subprocess.Popen([str(RUBY), str(RUBY.parent / "bundle"), "exec", "puma", "-C", "config/puma.rb"], cwd=REPOSITORY, env=camp_env, stdout=log, stderr=log)
                try:
                    wait_for_server(camp_port, camp)
                    camp_cookie, camp_csrf = login_campfire(camp_port)
                    camp_sgid = mention_sgid(camp_port, camp_cookie, True)
                    camp_nonmember_sgid = mention_sgid(camp_port, camp_cookie, True, 3)
                    camp_markup = {case: post(camp_port, camp_cookie, camp_csrf, camp_nonmember_sgid if case == "nonmember" else camp_sgid, case) for case in ("bare", "embedded-content", "trix-figure", "duplicate", "nonmember")}
                finally:
                    stop_server(camp)
        finally:
            redis.terminate()
            redis.wait(timeout=10)
            redis_log.close()
        with sqlite3.connect(rust_db) as rust, sqlite3.connect(camp_db) as camp:
            rust_search = rust.execute("SELECT body FROM message_search_index ORDER BY rowid").fetchall()
            camp_search = camp.execute("SELECT body FROM message_search_index ORDER BY rowid").fetchall()
            rust_mentions = rust.execute("SELECT m.client_message_id,mm.user_id FROM messages m LEFT JOIN message_mentions mm ON mm.message_id=m.id ORDER BY m.id,mm.user_id").fetchall()
        runner = 'require "json"; puts JSON.generate(Message.order(:id).map { |m| [m.client_message_id, m.mentionees.pluck(:id)] })'
        camp_mentions_output = subprocess.check_output([str(RUBY), str(RUBY.parent / "bundle"), "exec", "rails", "runner", runner], cwd=REPOSITORY, env=camp_env, text=True, stderr=subprocess.PIPE)
        camp_mentions = json.loads(camp_mentions_output.strip().splitlines()[-1])
    assert (rust_sgid, rust_nonmember_sgid) == (camp_sgid, camp_nonmember_sgid)
    for case in rust_markup:
        assert rust_markup[case] == camp_markup[case], (case, rust_markup[case], camp_markup[case])
        if case != "nonmember":
            assert ("a", (("class", "btn avatar"), ("href", "/users/2"), ("title", "Rustfire Compare – Team lead"))) in rust_markup[case]
    expected_search = [("Hello @Rustfire Compare!",)] * 3 + [("Hello @Rustfire Compare @Rustfire Compare!",), ("Hello @User 3!",)]
    assert rust_search == camp_search == expected_search, (rust_search, camp_search)
    rust_mentions = [(client_id, [] if user_id is None else [user_id]) for client_id, user_id in rust_mentions]
    expected_mentions = [[f"paired-mention-{case}", [2] if case != "nonmember" else []] for case in rust_markup]
    assert rust_mentions == [tuple(row) for row in camp_mentions] == [tuple(row) for row in expected_mentions], (rust_mentions, camp_mentions)
    print("PASS signed mention presentation, search text, duplicate recipient deduplication, and nonmember exclusion match pinned Campfire")


if __name__ == "__main__":
    main()
