"""Compare Campfire and Rustfire bot message and boost write APIs."""

import http.client
import json
import pathlib
import sqlite3
import subprocess
import tempfile
import urllib.parse

from direct_lookup import free_port, start_server, stop_server
from paired_banned_content import BUNDLE, REPOSITORY, REVISION, RUBY, isolated_campfire, start_redis
from paired_direct_lookup import login_campfire, seed_campfire, seed_rustfire, wait_for_server
from paired_mention_webhook import BOT_ID, BOT_TOKEN, bot_index, seed_bot


def request(port, method, path, body=b"", *, cookie="", csrf="", content_type="text/plain", accept="application/json"):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    headers = {"Accept": accept}
    if cookie:
        headers["Cookie"] = cookie
        headers["X-CSRF-Token"] = csrf
    if body:
        headers["Content-Type"] = content_type
    try:
        connection.request(method, path, body, headers)
        response = connection.getresponse()
        return response.status, response.getheader("Location"), response.read()
    finally:
        connection.close()


def normalized_json(data):
    value = json.loads(data)

    def normalize(item):
        if isinstance(item, dict):
            return {
                key: normalize(val)
                for key, val in item.items()
                if key not in ("created_at",)
            }
        if isinstance(item, list):
            return [normalize(val) for val in item]
        if isinstance(item, str) and item.startswith(("http://", "https://")):
            url = urllib.parse.urlsplit(item)
            return url.path + ("?" + url.query if url.query else "")
        return item

    return normalize(value)


def workflow(port, cookie, csrf, database):
    base = f"/rooms/1/{BOT_ID}-{BOT_TOKEN}/messages"
    results = {}

    invalid_base = "/rooms/1/invalid-bot-key/messages"
    for label, method, path, content in (
        ("invalid_key_get", "GET", invalid_base, b""),
        ("invalid_key_post", "POST", invalid_base, b"Hello"),
        ("invalid_key_patch", "PATCH", f"{invalid_base}/1", b"Hello"),
        ("invalid_key_delete", "DELETE", f"{invalid_base}/1", b""),
        ("invalid_key_boost", "POST", f"{invalid_base}/1/boosts", b"Nice"),
    ):
        status, location, body = request(port, method, path, content)
        results[label] = (status, urllib.parse.urlsplit(location or "").path)

    for label, content in (("empty_message", b""), ("blank_message", b"   ")):
        status, _, body = request(port, "POST", base, content)
        results[label] = status

    browser_body = urllib.parse.urlencode({"message[body]": "Human origin", "message[client_message_id]": "paired-bot-mutation-human", "authenticity_token": csrf})
    status, _, body = request(port, "POST", "/rooms/1/messages", browser_body, cookie=cookie, csrf=csrf, content_type="application/x-www-form-urlencoded", accept="text/vnd.turbo-stream.html, text/html")
    assert status == 200, (status, body[:300])

    status, location, body = request(port, "POST", base, "Deploying...".encode())
    results["create"] = (status, urllib.parse.urlsplit(location or "").path, body)
    assert status == 201, results["create"]
    with sqlite3.connect(database) as db:
        human_id = db.execute("SELECT id FROM messages WHERE creator_id=1 ORDER BY id DESC LIMIT 1").fetchone()[0]
        bot_id = db.execute("SELECT id FROM messages WHERE creator_id=52 ORDER BY id DESC LIMIT 1").fetchone()[0]
    results["after_create"] = bot_index(port, 1)

    status, _, body = request(port, "PATCH", f"{base}/{bot_id}", "Deployed 🚀!".encode())
    results["update"] = (status, normalized_json(body))
    status, _, body = request(port, "PATCH", f"{base}/{human_id}", b"Hijacked!")
    results["forbidden_update"] = status

    boost_path = f"{base}/{human_id}/boosts"
    status, _, body = request(port, "POST", boost_path, "  Nice 👀  ".encode())
    results["create_boost"] = (status, normalized_json(body))
    assert status == 201, (status, body[:300])
    boost_id = results["create_boost"][1]["id"]
    for label, content in (("empty_boost", b""), ("blank_boost", b"   ")):
        status, _, body = request(port, "POST", boost_path, content)
        results[label] = status
    with sqlite3.connect(database) as db:
        results["saved_boost"] = db.execute("SELECT content FROM boosts WHERE id=?", [boost_id]).fetchone()[0]
    status, _, body = request(port, "DELETE", f"{boost_path}/{boost_id}")
    results["delete_boost"] = (status, body)

    status, _, body = request(port, "DELETE", f"{base}/{human_id}")
    results["forbidden_delete"] = status
    status, _, body = request(port, "DELETE", f"{base}/{bot_id}")
    results["delete_message"] = (status, body)
    results["after_delete"] = bot_index(port, 1)
    with sqlite3.connect(database) as db:
        results["saved_rows"] = (db.execute("SELECT count(*) FROM messages").fetchone()[0], db.execute("SELECT count(*) FROM boosts").fetchone()[0])
    return results


def main():
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPOSITORY, text=True).strip() == REVISION
    with tempfile.TemporaryDirectory(prefix="paired-bot-mutations-") as scratch:
        temp = pathlib.Path(scratch)
        rust_db, camp_db = temp / "rust.sqlite3", temp / "camp.sqlite3"
        rust_port, camp_port, redis_port = free_port(), free_port(), free_port()
        seed_rustfire(rust_db, rust_port, [])
        camp_env = seed_campfire(REPOSITORY, RUBY, BUNDLE, REPOSITORY / "storage/db/production.sqlite3", camp_db, [], camp_port, temp)
        checkout = isolated_campfire(temp, redis_port)
        camp_env["REDIS_URL"] = f"redis://127.0.0.1:{redis_port}"
        seed_bot(rust_db, False, "", with_webhooks=False)
        seed_bot(camp_db, True, "", with_webhooks=False)
        with sqlite3.connect(camp_db) as camp, sqlite3.connect(rust_db) as rust:
            rust.executemany("UPDATE users SET name=?2,updated_at=?3 WHERE id=?1", camp.execute("SELECT id,name,updated_at FROM users WHERE id IN(1,2)").fetchall())
            rust.execute("UPDATE rooms SET name=? WHERE id=1", [camp.execute("SELECT name FROM rooms WHERE id=1").fetchone()[0]])
        redis, redis_log = start_redis(temp, redis_port)
        try:
            rust = start_server(rust_db, rust_port, {"RUSTFIRE_CAMPFIRE_SECRET_KEY_BASE": camp_env["SECRET_KEY_BASE"], "RUSTFIRE_DISABLE_WEBHOOKS": "1"})
            try:
                rust_results = workflow(rust_port, "session_token=benchmark-session", "benchmark-csrf", rust_db)
            finally:
                stop_server(rust)
            with open(temp / "puma.log", "w+") as log:
                camp = subprocess.Popen([str(RUBY), str(RUBY.parent / "bundle"), "exec", "puma", "-C", "config/puma.rb"], cwd=checkout, env=camp_env, stdout=log, stderr=log)
                try:
                    wait_for_server(camp_port, camp)
                    cookie, csrf = login_campfire(camp_port)
                    camp_results = workflow(camp_port, cookie, csrf, camp_db)
                finally:
                    stop_server(camp)
        finally:
            redis.terminate()
            redis.wait(timeout=10)
            redis_log.close()
    differences = {key: (rust_results[key], camp_results[key]) for key in rust_results if rust_results[key] != camp_results[key]}
    assert not differences, differences
    print("PASS paired bot message and boost create, update, authorization, and delete match pinned Campfire")


if __name__ == "__main__":
    main()
