"""Compare invalid-CSRF responses on matched disposable Campfire fixtures."""

import argparse
import hashlib
import http.client
import pathlib
import subprocess
import tempfile
import urllib.parse

from direct_lookup import free_port, start_server, stop_server
from paired_banned_content import BUNDLE, REPOSITORY, REVISION, RUBY, isolated_campfire, start_redis
from paired_direct_lookup import login_campfire, seed_campfire, seed_rustfire, wait_for_server
from paired_turbo_fanout import seed_boost_message


CASES = (
    ("POST", "/account"),
    ("POST", "/account/bots"),
    ("PATCH", "/account/bots/1/key"),
    ("PATCH", "/account/users/2"),
    ("POST", "/account/join_code"),
    ("PATCH", "/account/custom_styles"),
    ("POST", "/rooms/1/messages"),
    ("PATCH", "/rooms/1/messages/1"),
    ("POST", "/rooms/opens"),
    ("POST", "/messages/1/boosts"),
    ("POST", "/searches"),
    ("POST", "/unfurl_link"),
    ("PATCH", "/users/me/profile"),
    ("POST", "/users/me/push_subscriptions"),
    ("PATCH", "/rooms/1/involvement"),
)
ACCEPTS = ("text/html", "application/json", "text/vnd.turbo-stream.html", "*/*")
OVERRIDE_CASES = (
    ("POST", "/account", "_method=patch&authenticity_token=invalid"),
    ("POST", "/account.1", "_method=put&authenticity_token=invalid"),
    ("POST", "/account.1", "authenticity_token=invalid"),
)


def request(port, cookie, method, path, accept, body, compare_bodies):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
    try:
        connection.request(method, path, body=body, headers={
            "Cookie": cookie,
            "Accept": accept,
            "Content-Type": "application/x-www-form-urlencoded",
        })
        response = connection.getresponse()
        body = response.read()
        location = response.getheader("Location")
        media = (response.getheader("Content-Type") or "").split(";", 1)[0]
        result = (response.status, media, urllib.parse.urlsplit(location).path if location else None)
        if compare_bodies:
            result += (len(body), hashlib.sha256(body).hexdigest())
        return result
    finally:
        connection.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--all-accepts", action="store_true", help="compare HTML, JSON, Turbo, and wildcard Accept headers")
    parser.add_argument("--compare-bodies", action="store_true", help="also compare response body length and SHA-256")
    args = parser.parse_args()
    accepts = ACCEPTS if args.all_accepts else ("text/html",)
    cases = [(method, path, accept, "authenticity_token=invalid") for method, path in CASES for accept in accepts]
    cases += [(method, path, accept, body) for method, path, body in OVERRIDE_CASES for accept in accepts]
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPOSITORY, text=True).strip() == REVISION
    with tempfile.TemporaryDirectory(prefix="paired-csrf-precedence-") as scratch:
        temp = pathlib.Path(scratch)
        rust_db, camp_db = temp / "rust.sqlite3", temp / "camp.sqlite3"
        rust_port, camp_port, redis_port = free_port(), free_port(), free_port()
        seed_rustfire(rust_db, rust_port, [])
        camp_env = seed_campfire(REPOSITORY, RUBY, BUNDLE, REPOSITORY / "storage/db/production.sqlite3", camp_db, [], camp_port, temp)
        seed_boost_message(rust_db, camp_db)
        checkout = isolated_campfire(temp, redis_port)
        camp_env["REDIS_URL"] = f"redis://127.0.0.1:{redis_port}"
        redis, redis_log = start_redis(temp, redis_port)
        try:
            rust = start_server(rust_db, rust_port)
            try:
                rust_responses = {case: request(rust_port, "session_token=benchmark-session", *case, args.compare_bodies) for case in cases}
            finally:
                stop_server(rust)
            with open(temp / "puma.log", "w+") as log:
                camp = subprocess.Popen([str(RUBY), str(RUBY.parent / "bundle"), "exec", "puma", "-C", "config/puma.rb"], cwd=checkout, env=camp_env, stdout=log, stderr=log)
                try:
                    wait_for_server(camp_port, camp)
                    cookie, _ = login_campfire(camp_port)
                    camp_responses = {case: request(camp_port, cookie, *case, args.compare_bodies) for case in cases}
                finally:
                    stop_server(camp)
        finally:
            redis.terminate()
            redis.wait(timeout=10)
            redis_log.close()
    mismatches = [(case, rust_responses[case], camp_responses[case])
                  for case in cases if rust_responses[case] != camp_responses[case]]
    for (method, path, accept, body), rust, camp in mismatches:
        print(f"{method} {path} Accept={accept} body={body}: Rustfire {rust}, Campfire {camp}")
    print(f"Matched {len(cases) - len(mismatches)}/{len(cases)} invalid-CSRF response cases")
    if mismatches:
        raise AssertionError(f"{len(mismatches)} invalid-CSRF cases differ")


if __name__ == "__main__":
    main()
