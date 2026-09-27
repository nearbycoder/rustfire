"""Compare bot JSON pagination cursor precedence and malformed cursor responses."""

import argparse
import http.client
import pathlib
import sqlite3
import subprocess
import tempfile

from direct_lookup import free_port, start_server, stop_server
from paired_banned_content import start_redis
from paired_bot_messages import PATH, seed_messages
from paired_direct_lookup import seed_campfire, seed_rustfire, wait_for_server
from paired_turbo_fanout import MessageTagSequence


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
FORMAT_CASES = (
    ("", "text/html"), ("", "text/vnd.turbo-stream.html"), ("", "*/*"),
    (".json", "text/html"), (".html", "application/json"), (".xml", "application/json"),
    ("?format=json", "text/html"), ("?format=html", "application/json"),
    ("?format=xml", "application/json"), (".json?format=html", "application/json"),
    (".html?format=json", "text/html"),
)


def response(port, query, accept="application/json"):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    try:
        path = PATH + query
        connection.request("GET", path, headers={"Accept": accept})
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


def comparable(result):
    status, media, total, link, body = result
    if status == 200 and media.startswith("text/html"):
        parser = MessageTagSequence()
        parser.feed(body.decode())
        attributes = []
        for tag, pairs in parser.attributes:
            values = dict(pairs)
            if tag == "input" and values.get("name") == "authenticity_token":
                assert values.get("value"), "generated authenticity token is empty"
                values["value"] = "GENERATED_TOKEN"
            attributes.append((tag, tuple(sorted(values.items()))))
        return (status, media, total, link, parser.tags, attributes, parser.text)
    if (status == 200 and media.startswith("application/json")) or media.startswith("application/xml"):
        return result
    return (status, media, total, link, None)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--formats", action="store_true", help="also compare explicit format and Accept negotiation")
    parser.add_argument("--sample-dir", type=pathlib.Path, help="write source and Rustfire HTML responses for inspection")
    args = parser.parse_args()
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
        with sqlite3.connect(camp_db) as camp, sqlite3.connect(rust_db) as rust:
            room_name = camp.execute("SELECT name FROM rooms WHERE id=1").fetchone()[0]
            updated_at = camp.execute("SELECT updated_at FROM messages WHERE id=1").fetchone()[0]
            rust.execute("UPDATE rooms SET name=? WHERE id=1", (room_name,))
            rust.execute("UPDATE messages SET updated_at=?", (updated_at,))
        redis, redis_log = start_redis(temp, redis_port)
        try:
            rust = start_server(rust_db, rust_port, {"RUSTFIRE_CAMPFIRE_SECRET_KEY_BASE": environment["SECRET_KEY_BASE"]})
            try:
                rust_results = {query: response(rust_port, query) for query in QUERIES}
                rust_formats = {case: response(rust_port, *case) for case in FORMAT_CASES} if args.formats else {}
            finally:
                stop_server(rust)
            with open(temp / "puma.log", "w+") as log:
                camp = subprocess.Popen([str(RUBY), str(RUBY.parent / "bundle"), "exec", "puma", "-C", "config/puma.rb"],
                    cwd=SOURCE, env=environment, stdout=log, stderr=log)
                try:
                    wait_for_server(camp_port, camp)
                    camp_results = {query: response(camp_port, query) for query in QUERIES}
                    camp_formats = {case: response(camp_port, *case) for case in FORMAT_CASES} if args.formats else {}
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
            for case in FORMAT_CASES if args.formats else ():
                rust_result, camp_result = rust_formats[case], camp_formats[case]
                if comparable(rust_result) != comparable(camp_result):
                    mismatches.append((case, summary(rust_result), summary(camp_result)))
                print(f"format {case}: rust={summary(rust_result)} camp={summary(camp_result)}")
            if args.sample_dir and args.formats:
                args.sample_dir.mkdir(parents=True, exist_ok=True)
                (args.sample_dir / "rustfire.html").write_bytes(rust_formats[(".html", "application/json")][4])
                (args.sample_dir / "campfire.html").write_bytes(camp_formats[(".html", "application/json")][4])
            assert not mismatches, mismatches
        finally:
            redis.terminate()
            redis.wait(timeout=10)
            redis_log.close()
    print("PASS paired bot pagination cursors, bodies, headers, and requested formats")


if __name__ == "__main__":
    main()
