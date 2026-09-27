"""Compare Campfire bot write-route suffix and Accept behavior with saved effects."""

import http.client
import hashlib
import json
import pathlib
import sqlite3
import subprocess
import tempfile
from urllib.parse import urlsplit

from direct_lookup import free_port, start_server, stop_server
from paired_banned_content import BUNDLE, REPOSITORY, REVISION, RUBY, isolated_campfire, start_redis
from paired_direct_lookup import seed_campfire, seed_rustfire, wait_for_server
from paired_mention_webhook import BOT_ID, BOT_TOKEN, seed_bot


FORMATS = (
    "", ".json", ".html", ".turbo_stream", ".xml", ".bogus",
    "?format=json", "?format=html", "?format=xml", "?format=bogus",
)
ACCEPTS = ("application/json", "text/html", "text/vnd.turbo-stream.html")


def request(port, method, path, body=b"", accept="application/json"):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    try:
        connection.request(method, path, body, {"Accept": accept, "Content-Type": "text/plain"})
        response = connection.getresponse()
        return response.status, response.getheader("Content-Type", ""), response.getheader("Location", ""), response.read()
    finally:
        connection.close()


def response_shape(result):
    status, media, location, body = result
    target = urlsplit(location).path
    if target.startswith("/messages/"):
        target = "/messages/ID"
    if media.startswith("application/json") and body and status < 400:
        payload = json.loads(body)
        detail = sorted(payload) if isinstance(payload, dict) else type(payload).__name__
    elif status >= 400:
        detail = hashlib.sha256(body).hexdigest()
    else:
        detail = len(body)
    return status, media, target, detail


def latest_message(db_path):
    with sqlite3.connect(db_path) as db:
        return db.execute("SELECT max(id) FROM messages").fetchone()[0]


def latest_boost(db_path):
    with sqlite3.connect(db_path) as db:
        return db.execute("SELECT max(id) FROM boosts").fetchone()[0]


def message_text(port, base, message_id):
    result = request(port, "GET", base)
    assert result[0] == 200, response_shape(result)
    for item in json.loads(result[3]):
        if item["id"] == message_id:
            return item["body"]["plain_text"]
    return None


def boost_exists(db_path, boost_id):
    with sqlite3.connect(db_path) as db:
        return bool(db.execute("SELECT 1 FROM boosts WHERE id=?", (boost_id,)).fetchone())


def workflow(port, db_path):
    base = f"/rooms/1/{BOT_ID}-{BOT_TOKEN}/messages"
    results = {}
    serial = 0

    def create_fixture():
        nonlocal serial
        serial += 1
        result = request(port, "POST", base, f"fixture-{serial}".encode())
        assert result[0] == 201, response_shape(result)
        return latest_message(db_path)

    for suffix in FORMATS:
        for accept in (ACCEPTS if not suffix else ("application/json",)):
            for method in ("PATCH", "PUT"):
                label = ("update", method, suffix, accept)
                message_id = create_fixture()
                result = request(port, method, f"{base}/{message_id}{suffix}", b"Changed", accept)
                results[label] = response_shape(result), message_text(port, base, message_id)

            label = ("delete", suffix, accept)
            message_id = create_fixture()
            result = request(port, "DELETE", f"{base}/{message_id}{suffix}", accept=accept)
            results[label] = response_shape(result), message_text(port, base, message_id)

            label = ("boost_create", suffix, accept)
            message_id = create_fixture()
            before = latest_boost(db_path)
            result = request(port, "POST", f"{base}/{message_id}/boosts{suffix}", b"Boosted", accept)
            after = latest_boost(db_path)
            results[label] = response_shape(result), after != before

            label = ("boost_delete", suffix, accept)
            result = request(port, "POST", f"{base}/{message_id}/boosts", b"Remove this")
            assert result[0] == 201, response_shape(result)
            boost_id = latest_boost(db_path)
            result = request(port, "DELETE", f"{base}/{message_id}/boosts/{boost_id}{suffix}", accept=accept)
            results[label] = response_shape(result), boost_exists(db_path, boost_id)

    for suffix in FORMATS:
        for accept in (ACCEPTS if not suffix else ("application/json",)):
            label = ("create", suffix, accept)
            before = latest_message(db_path)
            result = request(port, "POST", base + suffix, b"New from suffix", accept)
            results[label] = response_shape(result), latest_message(db_path) != before
    return results


def main():
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPOSITORY, text=True).strip() == REVISION
    with tempfile.TemporaryDirectory(prefix="paired-bot-write-formats-") as scratch:
        temp = pathlib.Path(scratch)
        rust_db, camp_db = temp / "rust.sqlite3", temp / "camp.sqlite3"
        rust_port, camp_port, redis_port = free_port(), free_port(), free_port()
        seed_rustfire(rust_db, rust_port, [])
        camp_env = seed_campfire(REPOSITORY, RUBY, BUNDLE, REPOSITORY / "storage/db/production.sqlite3", camp_db, [], camp_port, temp)
        checkout = isolated_campfire(temp, redis_port)
        camp_env["REDIS_URL"] = f"redis://127.0.0.1:{redis_port}"
        seed_bot(rust_db, False, "", with_webhooks=False)
        seed_bot(camp_db, True, "", with_webhooks=False)
        redis, redis_log = start_redis(temp, redis_port)
        try:
            rust = start_server(rust_db, rust_port, {"RUSTFIRE_CAMPFIRE_SECRET_KEY_BASE": camp_env["SECRET_KEY_BASE"], "RUSTFIRE_DISABLE_WEBHOOKS": "1"})
            try:
                rust_results = workflow(rust_port, rust_db)
            finally:
                stop_server(rust)
            with open(temp / "puma.log", "w+") as log:
                camp = subprocess.Popen([str(RUBY), str(RUBY.parent / "bundle"), "exec", "puma", "-C", "config/puma.rb"], cwd=checkout, env=camp_env, stdout=log, stderr=log)
                try:
                    wait_for_server(camp_port, camp)
                    camp_results = workflow(camp_port, camp_db)
                except Exception:
                    log.flush()
                    log.seek(0)
                    print(log.read()[-3000:])
                    raise
                finally:
                    stop_server(camp)
        finally:
            redis.terminate()
            redis.wait(timeout=10)
            redis_log.close()
    differences = {key: (rust_results[key], camp_results[key]) for key in rust_results if rust_results[key] != camp_results[key]}
    for key, values in differences.items():
        print(key, values)
    assert not differences, f"{len(differences)} bot write-format differences"
    print(f"PASS {len(rust_results)} paired bot write-format cases")


if __name__ == "__main__":
    main()
