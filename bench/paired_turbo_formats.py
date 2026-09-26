"""Compare Campfire's Turbo-only route format negotiation on disposable fixtures."""

import pathlib
import subprocess
import tempfile
import urllib.error
import urllib.request

from direct_lookup import free_port, start_server, stop_server
from paired_banned_content import start_redis
from paired_direct_lookup import login_campfire, seed_campfire, seed_rustfire, wait_for_server


SOURCE = pathlib.Path("/tmp/once-campfire-reference")
RUBY = pathlib.Path("/tmp/rustfire-baseline/local/bin/ruby")
BUNDLE = pathlib.Path("/tmp/rustfire-baseline/bundle")
REVISION = "91d294f4a09f9bbe37f9548959bfcb43645678fb"
CASES = [
    ("/rooms/1/refresh", None),
    ("/rooms/1/refresh", "text/html"),
    ("/rooms/1/refresh", "application/json"),
    ("/rooms/1/refresh", "*/*"),
    ("/rooms/1/refresh", "text/vnd.turbo-stream.html"),
    ("/rooms/1/refresh?since=nonsense", "text/vnd.turbo-stream.html"),
    ("/rooms/1/refresh.turbo_stream", "text/html"),
    ("/rooms/1/refresh.turbo_stream", "application/json"),
    ("/account/users", None),
    ("/account/users", "text/html"),
    ("/account/users", "*/*"),
    ("/account/users", "text/vnd.turbo-stream.html"),
    ("/account/users.turbo_stream", "text/html"),
    ("/account/users.turbo_stream", "application/json"),
]


def check(port, cookie):
    results = []
    for path, accept in CASES:
        headers = {"Cookie": cookie}
        if accept is not None:
            headers["Accept"] = accept
        request = urllib.request.Request(f"http://127.0.0.1:{port}{path}", headers=headers)
        try:
            response = urllib.request.urlopen(request, timeout=20)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            body = response.read()
            content_type = response.headers.get("Content-Type", "").split(";")[0]
            results.append((response.status, content_type))
            if response.status == 406 and accept == "application/json":
                assert body == b'{"status":406,"error":"Not Acceptable"}', (path, body)
            if response.status == 200 and path.startswith("/rooms/"):
                assert body.strip() == b"", (path, body[:100])
    return results


def main():
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=SOURCE, text=True).strip() == REVISION
    with tempfile.TemporaryDirectory(prefix="paired-turbo-formats-") as scratch:
        temp = pathlib.Path(scratch)
        rust_db, camp_db = temp / "rust.sqlite3", temp / "camp.sqlite3"
        rust_port, camp_port, redis_port = free_port(), free_port(), free_port()
        seed_rustfire(rust_db, rust_port, [])
        camp_env = seed_campfire(SOURCE, RUBY, BUNDLE, SOURCE / "storage/db/production.sqlite3", camp_db, [], camp_port, temp)
        camp_env["REDIS_URL"] = f"redis://127.0.0.1:{redis_port}"
        camp_env["WEB_CONCURRENCY"] = "1"
        redis, redis_log = start_redis(temp, redis_port)
        try:
            rust = start_server(rust_db, rust_port, {"RUSTFIRE_CAMPFIRE_SECRET_KEY_BASE": camp_env["SECRET_KEY_BASE"]})
            try:
                rust_results = check(rust_port, "session_token=benchmark-session")
            finally:
                stop_server(rust)
            with open(temp / "puma.log", "w+") as log:
                camp = subprocess.Popen([str(RUBY), str(RUBY.parent / "bundle"), "exec", "puma", "-C", "config/puma.rb"], cwd=SOURCE, env=camp_env, stdout=log, stderr=log)
                try:
                    wait_for_server(camp_port, camp)
                    cookie, _ = login_campfire(camp_port)
                    camp_results = check(camp_port, cookie)
                except Exception:
                    log.flush()
                    log.seek(0)
                    print(log.read()[-3000:])
                    raise
                finally:
                    stop_server(camp)
        finally:
            redis.terminate()
            redis.wait(timeout=10)
            redis_log.close()
    assert rust_results == camp_results, list(zip(CASES, rust_results, camp_results))
    print("PASS paired Turbo format negotiation for room refresh and account user pages")


if __name__ == "__main__":
    main()
