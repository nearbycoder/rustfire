"""Compare autocomplete query shapes and room access with pinned Campfire."""

import hashlib
import http.client
import json
import pathlib
import sqlite3
import subprocess
import tempfile
from urllib.parse import urlencode

from direct_lookup import free_port, start_server, stop_server
from paired_banned_content import BUNDLE, REPOSITORY, REVISION, RUBY, isolated_campfire, start_redis
from paired_direct_lookup import login_campfire, seed_campfire, seed_rustfire, wait_for_server


CASES = (
    ("plain", [("query", "da")]),
    ("case_insensitive", [("query", "DA")]),
    ("escaped_name", [("query", "<script>")]),
    ("accessible_room", [("query", "da"), ("room_id", "1")]),
    ("inaccessible_room", [("query", "da"), ("room_id", "2")]),
    ("absent_room", [("query", "da"), ("room_id", "999")]),
    ("blank_room", [("query", "da"), ("room_id", "")]),
    ("invalid_room", [("query", "da"), ("room_id", "bogus")]),
    ("q_only", [("q", "da")]),
    ("blank_query", [("query", "")]),
    ("duplicate_query", [("query", "da"), ("query", "user")]),
    ("array_query", [("query[]", "da")]),
)


def seed_names(database, campfire):
    stamp = "2026-01-01 00:00:00.000000" if campfire else "2026-01-01T00:00:00Z"
    with sqlite3.connect(database) as db:
        db.execute("UPDATE users SET name='David <script>alert(123)</script>' WHERE id=1")
        db.execute("UPDATE users SET name='Dana' WHERE id=2")
        db.execute("UPDATE users SET name='Daisy' WHERE id=3")
        db.execute("INSERT INTO rooms(id,name,type,creator_id,created_at,updated_at) VALUES(2,'Hidden','Rooms::Closed',2,?1,?1)", [stamp])
        if campfire:
            db.executemany("INSERT INTO memberships(room_id,user_id,involvement,created_at,updated_at) VALUES(2,?1,'everything',?2,?2)", [(2, stamp), (3, stamp)])
        else:
            db.executemany("INSERT INTO memberships(room_id,user_id,involvement,created_at) VALUES(2,?1,'everything',?2)", [(2, stamp), (3, stamp)])


def request(port, cookie, parameters):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    try:
        path = "/autocompletable/users.json?" + urlencode(parameters)
        connection.request("GET", path, headers={"Cookie": cookie, "Accept": "application/json"})
        response = connection.getresponse()
        body = response.read()
        media = response.getheader("Content-Type", "")
        if response.status == 200:
            entries = json.loads(body)
            detail = [(entry["value"], entry["name"]) for entry in entries]
        else:
            detail = hashlib.sha256(body).hexdigest()
        return response.status, media, detail
    finally:
        connection.close()


def exercise(port, cookie):
    return {label: request(port, cookie, parameters) for label, parameters in CASES}


def main():
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPOSITORY, text=True).strip() == REVISION
    with tempfile.TemporaryDirectory(prefix="paired-autocomplete-inputs-") as scratch:
        temp = pathlib.Path(scratch)
        rust_db, camp_db = temp / "rust.sqlite3", temp / "camp.sqlite3"
        rust_port, camp_port, redis_port = free_port(), free_port(), free_port()
        seed_rustfire(rust_db, rust_port, [])
        checkout = isolated_campfire(temp, redis_port)
        camp_env = seed_campfire(checkout, RUBY, BUNDLE, REPOSITORY / "storage/db/production.sqlite3", camp_db, [], camp_port, temp)
        camp_env["REDIS_URL"] = f"redis://127.0.0.1:{redis_port}"
        camp_env["WEB_CONCURRENCY"] = "1"
        seed_names(rust_db, False)
        seed_names(camp_db, True)
        redis, redis_log = start_redis(temp, redis_port)
        try:
            rust = start_server(rust_db, rust_port)
            try:
                rust_results = exercise(rust_port, "session_token=benchmark-session")
            finally:
                stop_server(rust)
            with open(temp / "puma.log", "w+") as log:
                camp = subprocess.Popen([str(RUBY), str(RUBY.parent / "bundle"), "exec", "puma", "-C", "config/puma.rb"],
                                        cwd=checkout, env=camp_env, stdout=log, stderr=log)
                try:
                    wait_for_server(camp_port, camp)
                    cookie, _ = login_campfire(camp_port)
                    camp_results = exercise(camp_port, cookie)
                finally:
                    stop_server(camp)
        finally:
            redis.terminate()
            redis.wait(timeout=10)
            redis_log.close()
    differences = {label: (rust_results[label], camp_results[label]) for label in rust_results
                   if rust_results[label] != camp_results[label]}
    for label, values in differences.items():
        print(label, values)
    assert not differences, f"{len(differences)} autocomplete input cases differ"
    print(f"PASS {len(CASES)} paired autocomplete input cases")


if __name__ == "__main__":
    main()
