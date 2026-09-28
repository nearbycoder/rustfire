"""Compare search query parsing, results, and errors with pinned Campfire."""

import hashlib
import http.client
import pathlib
import re
import sqlite3
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
RAW_QUERIES = (
    "q%5B%5D=benchmark",
    "q%5Bvalue%5D=benchmark",
    "q=42&q=benchmark",
    "q=benchmark&q=42",
    "q=benchmark&q%5B%5D=42",
    "q%5B%5D=benchmark&q=42",
)
FORMAT_CASES = (
    ("nested query with JSON Accept", "q%5B%5D=benchmark", "application/json", ""),
    ("nested query with JSON suffix", "q%5B%5D=benchmark", "text/html", ".json"),
    ("mixed query with JSON Accept", "q=benchmark&q%5B%5D=42", "application/json", ""),
    ("ordinary query with JSON Accept", "q=benchmark", "application/json", ""),
)
POST_CASES = (
    ("query overrides body", "q=42", "q=benchmark"),
    ("query array overrides body", "q%5B%5D=42", "q=benchmark"),
    ("query scalar overrides body array", "q=42", "q%5B%5D=benchmark"),
    ("body array", "", "q%5B%5D=42"),
    ("body scalar then array", "", "q=42&q%5B%5D=benchmark"),
    ("body array then scalar", "", "q%5B%5D=benchmark&q=42"),
    ("body duplicate scalar", "", "q=benchmark&q=42"),
    ("missing q", "", "foo=bar"),
)
POST_FORMAT_CASES = (
    ("array with JSON Accept", "", "q%5B%5D=42", "application/json"),
    ("mixed with JSON Accept", "", "q=42&q%5B%5D=benchmark", "application/json"),
    ("missing with JSON Accept", "", "foo=bar", "application/json"),
    ("valid with JSON Accept", "", "q=42", "application/json"),
)


def request(port, cookie, query, raw=False, accept="text/html", suffix=""):
    path = "/searches" + suffix + "?" + (query if raw else urllib.parse.urlencode({"q": query}))
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    try:
        connection.request("GET", path, headers={"Cookie": cookie, "Accept": accept})
        response = connection.getresponse()
        body = response.read()
        if response.status == 200:
            ids = re.findall(rb'id=["\']message_(fixture-\d+)["\']', body)
            return response.status, response.getheader("Content-Type"), tuple(id.decode() for id in ids)
        return response.status, response.getheader("Content-Type"), (len(body), hashlib.sha256(body).hexdigest())
    finally:
        connection.close()


def post_request(port, cookie, csrf, database, query, body, accept="text/html"):
    path = "/searches" + ("?" + query if query else "")
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    try:
        connection.request("POST", path, body=body, headers={"Cookie": cookie, "Accept": accept,
            "Content-Type": "application/x-www-form-urlencoded", "X-CSRF-Token": csrf})
        response = connection.getresponse()
        payload = response.read()
        location = response.getheader("Location")
        target = urllib.parse.urlsplit(location) if location else None
        with sqlite3.connect(database) as db:
            saved = tuple(query for (query,) in db.execute("SELECT query FROM searches WHERE user_id=1 ORDER BY id"))
        media = response.getheader("Content-Type", "")
        return (response.status, media if response.status >= 400 else media.split(";", 1)[0],
            target.path + ("?" + target.query if target.query else "") if target else None,
            (len(payload), hashlib.sha256(payload).hexdigest()) if response.status >= 400 else None, saved)
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
        for database in (rust_db, camp_db):
            with sqlite3.connect(database) as db:
                db.execute("DELETE FROM searches")
        rust = start_server(rust_db, rust_port, {})
        try:
            rust_results = [request(rust_port, "session_token=benchmark-session", query) for query in QUERIES]
            rust_results += [request(rust_port, "session_token=benchmark-session", query, True) for query in RAW_QUERIES]
            rust_formats = [request(rust_port, "session_token=benchmark-session", query, True, accept, suffix)
                for _, query, accept, suffix in FORMAT_CASES]
            rust_posts = [post_request(rust_port, "session_token=benchmark-session", "benchmark-csrf", rust_db, query, body)
                for _, query, body in POST_CASES]
            rust_post_formats = [post_request(rust_port, "session_token=benchmark-session", "benchmark-csrf", rust_db, query, body, accept)
                for _, query, body, accept in POST_FORMAT_CASES]
        finally:
            stop_server(rust)
        camp_env["WEB_CONCURRENCY"] = "1"
        with open(temp / "puma.log", "w+") as log:
            camp = subprocess.Popen([str(RUBY), str(RUBY.parent / "bundle"), "exec", "puma", "-C", "config/puma.rb"],
                                    cwd=REPOSITORY, env=camp_env, stdout=log, stderr=log)
            try:
                wait_for_server(camp_port, camp)
                cookie, csrf = login_campfire(camp_port)
                camp_results = [request(camp_port, cookie, query) for query in QUERIES]
                camp_results += [request(camp_port, cookie, query, True) for query in RAW_QUERIES]
                camp_formats = [request(camp_port, cookie, query, True, accept, suffix)
                    for _, query, accept, suffix in FORMAT_CASES]
                camp_posts = [post_request(camp_port, cookie, csrf, camp_db, query, body)
                    for _, query, body in POST_CASES]
                camp_post_formats = [post_request(camp_port, cookie, csrf, camp_db, query, body, accept)
                    for _, query, body, accept in POST_FORMAT_CASES]
            except Exception:
                log.flush()
                log.seek(0)
                print(log.read()[-2000:])
                raise
            finally:
                stop_server(camp)
    cases = QUERIES + RAW_QUERIES
    mismatches = [(query, rust, camp) for query, rust, camp in zip(cases, rust_results, camp_results) if rust != camp]
    for query, rust, camp in mismatches:
        print(f"{query!r}: Rustfire={rust!r} Campfire={camp!r}")
    format_mismatches = [(label, rust, camp) for (label, _, _, _), rust, camp in zip(FORMAT_CASES, rust_formats, camp_formats) if rust != camp]
    for label, rust, camp in format_mismatches:
        print(f"GET {label!r}: Rustfire={rust!r} Campfire={camp!r}")
    post_mismatches = [(label, rust, camp) for (label, _, _), rust, camp in zip(POST_CASES, rust_posts, camp_posts) if rust != camp]
    for label, rust, camp in post_mismatches:
        print(f"POST {label!r}: Rustfire={rust!r} Campfire={camp!r}")
    post_format_mismatches = [(label, rust, camp) for (label, _, _, _), rust, camp in zip(POST_FORMAT_CASES, rust_post_formats, camp_post_formats) if rust != camp]
    for label, rust, camp in post_format_mismatches:
        print(f"POST {label!r}: Rustfire={rust!r} Campfire={camp!r}")
    assert not mismatches, f"{len(mismatches)} search query cases differ"
    assert not format_mismatches, f"{len(format_mismatches)} search format cases differ"
    assert not post_mismatches, f"{len(post_mismatches)} search POST cases differ"
    assert not post_format_mismatches, f"{len(post_format_mismatches)} search POST format cases differ"
    print(f"PASS {len(cases)} GET, {len(FORMAT_CASES)} GET format, {len(POST_CASES)} POST, and {len(POST_FORMAT_CASES)} POST format search cases match Campfire")


if __name__ == "__main__":
    main()
