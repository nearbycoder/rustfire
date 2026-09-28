"""Compare raw and multipart bot message bodies with the pinned Campfire app."""

import hashlib
import http.client
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


CASES = (
    ("empty_raw", b"", "text/plain"),
    ("space_raw", b" \t\n", "text/plain"),
    ("text_raw", b"Hello bot", "text/plain"),
    ("utf8_raw", "Café 🔥".encode(), "text/plain"),
    ("invalid_utf8_raw", b"\xff", "text/plain"),
    ("invalid_utf8_after_text", b"Hello \xff", "text/plain"),
    ("html_raw", b"<b>Rendered?</b>", "text/html"),
    ("form_raw", b"body=Form+contents", "application/x-www-form-urlencoded"),
    ("file_only", (("attachment", b"a file\n", "note.txt", "text/plain"),), "multipart"),
    ("file_and_body", (("body", b"Caption"), ("attachment", b"a file\n", "note.txt", "text/plain")), "multipart"),
    ("zero_byte_file", (("attachment", b"", "empty.txt", "text/plain"),), "multipart"),
    ("empty_filename", (("attachment", b"a file", "", "text/plain"),), "multipart"),
    ("scalar_attachment", (("attachment", b"scalar"),), "multipart"),
    ("body_field_only", (("body", b"Field body"),), "multipart"),
    ("invalid_utf8_multipart_body", (("body", b"\xff"),), "multipart"),
    ("raw_query_attachment", b"Raw body", "text/plain"),
    ("file_query_attachment", (("attachment", b"a file", "note.txt", "text/plain"),), "multipart"),
    ("raw_query_blank_attachment", b"Raw body", "text/plain"),
    ("file_query_blank_attachment", (("attachment", b"a file", "note.txt", "text/plain"),), "multipart"),
    ("raw_query_body", b"Raw body", "text/plain"),
    ("absent_room_query_attachment", b"Raw body", "text/plain"),
    ("absent_room_empty_raw", b"", "text/plain"),
)

QUERIES = {
    "raw_query_attachment": "?attachment=scalar",
    "file_query_attachment": "?attachment=scalar",
    "raw_query_blank_attachment": "?attachment=",
    "file_query_blank_attachment": "?attachment=",
    "raw_query_body": "?body=Query",
    "absent_room_query_attachment": "?attachment=scalar",
}


def multipart(parts):
    boundary = "rustfire-bot-body-boundary"
    payload = bytearray()
    for part in parts:
        name, value, *file = part
        payload.extend(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"".encode())
        if file:
            payload.extend(f"; filename=\"{file[0]}\"\r\nContent-Type: {file[1]}".encode())
        payload.extend(b"\r\n\r\n" + value + b"\r\n")
    payload.extend(f"--{boundary}--\r\n".encode())
    return bytes(payload), f"multipart/form-data; boundary={boundary}"


def request(port, method, path, payload=b"", content_type="text/plain"):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    try:
        connection.request(method, path, payload, {"Accept": "application/json", "Content-Type": content_type})
        response = connection.getresponse()
        return response.status, response.getheader("Content-Type", ""), response.getheader("Location", ""), response.read()
    finally:
        connection.close()


def latest_message(db_path):
    with sqlite3.connect(db_path) as db:
        return db.execute("SELECT COALESCE(MAX(id),0) FROM messages").fetchone()[0]


def exercise(port, db_path):
    base = f"/rooms/1/{BOT_ID}-{BOT_TOKEN}/messages"
    results = {}
    for label, value, media in CASES:
        payload, content_type = multipart(value) if media == "multipart" else (value, media)
        before = latest_message(db_path)
        path = base.replace("/rooms/1/", "/rooms/999/") if label.startswith("absent_room_") else base
        status, response_media, location, body = request(port, "POST", path + QUERIES.get(label, ""), payload, content_type)
        after = latest_message(db_path)
        saved = None
        if after != before:
            listing = request(port, "GET", base)
            assert listing[0] == 200, listing[:3]
            saved = next(message["body"]["plain_text"] for message in json.loads(listing[3]) if message["id"] == after)
        results[label] = (status, response_media, urlsplit(location).path.replace(f"/messages/{after}", "/messages/ID"),
                          after != before, saved, hashlib.sha256(body).hexdigest() if status >= 400 else None)
    return results


def main():
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPOSITORY, text=True).strip() == REVISION
    with tempfile.TemporaryDirectory(prefix="paired-bot-body-inputs-") as scratch:
        temp = pathlib.Path(scratch)
        rust_db, camp_db = temp / "rust.sqlite3", temp / "camp.sqlite3"
        rust_port, camp_port, redis_port = free_port(), free_port(), free_port()
        seed_rustfire(rust_db, rust_port, [])
        checkout = isolated_campfire(temp, redis_port)
        camp_env = seed_campfire(checkout, RUBY, BUNDLE, REPOSITORY / "storage/db/production.sqlite3", camp_db, [], camp_port, temp)
        camp_env["REDIS_URL"] = f"redis://127.0.0.1:{redis_port}"
        camp_env["WEB_CONCURRENCY"] = "1"
        seed_bot(rust_db, False, "", with_webhooks=False)
        seed_bot(camp_db, True, "", with_webhooks=False)
        redis, redis_log = start_redis(temp, redis_port)
        try:
            rust = start_server(rust_db, rust_port, {"RUSTFIRE_DISABLE_WEBHOOKS": "1"})
            try:
                rust_results = exercise(rust_port, rust_db)
            finally:
                stop_server(rust)
            with open(temp / "puma.log", "w+") as log:
                camp = subprocess.Popen([str(RUBY), str(RUBY.parent / "bundle"), "exec", "puma", "-C", "config/puma.rb"],
                                        cwd=checkout, env=camp_env, stdout=log, stderr=log)
                try:
                    wait_for_server(camp_port, camp)
                    camp_results = exercise(camp_port, camp_db)
                finally:
                    stop_server(camp)
        finally:
            redis.terminate()
            redis.wait(timeout=10)
            redis_log.close()
    differences = {name: (rust_results[name], camp_results[name]) for name in rust_results if rust_results[name] != camp_results[name]}
    for name, values in differences.items():
        print(name, values)
    assert not differences, f"{len(differences)} bot body input differences"
    print(f"PASS {len(CASES)} paired bot body input cases")


if __name__ == "__main__":
    main()
