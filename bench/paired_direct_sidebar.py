"""Compare Campfire and Rustfire direct-room sidebar links on matching fixtures.

Requires a release Rustfire build and the pinned Campfire checkout/bundle.
Uses disposable SQLite databases and an isolated Redis server.
"""

import argparse
import json
import pathlib
import re
import sqlite3
import subprocess
import tempfile
import time
import urllib.request
import urllib.parse

from direct_lookup import ROOT, free_port, start_server, stop_server
from paired_banned_content import REPOSITORY, RUBY, BUNDLE, REVISION, isolated_campfire, start_redis
from paired_direct_lookup import login_campfire, seed_campfire, seed_rustfire, wait_for_server


SECRET = "paired-direct-sidebar-secret"


def direct_link(page, room_id):
    match = re.search(rf'<a\b[^>]*\bid="list_rooms_direct_{room_id}"[^>]*>.*?</a>', page, re.S)
    if not match:
        raise AssertionError(f"Direct room {room_id} link missing from sidebar")
    return match.group(0)


def request(port, path, cookie, csrf=None, user_ids=()):
    body = None if csrf is None else urllib.parse.urlencode([("user_ids[]", value) for value in user_ids] + [("authenticity_token", csrf)]).encode()
    headers = {"Cookie": cookie}
    if csrf is not None:
        headers.update({"Content-Type": "application/x-www-form-urlencoded", "X-CSRF-Token": csrf})
    with urllib.request.urlopen(urllib.request.Request(f"http://127.0.0.1:{port}{path}", headers=headers, data=body), timeout=15) as response:
        return response.status, response.url, response.read().decode()


def run(port, cookie, csrf, capture_dir):
    process = subprocess.Popen(
        ["node", "bench/capture_direct_sidebar.mjs", "--base", f"http://127.0.0.1:{port}", "--cookie", cookie, "--count", "4", "--output", str(capture_dir)],
        cwd=ROOT, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    try:
        ready = process.stdout.readline().strip()
        if ready != "READY":
            output, error = process.communicate(timeout=10)
            raise AssertionError(f"Direct-room capture did not start: {ready}\n{output}\n{error}")
        results = []
        for members in ((2,), (2, 3), (2, 3, 4), (2, 3, 4, 5, 6)):
            status, url, _ = request(port, "/rooms/directs", cookie, csrf, members)
            assert status == 200, (status, url)
            room_id = int(url.rstrip("/").rsplit("/", 1)[1])
            assert room_id == len(results) + 2, (room_id, results)
            _, _, sidebar = request(port, "/users/me/sidebar", cookie)
            results.append(direct_link(sidebar, room_id))
        output, error = process.communicate(timeout=35)
        assert process.returncode == 0, (output, error)
        received = json.loads(output.strip().splitlines()[-1])
        assert received == {"received": 4, "unexpected": 0, "ids": [2, 3, 4, 5]}, received
        streams = [(capture_dir / f"{room_id}.html").read_text() for room_id in (2, 3, 4, 5)]
        return results, streams
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()


def normalized(html):
    epoch = re.search(r'data-sorted-list-number="(\d+)"', html)
    assert epoch and abs(int(epoch.group(1)) - int(time.time() * 1000)) < 30_000, "Direct room sort time is not a current epoch millisecond value"
    return re.sub(r'data-sorted-list-number="\d+"', 'data-sorted-list-number="<epoch-ms>"', html)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sample-dir", type=pathlib.Path)
    args = parser.parse_args()
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPOSITORY, text=True).strip()
    assert revision == REVISION, revision
    with tempfile.TemporaryDirectory(prefix="paired-direct-sidebar-") as temporary:
        temp = pathlib.Path(temporary)
        rust_db, camp_db = temp / "rust.sqlite3", temp / "camp.sqlite3"
        rust_port, camp_port, redis_port = free_port(), free_port(), free_port()
        checkout = isolated_campfire(temp, redis_port)
        seed_rustfire(rust_db, rust_port, [])
        env = seed_campfire(checkout, RUBY, BUNDLE, REPOSITORY / "storage/db/production.sqlite3", camp_db, [], camp_port, temp)
        env["REDIS_URL"] = f"redis://127.0.0.1:{redis_port}"
        env["SECRET_KEY_BASE"] = SECRET
        with sqlite3.connect(camp_db) as db:
            db.execute("UPDATE users SET name='User '||id,updated_at='2026-01-01 00:00:00.000000' WHERE id IN (1,2)")

        rust = start_server(rust_db, rust_port, {"RUSTFIRE_CAMPFIRE_SECRET_KEY_BASE": SECRET})
        try:
            rust_links, rust_streams = run(rust_port, "session_token=benchmark-session", "benchmark-csrf", temp / "rust-streams")
        finally:
            stop_server(rust)

        redis, redis_log = start_redis(temp, redis_port)
        log = open(temp / "puma.log", "w+")
        camp = None
        try:
            camp = subprocess.Popen([str(RUBY), str(RUBY.parent / "bundle"), "exec", "puma", "-C", "config/puma.rb"], cwd=checkout, env=env, stdout=log, stderr=log)
            wait_for_server(camp_port, camp)
            cookie, csrf = login_campfire(camp_port)
            camp_links, camp_streams = run(camp_port, cookie, csrf, temp / "camp-streams")
        finally:
            if camp is not None:
                stop_server(camp)
            redis.terminate()
            redis.wait(timeout=10)
            redis_log.close()
            log.close()

        if args.sample_dir:
            args.sample_dir.mkdir(parents=True, exist_ok=True)
            for index, (rust_link, camp_link) in enumerate(zip(rust_links, camp_links), 2):
                (args.sample_dir / f"rustfire-{index}.html").write_text(rust_link)
                (args.sample_dir / f"campfire-{index}.html").write_text(camp_link)
                (args.sample_dir / f"rustfire-stream-{index}.html").write_text(rust_streams[index - 2])
                (args.sample_dir / f"campfire-stream-{index}.html").write_text(camp_streams[index - 2])
        for index, (rust_link, camp_link) in enumerate(zip(rust_links, camp_links), 2):
            assert normalized(rust_link) == normalized(camp_link), f"Direct room {index} markup differs; use --sample-dir to inspect"
            assert normalized(rust_streams[index - 2]) == normalized(camp_streams[index - 2]), f"Direct room {index} Turbo event differs; use --sample-dir to inspect"
        print("PASS paired direct sidebar links and Turbo events for one, two, three, and five peers; only room epoch milliseconds normalized")


if __name__ == "__main__":
    main()
