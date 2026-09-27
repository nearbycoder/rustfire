"""Compare missing-record write behavior across Campfire's public routes."""

import http.client
import hashlib
import pathlib
import subprocess
import tempfile
import urllib.parse

from direct_lookup import free_port, start_server, stop_server
from paired_banned_content import BUNDLE, REPOSITORY, REVISION, RUBY, isolated_campfire, start_redis
from paired_direct_lookup import login_campfire, seed_campfire, seed_rustfire, wait_for_server


CASES = (
    ("PATCH", "/account/users/999", "text/html"),
    ("PUT", "/account/users/999", "text/html"),
    ("DELETE", "/account/users/999", "text/html"),
    ("POST", "/users/999/ban", "text/html"),
    ("DELETE", "/users/999/ban", "text/html"),
    ("DELETE", "/users/999/avatar", "text/html"),
    ("PATCH", "/users/999/profile", "text/html"),
    ("DELETE", "/users/999/push_subscriptions/999", "text/html"),
    ("POST", "/users/999/push_subscriptions/999/test_notifications", "text/html"),
    ("PATCH", "/account/bots/999", "text/html"),
    ("PUT", "/account/bots/999", "text/html"),
    ("DELETE", "/account/bots/999", "text/html"),
    ("PATCH", "/account/bots/999/key", "text/html"),
    ("PUT", "/account/bots/999/key", "text/html"),
    ("POST", "/rooms/999/messages", "text/vnd.turbo-stream.html"),
    ("PATCH", "/rooms/999/messages/999", "text/html"),
    ("PUT", "/rooms/999/messages/999", "text/html"),
    ("DELETE", "/rooms/999/messages/999", "text/vnd.turbo-stream.html"),
    ("PATCH", "/rooms/1/messages/999", "text/html"),
    ("PUT", "/rooms/1/messages/999", "text/html"),
    ("DELETE", "/rooms/1/messages/999", "text/vnd.turbo-stream.html"),
    ("DELETE", "/rooms/999", "text/html"),
    ("PATCH", "/rooms/opens/999", "text/html"),
    ("PUT", "/rooms/opens/999", "text/html"),
    ("DELETE", "/rooms/opens/999", "text/html"),
    ("PATCH", "/rooms/closeds/999", "text/html"),
    ("PUT", "/rooms/closeds/999", "text/html"),
    ("DELETE", "/rooms/closeds/999", "text/html"),
    ("DELETE", "/rooms/directs/999", "text/html"),
    ("PATCH", "/rooms/999/involvement", "text/html"),
    ("PUT", "/rooms/999/involvement", "text/html"),
    ("POST", "/messages/999/boosts", "text/vnd.turbo-stream.html"),
    ("DELETE", "/messages/999/boosts/999", "text/vnd.turbo-stream.html"),
)


def request(port, cookie, csrf, method, path, accept):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
    try:
        connection.request(method, path, b"", {
            "Cookie": cookie,
            "X-CSRF-Token": csrf,
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": accept,
        })
        response = connection.getresponse()
        location = response.getheader("Location")
        body = response.read()
        result = (response.status, response.getheader("Content-Type", "").split(";", 1)[0],
                  urllib.parse.urlsplit(location).path if location else None,
                  len(body), hashlib.sha256(body).hexdigest())
        return result
    finally:
        connection.close()


def main():
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPOSITORY, text=True).strip() == REVISION
    with tempfile.TemporaryDirectory(prefix="paired-write-route-edges-") as scratch:
        temp = pathlib.Path(scratch)
        rust_db, camp_db = temp / "rust.sqlite3", temp / "camp.sqlite3"
        rust_port, camp_port, redis_port = free_port(), free_port(), free_port()
        seed_rustfire(rust_db, rust_port, [])
        camp_env = seed_campfire(REPOSITORY, RUBY, BUNDLE, REPOSITORY / "storage/db/production.sqlite3", camp_db, [], camp_port, temp)
        checkout = isolated_campfire(temp, redis_port)
        camp_env["REDIS_URL"] = f"redis://127.0.0.1:{redis_port}"
        redis, redis_log = start_redis(temp, redis_port)
        try:
            rust = start_server(rust_db, rust_port)
            try:
                rust_results = [request(rust_port, "session_token=benchmark-session", "benchmark-csrf", *case) for case in CASES]
            finally:
                stop_server(rust)
            with open(temp / "puma.log", "w+") as log:
                camp = subprocess.Popen([str(RUBY), str(RUBY.parent / "bundle"), "exec", "puma", "-C", "config/puma.rb"], cwd=checkout, env=camp_env, stdout=log, stderr=log)
                try:
                    wait_for_server(camp_port, camp)
                    cookie, csrf = login_campfire(camp_port)
                    camp_results = [request(camp_port, cookie, csrf, *case) for case in CASES]
                finally:
                    stop_server(camp)
            mismatches = [(case, target, original) for case, target, original in zip(CASES, rust_results, camp_results) if target != original]
            assert not mismatches, mismatches
            print(f"PASS {len(CASES)} paired missing-record write routes")
        finally:
            redis.terminate()
            redis.wait(timeout=10)
            redis_log.close()


if __name__ == "__main__":
    main()
