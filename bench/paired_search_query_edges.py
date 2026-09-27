"""Compare search query parsing, results, and errors with pinned Campfire."""

import hashlib
import http.client
import pathlib
import re
import subprocess
import tempfile
import urllib.parse

from direct_lookup import free_port, start_server, stop_server
from paired_direct_lookup import login_campfire, seed_campfire, seed_rustfire, wait_for_server
from paired_search import seed_messages


REPOSITORY = pathlib.Path("/tmp/once-campfire-reference")
RUBY = pathlib.Path("/tmp/rustfire-baseline/local/bin/ruby")
BUNDLE = pathlib.Path("/tmp/rustfire-baseline/bundle")
REVISION = "91d294f4a09f9bbe37f9548959bfcb43645678fb"
QUERIES = (
    "benchmark", "benchmark!", "benchmark*", '"benchmark"', "benchmark OR message",
    "benchmark AND message", "benchmark NOT message", "OR", "AND", "NOT",
    '"', "*", "()", "!!!", " ", "", "benchmark+message", "benchmark_message",
    "café", "你好", "🚀", "123", "benchmark\tmessage", "benchmark\nmessage",
)


def request(port, cookie, query):
    path = "/searches?" + urllib.parse.urlencode({"q": query})
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    try:
        connection.request("GET", path, headers={"Cookie": cookie, "Accept": "text/html"})
        response = connection.getresponse()
        body = response.read()
        if response.status == 200:
            ids = re.findall(rb'id=["\']message_(fixture-\d+)["\']', body)
            return response.status, response.getheader("Content-Type"), tuple(id.decode() for id in ids)
        return response.status, response.getheader("Content-Type"), (len(body), hashlib.sha256(body).hexdigest())
    finally:
        connection.close()


def main():
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPOSITORY, text=True).strip() == REVISION
    with tempfile.TemporaryDirectory(prefix="paired-search-query-edges-") as scratch:
        temp = pathlib.Path(scratch)
        rust_db, camp_db = temp / "rust.sqlite3", temp / "camp.sqlite3"
        rust_port, camp_port = free_port(), free_port()
        seed_rustfire(rust_db, rust_port, [])
        camp_env = seed_campfire(REPOSITORY, RUBY, BUNDLE, REPOSITORY / "storage/db/production.sqlite3", camp_db, [], camp_port, temp)
        seed_messages(rust_db, camp_db, 100)
        rust = start_server(rust_db, rust_port, {})
        try:
            rust_results = [request(rust_port, "session_token=benchmark-session", query) for query in QUERIES]
        finally:
            stop_server(rust)
        camp_env["WEB_CONCURRENCY"] = "1"
        with open(temp / "puma.log", "w+") as log:
            camp = subprocess.Popen([str(RUBY), str(RUBY.parent / "bundle"), "exec", "puma", "-C", "config/puma.rb"],
                                    cwd=REPOSITORY, env=camp_env, stdout=log, stderr=log)
            try:
                wait_for_server(camp_port, camp)
                cookie, _ = login_campfire(camp_port)
                camp_results = [request(camp_port, cookie, query) for query in QUERIES]
            except Exception:
                log.flush()
                log.seek(0)
                print(log.read()[-2000:])
                raise
            finally:
                stop_server(camp)
    mismatches = [(query, rust, camp) for query, rust, camp in zip(QUERIES, rust_results, camp_results) if rust != camp]
    for query, rust, camp in mismatches:
        print(f"{query!r}: Rustfire={rust!r} Campfire={camp!r}")
    assert not mismatches, f"{len(mismatches)} search query cases differ"
    print(f"PASS {len(QUERIES)} paired search query cases match Campfire")


if __name__ == "__main__":
    main()
