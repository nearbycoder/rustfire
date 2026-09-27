"""Compare missing actions and unsupported HTTP methods with pinned Campfire."""

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
    ("account edit", "GET", "/account/edit", True, 200),
    ("account show action absent", "GET", "/account", True, 404),
    ("session show action absent", "GET", "/session", True, 404),
    ("bot show action absent", "GET", "/account/bots/1", True, 404),
    ("account user new action absent", "GET", "/account/users/new", True, 404),
    ("anonymous bot key POST", "POST", "/account/bots/1/key", False, 404),
    ("profile edit action absent", "GET", "/users/me/profile/edit", True, 404),
    ("health", "GET", "/up", False, 200),
    ("health POST", "POST", "/up", False, 404),
    ("edit styles DELETE", "DELETE", "/account/custom_styles/edit", True, 404),
    ("message new action absent", "GET", "/rooms/1/messages/new", True, 404),
    ("anonymous message new action absent", "GET", "/rooms/1/messages/new", False, 404),
    ("invalid user ID", "GET", "/users/abc", True, 404),
    ("boost show action absent", "GET", "/messages/1/boosts/1", True, 404),
    ("anonymous boost show action absent", "GET", "/messages/1/boosts/1", False, 404),
    ("push subscription new action absent", "GET", "/users/me/push_subscriptions/new", True, 404),
    ("anonymous push subscription new action absent", "GET", "/users/me/push_subscriptions/new", False, 404),
    ("scoped push subscription show action absent", "GET", "/users/1/push_subscriptions/1", True, 404),
    ("malformed message ID", "GET", "/rooms/1/messages/abc", True, 404),
    ("anonymous malformed message ID", "GET", "/rooms/1/messages/abc", False, 302),
    ("malformed message edit ID", "GET", "/rooms/1/messages/abc/edit", True, 404),
    ("anonymous malformed message edit ID", "GET", "/rooms/1/messages/abc/edit", False, 302),
    ("malformed room ID on message list", "GET", "/rooms/abc/messages", True, 404),
    ("anonymous malformed room ID on message list", "GET", "/rooms/abc/messages", False, 302),
    ("malformed room ID on message show", "GET", "/rooms/abc/messages/1", True, 404),
    ("anonymous malformed room ID on message show", "GET", "/rooms/abc/messages/1", False, 302),
    ("malformed boost message ID", "GET", "/messages/abc/boosts", True, 404),
    ("anonymous malformed boost message ID", "GET", "/messages/abc/boosts", False, 302),
    ("malformed boost editor message ID", "GET", "/messages/abc/boosts/new", True, 404),
    ("anonymous malformed boost editor message ID", "GET", "/messages/abc/boosts/new", False, 302),
    ("malformed room involvement ID", "GET", "/rooms/abc/involvement", True, 404),
    ("anonymous malformed room involvement ID", "GET", "/rooms/abc/involvement", False, 302),
    ("malformed room refresh ID", "GET", "/rooms/abc/refresh", True, 404),
    ("anonymous malformed room refresh ID", "GET", "/rooms/abc/refresh", False, 302),
    ("anonymous malformed user ID", "GET", "/users/abc", False, 302),
    ("suffixed user ID", "GET", "/users/1abc", True, 200),
    ("suffixed room message list ID", "GET", "/rooms/1abc/messages", True, 200),
    ("suffixed room involvement ID", "GET", "/rooms/1abc/involvement", True, 200),
    ("suffixed room refresh ID", "GET", "/rooms/1abc/refresh", True, 406),
    ("suffixed message ID", "GET", "/rooms/1/messages/1abc", True, 200),
    ("suffixed message edit ID", "GET", "/rooms/1/messages/1abc/edit", True, 200),
    ("suffixed boost message ID", "GET", "/messages/1abc/boosts", True, 200),
    ("suffixed boost editor message ID", "GET", "/messages/1abc/boosts/new", True, 200),
)


def request(port, method, path, cookie, csrf=None):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        headers = {"Accept": "text/html"}
        if cookie:
            headers["Cookie"] = cookie
        if csrf:
            headers["X-CSRF-Token"] = csrf
        connection.request(method, path, headers=headers)
        response = connection.getresponse()
        response.read()
        location = response.getheader("Location")
        return response.status, response.getheader("Allow"), urllib.parse.urlsplit(location).path if location else None
    finally:
        connection.close()


def main():
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPOSITORY, text=True).strip() == REVISION
    with tempfile.TemporaryDirectory(prefix="paired-route-methods-") as scratch:
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
                rust_results = {label: request(rust_port, method, path, "session_token=benchmark-session" if signed_in else "", "benchmark-csrf" if signed_in else None)
                                for label, method, path, signed_in, _ in CASES}
            finally:
                stop_server(rust)
            with open(temp / "puma.log", "w+") as log:
                camp = subprocess.Popen([str(RUBY), str(RUBY.parent / "bundle"), "exec", "puma", "-C", "config/puma.rb"],
                                        cwd=checkout, env=camp_env, stdout=log, stderr=log)
                try:
                    wait_for_server(camp_port, camp)
                    cookie, csrf = login_campfire(camp_port)
                    camp_results = {label: request(camp_port, method, path, cookie if signed_in else "", csrf if signed_in else None)
                                    for label, method, path, signed_in, _ in CASES}
                except Exception:
                    log.flush()
                    log.seek(0)
                    print(log.read()[-3000:])
                    raise
                finally:
                    stop_server(camp)
            for label, _, _, _, expected in CASES:
                destination = "/session/new" if expected == 302 else None
                assert rust_results[label] == camp_results[label] == (expected, None, destination), (label, rust_results[label], camp_results[label])
            print("PASS paired missing-action and unsupported-method route status/Allow responses")
        finally:
            redis.terminate()
            redis.wait(timeout=10)
            redis_log.close()


if __name__ == "__main__":
    main()
