"""Check how Campfire treats an extra ``message[format]`` form field.

Run after ``cargo build --release`` with the pinned Campfire checkout and bundle.
"""

import http.client
import pathlib
import sqlite3
import subprocess
import tempfile
import urllib.parse

from direct_lookup import free_port, start_server, stop_server
from paired_banned_content import BUNDLE, REPOSITORY, REVISION, RUBY, start_redis
from paired_direct_lookup import login_campfire, seed_campfire, seed_rustfire, wait_for_server
from paired_rich_filters import Presentation


BODY = "<div>Before <strong>bold</strong> after</div>"
CASES = [
    ("default", None, False, False),
    ("plain", "text", False, False),
    ("unknown", "markdown", False, False),
    ("empty", "", False, False),
    ("multipart", "text", True, False),
    ("query", "text", False, True),
]


def request(port, cookie, csrf, method, path, name, body, format_value, multipart=False):
    fields = {"message[body]": body, "authenticity_token": csrf}
    if method == "POST":
        fields["message[client_message_id]"] = name
    if format_value is not None:
        fields["message[format]"] = format_value
    if multipart:
        boundary = "----rustfire-format-field"
        payload = b"".join(
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"{key}\"\r\n\r\n{value}\r\n".encode()
            for key, value in fields.items()
        ) + f"--{boundary}--\r\n".encode()
        content_type = f"multipart/form-data; boundary={boundary}"
    else:
        payload = urllib.parse.urlencode(fields)
        content_type = "application/x-www-form-urlencoded"
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
    try:
        connection.request(method, path, payload, {
            "Cookie": cookie, "Content-Type": content_type,
            "Accept": "text/vnd.turbo-stream.html, text/html" if method == "POST" else "text/html",
        })
        response = connection.getresponse()
        payload = response.read()
        parsed = Presentation(name)
        parsed.feed(payload.decode())
        return response.status, parsed.structure
    finally:
        connection.close()


def read(port, cookie, message_id, name):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
    try:
        connection.request("GET", f"/rooms/1/messages/{message_id}", headers={"Cookie": cookie, "Accept": "text/html"})
        response = connection.getresponse()
        payload = response.read()
        parsed = Presentation(name)
        parsed.feed(payload.decode())
        return response.status, parsed.structure
    finally:
        connection.close()


def run(port, cookie, csrf, database):
    results = {}
    for label, format_value, multipart, query in CASES:
        name = f"format-{label}"
        path = "/rooms/1/messages"
        if query:
            path += "?" + urllib.parse.urlencode({"message[body]": BODY, "message[format]": format_value,
                                                    "message[client_message_id]": name})
        result = request(port, cookie, csrf, "POST", path, name, BODY, format_value, multipart)
        with sqlite3.connect(database) as db:
            message_id = db.execute("SELECT id FROM messages WHERE client_message_id=?", (name,)).fetchone()[0]
            search = db.execute("SELECT body FROM message_search_index WHERE rowid=?", (message_id,)).fetchone()
        results[label] = result, search, read(port, cookie, message_id, name)
    name = "format-default"
    with sqlite3.connect(database) as db:
        message_id = db.execute("SELECT id FROM messages WHERE client_message_id=?", (name,)).fetchone()[0]
    edit = request(port, cookie, csrf, "PATCH", f"/rooms/1/messages/{message_id}", name,
                   "<div>Edited <em>text</em></div>", "text")
    with sqlite3.connect(database) as db:
        search = db.execute("SELECT body FROM message_search_index WHERE rowid=?", (message_id,)).fetchone()
    results["edit"] = edit, search, read(port, cookie, message_id, name)
    edit = request(port, cookie, csrf, "PATCH", f"/rooms/1/messages/{message_id}", name,
                   "<div>Multipart <em>again</em></div>", "text", multipart=True)
    with sqlite3.connect(database) as db:
        search = db.execute("SELECT body FROM message_search_index WHERE rowid=?", (message_id,)).fetchone()
    results["edit-multipart"] = edit, search, read(port, cookie, message_id, name)
    return results


def main():
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPOSITORY, text=True).strip() == REVISION
    with tempfile.TemporaryDirectory(prefix="paired-message-format-field-") as scratch:
        temp = pathlib.Path(scratch)
        rust_db, camp_db = temp / "rust.sqlite3", temp / "camp.sqlite3"
        rust_port, camp_port, redis_port = free_port(), free_port(), free_port()
        seed_rustfire(rust_db, rust_port, [])
        env = seed_campfire(REPOSITORY, RUBY, BUNDLE, REPOSITORY / "storage/db/production.sqlite3", camp_db, [], camp_port, temp)
        env["REDIS_URL"] = f"redis://127.0.0.1:{redis_port}"
        env["WEB_CONCURRENCY"] = "1"
        redis, redis_log = start_redis(temp, redis_port)
        try:
            process = start_server(rust_db, rust_port)
            try:
                rust = run(rust_port, "session_token=benchmark-session", "benchmark-csrf", rust_db)
            finally:
                stop_server(process)
            with (temp / "puma.log").open("w+") as log:
                process = subprocess.Popen([str(RUBY), str(RUBY.parent / "bundle"), "exec", "puma", "-C", "config/puma.rb"], cwd=REPOSITORY, env=env, stdout=log, stderr=log)
                try:
                    wait_for_server(camp_port, process)
                    cookie, csrf = login_campfire(camp_port)
                    camp = run(camp_port, cookie, csrf, camp_db)
                finally:
                    stop_server(process)
        finally:
            redis.terminate()
            redis.wait(timeout=10)
            redis_log.close()
    mismatches = [(label, rust[label], camp[label]) for label in rust if rust[label] != camp[label]]
    for label, left, right in mismatches:
        print(f"{label}: Rustfire {left!r} / Campfire {right!r}")
    assert not mismatches, f"{len(mismatches)} message format-field cases differ"
    print(f"PASS {len(rust)} message format-field cases")


if __name__ == "__main__":
    main()
