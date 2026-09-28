"""Compare room-index responses when the signed-in user has no memberships."""

import hashlib
import pathlib
import sqlite3
import subprocess
import tempfile
import urllib.parse

from direct_lookup import free_port, start_server, stop_server
from paired_banned_content import BUNDLE, REPOSITORY, REVISION, RUBY, start_redis
from paired_direct_lookup import login_campfire, seed_campfire, seed_rustfire, wait_for_server
from paired_room_shell import raw_get


PATHS = ("/rooms", "/rooms.json", "/rooms?format=json")
ACCEPTS = ("text/html", "application/json", "text/vnd.turbo-stream.html", "*/*")


def response(port, cookie, path, accept):
    status, headers, body = raw_get(port, path, cookie, {"Accept": accept})
    location = urllib.parse.urlsplit(headers.get("location", ""))
    redirect = location.path + ("?" + location.query if location.query else "") if location.path else None
    return status, (headers.get("content-type") or "").split(";", 1)[0], redirect, len(body), hashlib.sha256(body).hexdigest()


def main():
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPOSITORY, text=True).strip() == REVISION
    with tempfile.TemporaryDirectory(prefix="paired-empty-room-index-") as scratch:
        temp = pathlib.Path(scratch)
        rust_db, camp_db = temp / "rust.sqlite3", temp / "camp.sqlite3"
        rust_port, camp_port, redis_port = free_port(), free_port(), free_port()
        seed_rustfire(rust_db, rust_port, [])
        environment = seed_campfire(REPOSITORY, RUBY, BUNDLE, REPOSITORY / "storage/db/production.sqlite3", camp_db, [], camp_port, temp)
        environment["REDIS_URL"] = f"redis://127.0.0.1:{redis_port}"
        for database in (rust_db, camp_db):
            with sqlite3.connect(database) as db:
                db.execute("DELETE FROM memberships WHERE user_id=1")
        redis, redis_log = start_redis(temp, redis_port)
        try:
            rust = start_server(rust_db, rust_port)
            with (temp / "puma.log").open("w+") as log:
                camp = subprocess.Popen(
                    [str(RUBY), str(RUBY.parent / "bundle"), "exec", "puma", "-C", "config/puma.rb"],
                    cwd=REPOSITORY, env=environment, stdout=log, stderr=log,
                )
                try:
                    wait_for_server(camp_port, camp)
                    camp_cookie, _ = login_campfire(camp_port)
                    rust_cookie = "session_token=benchmark-session"
                    cases = [(path, accept) for path in PATHS for accept in ACCEPTS]
                    rust_results = [response(rust_port, rust_cookie, path, accept) for path, accept in cases]
                    camp_results = [response(camp_port, camp_cookie, path, accept) for path, accept in cases]
                    welcome = (
                        response(rust_port, rust_cookie, "/", "text/html"),
                        response(camp_port, camp_cookie, "/", "text/html"),
                    )
                    anonymous = [
                        (response(rust_port, "", path, "text/html"), response(camp_port, "", path, "text/html"))
                        for path in ("/rooms", "/rooms.json")
                    ]
                except Exception:
                    log.flush()
                    log.seek(0)
                    print(log.read()[-2500:])
                    raise
                finally:
                    stop_server(camp)
                    stop_server(rust)
        finally:
            redis.terminate()
            redis.wait(timeout=10)
            redis_log.close()
    for case, rust, camp in zip(cases, rust_results, camp_results):
        if rust != camp:
            print(f"{case}: Rustfire={rust}, Campfire={camp}")
    assert rust_results == camp_results, "Room-index errors differ"
    assert all(result[0] == 500 and result[2] is None for result in rust_results)
    assert all(result[0] == 200 and result[1] == "text/html" for result in welcome)
    assert all(rust == camp and rust[:3] == (302, "text/html", "/session/new") for rust, camp in anonymous), anonymous
    print(f"PASS {len(cases)} no-membership room-index formats match Campfire; root and anonymous redirects remain intact")


if __name__ == "__main__":
    main()
