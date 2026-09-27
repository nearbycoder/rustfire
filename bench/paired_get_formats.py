"""Compare authenticated GET status and media type across common Rails formats.

Run after cargo build --release. Both applications use disposable databases,
and Campfire runs from the pinned source in an isolated checkout and Redis.
"""

import argparse
import http.client
import pathlib
import sqlite3
import subprocess
import tempfile

from direct_lookup import free_port, start_server, stop_server
from paired_banned_content import BUNDLE, REPOSITORY, REVISION, RUBY, isolated_campfire, start_redis
from paired_direct_lookup import login_campfire, seed_campfire, seed_rustfire, wait_for_server
from paired_turbo_fanout import seed_boost_message


PATHS = (
    "/", "/first_run", "/session/new", "/account/edit", "/account/users",
    "/account/bots", "/account/bots/new", "/account/logo",
    "/account/custom_styles/edit", "/users/1", "/users/1/profile",
    "/users/me/profile", "/users/1/sidebar", "/users/me/sidebar",
    "/users/1/push_subscriptions", "/users/me/push_subscriptions",
    "/autocompletable/users", "/rooms", "/rooms/1", "/rooms/1/messages",
    "/rooms/1/messages/1", "/rooms/1/messages/1/edit", "/rooms/1/refresh",
    "/rooms/1/involvement", "/rooms/1/@1",
    "/rooms/opens", "/rooms/opens/1", "/messages/1/boosts",
    "/messages/1/boosts/new", "/searches",
)
SUFFIXES = ("", ".html", ".json", ".turbo_stream")
ACCEPTS = ("text/html", "application/json", "text/vnd.turbo-stream.html", "*/*")


def request(port, path, cookie, accept, compare_406_bodies):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
    try:
        connection.request("GET", path, headers={"Cookie": cookie, "Accept": accept})
        response = connection.getresponse()
        body = response.read()
        content_type = (response.getheader("Content-Type") or "").split(";", 1)[0]
        if compare_406_bodies:
            return response.status, content_type, body if response.status == 406 else None
        return response.status, content_type
    finally:
        connection.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--all-accepts", action="store_true", help="compare HTML, JSON, Turbo, and wildcard Accept headers")
    parser.add_argument("--compare-406-bodies", action="store_true", help="also compare Not Acceptable response bodies")
    args = parser.parse_args()
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPOSITORY, text=True).strip() == REVISION
    with tempfile.TemporaryDirectory(prefix="paired-get-formats-") as scratch:
        temp = pathlib.Path(scratch)
        rust_db, camp_db = temp / "rust.sqlite3", temp / "camp.sqlite3"
        rust_port, camp_port, redis_port = free_port(), free_port(), free_port()
        seed_rustfire(rust_db, rust_port, [])
        camp_env = seed_campfire(REPOSITORY, RUBY, BUNDLE, REPOSITORY / "storage/db/production.sqlite3", camp_db, [], camp_port, temp)
        seed_boost_message(rust_db, camp_db)
        checkout = isolated_campfire(temp, redis_port)
        camp_env["REDIS_URL"] = f"redis://127.0.0.1:{redis_port}"
        redis, redis_log = start_redis(temp, redis_port)
        # Pinned Campfire routes /rooms/:id/settings to a missing controller and
        # returns 500 for all four formats, so it is not a usable parity case.
        paths = [path + suffix for path in PATHS for suffix in (SUFFIXES if path != "/" else ("",))]
        accepts = ACCEPTS if args.all_accepts else ("text/html",)
        cases = [(path, accept) for path in paths for accept in accepts]
        try:
            rust = start_server(rust_db, rust_port)
            try:
                rust_responses = {case: request(rust_port, case[0], "session_token=benchmark-session", case[1], args.compare_406_bodies) for case in cases}
            finally:
                stop_server(rust)
            with open(temp / "puma.log", "w+") as log:
                camp = subprocess.Popen([str(RUBY), str(RUBY.parent / "bundle"), "exec", "puma", "-C", "config/puma.rb"], cwd=checkout, env=camp_env, stdout=log, stderr=log)
                try:
                    wait_for_server(camp_port, camp)
                    cookie, _ = login_campfire(camp_port)
                    camp_responses = {case: request(camp_port, case[0], cookie, case[1], args.compare_406_bodies) for case in cases}
                finally:
                    stop_server(camp)
        finally:
            redis.terminate()
            redis.wait(timeout=10)
            redis_log.close()
    mismatches = [(case, rust_responses[case], camp_responses[case]) for case in cases if rust_responses[case] != camp_responses[case]]
    for (path, accept), rust_response, camp_response in mismatches:
        print(f"{path} Accept={accept}: Rustfire {rust_response}, Campfire {camp_response}")
    print(f"Matched {len(cases) - len(mismatches)}/{len(cases)} authenticated GET status/media-type cases")
    if mismatches:
        raise AssertionError(f"{len(mismatches)} GET format cases differ")


if __name__ == "__main__":
    main()
