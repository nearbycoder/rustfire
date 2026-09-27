"""Compare Rails format suffixes and JSON negotiation on public read routes."""

import http.client
import pathlib
import subprocess
import tempfile

from direct_lookup import free_port, start_server, stop_server
from paired_banned_content import BUNDLE, REPOSITORY, REVISION, RUBY, isolated_campfire, start_redis
from paired_direct_lookup import login_campfire, seed_campfire, seed_rustfire, wait_for_server
from paired_turbo_fanout import seed_boost_message


CASES = (
    ("/qr_code/cnVzdA.svg", False, "text/html", 200),
    ("/qr_code/cnVzdA.png", False, "text/html", 200),
    ("/rooms/1/messages.json", True, "text/html", 406),
    ("/rooms/1/messages/1.json", True, "text/html", 406),
    ("/rooms/1/messages/1.turbo_stream", True, "text/html", 406),
    ("/rooms/1.json", True, "text/html", 406),
    ("/users/1.json", True, "text/html", 406),
    ("/account/edit.json", True, "text/html", 406),
    ("/searches.json", True, "text/html", 406),
    ("/webmanifest.json", False, "text/html", 200),
    ("/service-worker.js", False, "text/html", 200),
    ("/rooms/1/messages/1/edit.html", True, "text/html", 200),
    ("/rooms/1/messages/1.html", True, "text/html", 200),
    ("/rooms/1/messages", True, "application/json", 406),
    ("/rooms/1/messages/1", True, "application/json", 406),
    ("/rooms/1", True, "application/json", 406),
    ("/users/1", True, "application/json", 406),
    ("/account/edit", True, "application/json", 406),
    ("/searches", True, "application/json", 406),
)


def request(port, path, cookie, accept):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
    try:
        connection.request("GET", path, headers={"Cookie": cookie, "Accept": accept})
        response = connection.getresponse()
        body = response.read()
        return response.status, response.getheader("Content-Type", "").split(";", 1)[0], body
    finally:
        connection.close()


def collect(port, cookie):
    return {(path, accept): request(port, path, cookie if authenticated else "", accept)
            for path, authenticated, accept, _ in CASES}


def main():
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPOSITORY, text=True).strip() == REVISION
    with tempfile.TemporaryDirectory(prefix="paired-format-routes-") as scratch:
        temp = pathlib.Path(scratch)
        rust_db, camp_db = temp / "rust.sqlite3", temp / "camp.sqlite3"
        rust_port, camp_port, redis_port = free_port(), free_port(), free_port()
        seed_rustfire(rust_db, rust_port, [])
        environment = seed_campfire(REPOSITORY, RUBY, BUNDLE, REPOSITORY / "storage/db/production.sqlite3", camp_db, [], camp_port, temp)
        seed_boost_message(rust_db, camp_db)
        checkout = isolated_campfire(temp, redis_port)
        environment["REDIS_URL"] = f"redis://127.0.0.1:{redis_port}"
        redis, redis_log = start_redis(temp, redis_port)
        try:
            rust = start_server(rust_db, rust_port)
            try:
                rust_results = collect(rust_port, "session_token=benchmark-session")
            finally:
                stop_server(rust)
            with open(temp / "puma.log", "w+") as log:
                camp = subprocess.Popen([str(RUBY), str(RUBY.parent / "bundle"), "exec", "puma", "-C", "config/puma.rb"],
                                        cwd=checkout, env=environment, stdout=log, stderr=log)
                try:
                    wait_for_server(camp_port, camp)
                    cookie, _ = login_campfire(camp_port)
                    camp_results = collect(camp_port, cookie)
                finally:
                    stop_server(camp)
            for path, _, accept, expected in CASES:
                rust_status, rust_type, rust_body = rust_results[path, accept]
                camp_status, camp_type, camp_body = camp_results[path, accept]
                assert rust_status == camp_status == expected, (path, accept, rust_status, camp_status)
                assert rust_type == camp_type, (path, accept, rust_type, camp_type)
                if path.startswith("/qr_code/"):
                    assert rust_body == camp_body, (path, len(rust_body), len(camp_body))
        finally:
            redis.terminate()
            redis.wait(timeout=10)
            redis_log.close()
    print("PASS paired format-suffixed QR/media pages and JSON negotiation on 19 public GET cases")


if __name__ == "__main__":
    main()
