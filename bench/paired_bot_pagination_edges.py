"""Compare bot JSON pagination cursor precedence and malformed cursor responses."""

import http.client
import pathlib
import subprocess
import tempfile

from direct_lookup import free_port, start_server, stop_server
from paired_banned_content import start_redis
from paired_bot_messages import PATH, seed_messages
from paired_direct_lookup import seed_campfire, seed_rustfire, wait_for_server


SOURCE = pathlib.Path("/tmp/once-campfire-reference")
RUBY = pathlib.Path("/tmp/rustfire-baseline/local/bin/ruby")
BUNDLE = pathlib.Path("/tmp/rustfire-baseline/bundle")
REVISION = "91d294f4a09f9bbe37f9548959bfcb43645678fb"
QUERIES = (
    "", "?before=60", "?after=20", "?before=60&after=20", "?before=60&after=bogus",
    "?before=60&after=", "?before=&after=20", "?before=bogus&after=20",
    "?after=0", "?before=0", "?before=", "?after=", "?before=%20", "?after=%20",
    "?before=bogus", "?after=bogus",
)


def response(port, query):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    try:
        connection.request("GET", PATH + query, headers={"Accept": "application/json"})
        received = connection.getresponse()
        body = received.read()
        origin = f"http://127.0.0.1:{port}".encode()
        link = received.getheader("Link", "").replace(origin.decode(), "ORIGIN")
        return (received.status, received.getheader("Content-Type", ""),
            received.getheader("X-Total-Count", ""), link, body.replace(origin, b"ORIGIN"))
    finally:
        connection.close()


def summary(result):
    status, media, total, link, body = result
    if media.startswith("application/json") and status == 200:
        import json
        ids = [message["id"] for message in json.loads(body)]
        return (status, media, total, link, len(ids), ids[:2], ids[-2:])
    return (status, media, total, link, len(body))


def main():
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=SOURCE, text=True).strip() == REVISION
    with tempfile.TemporaryDirectory(prefix="paired-bot-pagination-") as scratch:
        temp = pathlib.Path(scratch)
        rust_db, camp_db = temp / "rust.sqlite3", temp / "camp.sqlite3"
        rust_port, camp_port, redis_port = free_port(), free_port(), free_port()
        seed_rustfire(rust_db, rust_port, [])
        environment = seed_campfire(SOURCE, RUBY, BUNDLE, SOURCE / "storage/db/production.sqlite3", camp_db, [], camp_port, temp)
        environment["WEB_CONCURRENCY"] = "1"
        environment["REDIS_URL"] = f"redis://127.0.0.1:{redis_port}"
        seed_messages(rust_db, camp_db, 90, False)
        redis, redis_log = start_redis(temp, redis_port)
        try:
            rust = start_server(rust_db, rust_port, {"RUSTFIRE_CAMPFIRE_SECRET_KEY_BASE": environment["SECRET_KEY_BASE"]})
            try:
                rust_results = {query: response(rust_port, query) for query in QUERIES}
            finally:
                stop_server(rust)
            with open(temp / "puma.log", "w+") as log:
                camp = subprocess.Popen([str(RUBY), str(RUBY.parent / "bundle"), "exec", "puma", "-C", "config/puma.rb"],
                    cwd=SOURCE, env=environment, stdout=log, stderr=log)
                try:
                    wait_for_server(camp_port, camp)
                    camp_results = {query: response(camp_port, query) for query in QUERIES}
                except Exception:
                    log.flush()
                    log.seek(0)
                    print(log.read()[-3000:])
                    raise
                finally:
                    stop_server(camp)
            mismatches = []
            for query in QUERIES:
                rust_result, camp_result = rust_results[query], camp_results[query]
                if rust_result != camp_result:
                    mismatches.append((query, summary(rust_result), summary(camp_result)))
                print(f"{query or '(none)'}: rust={summary(rust_result)} camp={summary(camp_result)}")
            assert not mismatches, mismatches
        finally:
            redis.terminate()
            redis.wait(timeout=10)
            redis_log.close()
    print("PASS paired bot JSON pagination cursors, bodies, and headers")


if __name__ == "__main__":
    main()
