"""Compare room index and room-namespace redirects with pinned Campfire."""

import http.client
import pathlib
import sqlite3
import subprocess
import tempfile
import urllib.parse

from direct_lookup import free_port, start_server, stop_server
from paired_banned_content import BUNDLE, REPOSITORY, REVISION, RUBY, isolated_campfire, start_redis
from paired_direct_lookup import login_campfire, seed_campfire, seed_rustfire, wait_for_server


PATHS = (
    "/rooms", "/rooms/opens", "/rooms/closeds", "/rooms/directs",
    "/rooms/opens/1", "/rooms/closeds/1", "/rooms/directs/1",
    "/rooms/opens/2", "/rooms/closeds/2", "/rooms/directs/2",
    "/rooms/opens/3", "/rooms/closeds/3", "/rooms/directs/3",
    "/rooms/999", "/rooms/opens/999", "/rooms/directs/999",
    "/rooms/new", "/rooms/abc", "/rooms/opens/abc", "/rooms/closeds/abc",
    "/rooms/1abc", "/rooms/1.0", "/rooms/opens/1abc", "/rooms/closeds/1abc",
)
ANONYMOUS_PATHS = (
    "/rooms", "/rooms/opens", "/rooms/closeds", "/rooms/directs",
    "/rooms/1", "/rooms/999", "/rooms/opens/1", "/rooms/opens/999", "/rooms/directs/2",
    "/rooms/new", "/rooms/abc", "/rooms/opens/abc", "/rooms/closeds/abc",
    "/rooms/1abc", "/rooms/1.0", "/rooms/opens/1abc", "/rooms/closeds/1abc",
)


def seed_closed_room(database, campfire):
    stamp = "2026-01-01 00:00:00.000000" if campfire else "2026-01-01T00:00:00Z"
    with sqlite3.connect(database) as db:
        db.execute("INSERT INTO rooms(id,name,type,creator_id,created_at,updated_at) VALUES(3,'Private','Rooms::Closed',1,?1,?1)", (stamp,))
        if campfire:
            db.execute("INSERT INTO memberships(room_id,user_id,involvement,created_at,updated_at) VALUES(3,1,'everything',?1,?1)", (stamp,))
        else:
            db.execute("INSERT INTO memberships(room_id,user_id,involvement,created_at) VALUES(3,1,'everything',?1)", (stamp,))


def request(port, cookie, path):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        connection.request("GET", path, headers={"Cookie": cookie})
        response = connection.getresponse()
        response.read()
        location = response.getheader("Location")
        return response.status, urllib.parse.urlsplit(location).path if location else None
    finally:
        connection.close()


def main():
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPOSITORY, text=True).strip() == REVISION
    with tempfile.TemporaryDirectory(prefix="paired-room-redirects-") as scratch:
        temp = pathlib.Path(scratch)
        rust_db, camp_db = temp / "rust.sqlite3", temp / "camp.sqlite3"
        rust_port, camp_port, redis_port = free_port(), free_port(), free_port()
        seed_rustfire(rust_db, rust_port, [[2]])
        camp_env = seed_campfire(REPOSITORY, RUBY, BUNDLE, REPOSITORY / "storage/db/production.sqlite3", camp_db, [[2]], camp_port, temp)
        checkout = isolated_campfire(temp, redis_port)
        camp_env["REDIS_URL"] = f"redis://127.0.0.1:{redis_port}"
        seed_closed_room(rust_db, False)
        seed_closed_room(camp_db, True)
        redis, redis_log = start_redis(temp, redis_port)
        try:
            rust = start_server(rust_db, rust_port)
            try:
                rust_result = {path: request(rust_port, "session_token=benchmark-session", path) for path in PATHS}
                rust_anonymous = {path: request(rust_port, "", path) for path in ANONYMOUS_PATHS}
            finally:
                stop_server(rust)
            with open(temp / "puma.log", "w+") as puma_log:
                camp = subprocess.Popen([str(RUBY), str(RUBY.parent / "bundle"), "exec", "puma", "-C", "config/puma.rb"], cwd=checkout, env=camp_env, stdout=puma_log, stderr=puma_log)
                try:
                    wait_for_server(camp_port, camp)
                    cookie, _ = login_campfire(camp_port)
                    camp_result = {path: request(camp_port, cookie, path) for path in PATHS}
                    camp_anonymous = {path: request(camp_port, "", path) for path in ANONYMOUS_PATHS}
                finally:
                    stop_server(camp)
        finally:
            redis.terminate()
            redis.wait(timeout=10)
            redis_log.close()
    comparable = [path for path in PATHS if camp_result[path][0] != 500]
    source_errors = [path for path in PATHS if camp_result[path][0] == 500]
    assert set(source_errors) == {"/rooms/directs/1", "/rooms/directs/2", "/rooms/directs/3", "/rooms/directs/999"}, source_errors
    mismatches = {path: (rust_result[path], camp_result[path]) for path in comparable if rust_result[path] != camp_result[path]}
    if mismatches:
        for path, (rust, camp) in mismatches.items():
            print(f"{path}: Rustfire={rust}, Campfire={camp}")
        raise AssertionError(f"{len(mismatches)} room redirect mismatches")
    anonymous_mismatches = {path: (rust_anonymous[path], camp_anonymous[path]) for path in ANONYMOUS_PATHS if rust_anonymous[path] != camp_anonymous[path]}
    if anonymous_mismatches:
        for path, (rust, camp) in anonymous_mismatches.items():
            print(f"anonymous {path}: Rustfire={rust}, Campfire={camp}")
        raise AssertionError(f"{len(anonymous_mismatches)} anonymous room redirect mismatches")
    print(f"PASS {len(comparable)} signed-in and {len(ANONYMOUS_PATHS)} anonymous room redirects match Campfire; skipped {len(source_errors)} upstream 500 routes")


if __name__ == "__main__":
    main()
