"""Check Campfire-compatible search normalization and recent-history updates."""

import http.client
import pathlib
import re
import sqlite3
import subprocess
import tempfile
import urllib.parse

from direct_lookup import free_port, start_server, stop_server
from paired_direct_lookup import login_campfire, seed_campfire, seed_rustfire, wait_for_server
from paired_search import seed_messages


ROOT = pathlib.Path(__file__).resolve().parents[1]
REPOSITORY = pathlib.Path("/tmp/once-campfire-reference")
RUBY = pathlib.Path("/tmp/rustfire-baseline/local/bin/ruby")
BUNDLE = pathlib.Path("/tmp/rustfire-baseline/bundle")
REVISION = "91d294f4a09f9bbe37f9548959bfcb43645678fb"


def request(port, cookie, path, token=None, query=None):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    try:
        body = urllib.parse.urlencode({"q": query, "authenticity_token": token}) if query is not None else None
        connection.request("POST" if body is not None else "GET", path, body=body,
                           headers={"Cookie": cookie, "Accept": "text/html",
                                    "Content-Type": "application/x-www-form-urlencoded"})
        response = connection.getresponse()
        return response.status, response.getheader("Location"), response.read().decode()
    finally:
        connection.close()


def check_app(port, cookie, token, database):
    status, _, page = request(port, cookie, "/searches?q=benchmark%21")
    assert status == 200, (status, page[:200])
    assert len(re.findall(r"id=['\"]message_fixture-\d+['\"]", page)) == 100
    for raw, expected in (("old-term!", "old term "), ("hello_world", "hello_world"), ("!!!", "   ")):
        status, location, _ = request(port, cookie, "/searches", token, raw)
        assert status == 302, (raw, status, location)
        assert urllib.parse.parse_qs(urllib.parse.urlsplit(location).query, keep_blank_values=True)["q"] == [expected], (raw, location)
    with sqlite3.connect(database) as db:
        history = db.execute("SELECT query,created_at,updated_at FROM searches WHERE user_id=1 ORDER BY updated_at DESC,id DESC").fetchall()
    assert [row[0] for row in history] == ["   ", "hello_world", "old term ", "newer"], history
    assert history[2][1] == "2025-01-01 00:00:00" and history[2][2] != history[2][1], history[2]
    return [row[0] for row in history]


def main():
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPOSITORY, text=True).strip() == REVISION
    with tempfile.TemporaryDirectory(prefix="paired-search-history-") as scratch:
        temp = pathlib.Path(scratch)
        rust_db, camp_db = temp / "rust.sqlite3", temp / "camp.sqlite3"
        rust_port, camp_port = free_port(), free_port()
        seed_rustfire(rust_db, rust_port, [])
        camp_env = seed_campfire(REPOSITORY, RUBY, BUNDLE, REPOSITORY / "storage/db/production.sqlite3", camp_db, [], camp_port, temp)
        seed_messages(rust_db, camp_db, 100)
        for database in (rust_db, camp_db):
            with sqlite3.connect(database) as db:
                db.execute("DELETE FROM searches")
                columns = "id,user_id,query,created_at,updated_at"
                db.execute(f"INSERT INTO searches({columns}) VALUES(1,1,'old term ','2025-01-01 00:00:00','2025-01-01 00:00:00')")
                db.execute(f"INSERT INTO searches({columns}) VALUES(2,1,'newer','2026-01-01 00:00:00','2026-01-01 00:00:00')")
        rust = start_server(rust_db, rust_port, {})
        try:
            rust_history = check_app(rust_port, "session_token=benchmark-session", "benchmark-csrf", rust_db)
        finally:
            stop_server(rust)
        camp_env["WEB_CONCURRENCY"] = "1"
        with open(temp / "puma.log", "w+") as log:
            camp = subprocess.Popen([str(RUBY), str(RUBY.parent / "bundle"), "exec", "puma", "-C", "config/puma.rb"],
                                    cwd=REPOSITORY, env=camp_env, stdout=log, stderr=log)
            try:
                wait_for_server(camp_port, camp)
                cookie, token = login_campfire(camp_port)
                camp_history = check_app(camp_port, cookie, token, camp_db)
            except Exception:
                log.flush()
                log.seek(0)
                print(log.read()[-2000:])
                raise
            finally:
                stop_server(camp)
        assert rust_history == camp_history, (rust_history, camp_history)
    print("PASS paired search results, punctuation normalization, and recent-history timestamps")


if __name__ == "__main__":
    main()
