"""Compare profile behavior and read throughput with pinned ONCE Campfire.

Run after ``cargo build --release`` with the pinned Ruby bundle and Redis available.
The two disposable databases each contain the same user and open-room membership.
"""

import argparse
import concurrent.futures
import html
import http.client
import http.cookiejar
import math
import pathlib
import re
import sqlite3
import statistics
import subprocess
import tempfile
import threading
import time
import urllib.parse
import urllib.request

from direct_lookup import free_port, start_server, stop_server
from paired_direct_lookup import login_campfire, seed_campfire, seed_rustfire, wait_for_server
from paired_bot_admin import request


TRANSFER = re.compile(r"/session/transfers/[A-Za-z0-9_-]+--[0-9a-f]{64}")
PROFILE_FIELDS = ("user[avatar]", "user[name]", "user[email_address]", "user[password]", "user[bio]")


def transfer_path(page):
    match = TRANSFER.search(html.unescape(page.decode()))
    assert match, "Profile has no signed device-transfer link"
    return match.group()


def use_transfer(port, path):
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    base = f"http://127.0.0.1:{port}"
    with opener.open(base + path) as response:
        page = response.read().decode()
        assert response.status == 200
    assert "auto-submit" in page and ('name="_method"' in page or "name='_method'" in page)
    token = re.search(r'name=["\']authenticity_token["\'] value=["\']([^"\']+)', page)
    assert token, "Device-transfer form has no CSRF token"
    body = urllib.parse.urlencode({"_method": "put", "authenticity_token": html.unescape(token.group(1))}).encode()
    with opener.open(base + path, data=body) as response:
        assert response.status == 200 and "/rooms/1" in response.url, (response.status, response.url)
        response.read()


def measure(port, cookie, concurrency, requests):
    assert requests % concurrency == 0
    barrier = threading.Barrier(concurrency)

    def worker():
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
        samples = []
        lengths = set()
        try:
            for _ in range(2):
                connection.request("GET", "/users/me/profile", headers={"Cookie": cookie, "Accept": "text/html"})
                response = connection.getresponse()
                response.read()
                assert response.status == 200
            barrier.wait(timeout=30)
            run_start = time.perf_counter()
            for _ in range(requests // concurrency):
                started = time.perf_counter()
                connection.request("GET", "/users/me/profile", headers={"Cookie": cookie, "Accept": "text/html"})
                response = connection.getresponse()
                payload = response.read()
                samples.append((time.perf_counter() - started) * 1000)
                assert response.status == 200, response.status
                lengths.add(len(payload))
            run_end = time.perf_counter()
        finally:
            connection.close()
        return samples, lengths, run_start, run_end

    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = [pool.submit(worker) for _ in range(concurrency)]
        results = [future.result() for future in futures]
    elapsed = max(end for _, _, _, end in results) - min(start for _, _, start, _ in results)
    samples = [value for batch, _, _, _ in results for value in batch]
    lengths = sorted({length for _, batch, _, _ in results for length in batch})
    assert len(samples) == requests
    return {
        "rps": round(requests / elapsed, 1),
        "median_ms": round(statistics.median(samples), 2),
        "p95_ms": round(sorted(samples)[math.ceil(.95 * len(samples)) - 1], 2),
        "bytes": lengths,
    }


def verify_mutations(port, cookie, csrf, database):
    body = urllib.parse.urlencode({
        "_method": "patch",
        "user[name]": "Paired Admin",
        "user[email_address]": "paired@example.invalid",
        "user[bio]": "Profile parity check",
    }).encode()
    status, location, payload = request(port, "POST", "/users/me/profile", cookie, csrf, body, "application/x-www-form-urlencoded")
    assert status == 302 and urllib.parse.urlsplit(location).path == "/users/me/profile", (status, location, payload[:200])
    with sqlite3.connect(database) as db:
        profile = db.execute("SELECT name,email_address,bio FROM users WHERE id=1").fetchone()
    assert profile == ("Paired Admin", "paired@example.invalid", "Profile parity check"), profile
    status, _, page = request(port, "GET", "/users/me/profile", cookie, csrf)
    assert status == 200 and b"Profile parity check" in page
    body = urllib.parse.urlencode({"_method": "put"}).encode()
    status, location, payload = request(port, "POST", "/rooms/1/involvement?involvement=everything", cookie, csrf, body, "application/x-www-form-urlencoded")
    assert status == 302 and urllib.parse.urlsplit(location).path == "/rooms/1/involvement", (status, location, payload[:200])
    with sqlite3.connect(database) as db:
        involvement = db.execute("SELECT involvement FROM memberships WHERE room_id=1 AND user_id=1").fetchone()[0]
    assert involvement == "everything", involvement
    return profile, involvement


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--requests", type=int, default=160)
    parser.add_argument("--campfire-repo", type=pathlib.Path, default=pathlib.Path("/tmp/once-campfire-reference"))
    parser.add_argument("--ruby", type=pathlib.Path, default=pathlib.Path("/tmp/rustfire-baseline/local/bin/ruby"))
    parser.add_argument("--bundle-path", type=pathlib.Path, default=pathlib.Path("/tmp/rustfire-baseline/bundle"))
    args = parser.parse_args()
    if args.requests < 32 or args.requests % 32:
        parser.error("--requests must be a multiple of 32")
    repository, ruby, bundle_path = args.campfire_repo.resolve(), args.ruby.resolve(), args.bundle_path.resolve()
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repository, text=True).strip()
    assert revision == "91d294f4a09f9bbe37f9548959bfcb43645678fb", revision
    with tempfile.TemporaryDirectory(prefix="paired-profile-") as directory:
        temp = pathlib.Path(directory)
        rust_db, camp_db = temp / "rustfire.sqlite3", temp / "campfire.sqlite3"
        rust_port, camp_port = free_port(), free_port()
        seed_rustfire(rust_db, rust_port, [])
        env = seed_campfire(repository, ruby, bundle_path, repository / "storage/db/production.sqlite3", camp_db, [], camp_port, temp)
        env["WEB_CONCURRENCY"] = "4"
        with sqlite3.connect(rust_db) as db:
            db.execute("UPDATE users SET name='Test Admin',email_address='benchmark@example.invalid' WHERE id=1")
        rust = start_server(rust_db, rust_port, {"RUSTFIRE_CAMPFIRE_SECRET_KEY_BASE": env["SECRET_KEY_BASE"]})
        log = open(temp / "puma.log", "w+")
        camp = subprocess.Popen([str(ruby), str(ruby.parent / "bundle"), "exec", "puma", "-C", "config/puma.rb"], cwd=repository, env=env, stdout=log, stderr=log)
        try:
            wait_for_server(camp_port, camp)
            camp_cookie, camp_csrf = login_campfire(camp_port)
            rust_cookie, rust_csrf = "session_token=benchmark-session", "benchmark-csrf"
            camp_status, _, camp_page = request(camp_port, "GET", "/users/me/profile", camp_cookie, camp_csrf)
            rust_status, _, rust_page = request(rust_port, "GET", "/users/me/profile", rust_cookie, rust_csrf)
            assert (camp_status, rust_status) == (200, 200)
            for field in PROFILE_FIELDS:
                assert field.encode() in camp_page and field.encode() in rust_page, field
            with sqlite3.connect(rust_db) as db:
                transfers_before = db.execute("SELECT COUNT(*) FROM session_transfers").fetchone()[0]
            for _ in range(3):
                request(rust_port, "GET", "/users/me/profile", rust_cookie, rust_csrf)
            with sqlite3.connect(rust_db) as db:
                assert db.execute("SELECT COUNT(*) FROM session_transfers").fetchone()[0] == transfers_before
            use_transfer(rust_port, transfer_path(camp_page))
            use_transfer(camp_port, transfer_path(rust_page))
            print("profile fields, stateless reads, and two-way transfer: passed")
            print("GET /users/me/profile; 4 Puma workers; release Rustfire; requests per run:", args.requests)
            for concurrency in (1, 8, 32):
                source = measure(camp_port, camp_cookie, concurrency, args.requests)
                target = measure(rust_port, rust_cookie, concurrency, args.requests)
                print(f"concurrency={concurrency}: Campfire={source}; Rustfire={target}; throughput_ratio={target['rps']/source['rps']:.2f}x")
            source_mutations = verify_mutations(camp_port, camp_cookie, camp_csrf, camp_db)
            target_mutations = verify_mutations(rust_port, rust_cookie, rust_csrf, rust_db)
            assert source_mutations == target_mutations
            print("profile fields and involvement mutation: passed", source_mutations)
        except Exception:
            log.flush()
            log.seek(0)
            print(log.read()[-3000:])
            raise
        finally:
            stop_server(camp)
            stop_server(rust)
            log.close()


if __name__ == "__main__":
    main()
