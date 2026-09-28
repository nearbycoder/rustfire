"""Measure checked concurrent sign-ins against matched Campfire and Rustfire users."""

import argparse
import concurrent.futures
import hashlib
import json
import pathlib
import re
import sqlite3
import statistics
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

from direct_lookup import free_port, start_server, stop_server
from paired_banned_content import BUNDLE, REPOSITORY, REVISION, RUBY, isolated_campfire, start_redis
from paired_direct_lookup import seed_campfire, seed_rustfire, wait_for_server
from paired_join import browser


def request(opener, port, path, data=None, ip=None, csrf=None):
    headers = {"Accept": "text/html"}
    if data is not None:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    if ip is not None:
        headers["X-Forwarded-For"] = ip
    if csrf is not None:
        headers["X-CSRF-Token"] = csrf
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data, headers=headers)
    try:
        response = opener.open(req, timeout=30)
    except urllib.error.HTTPError as error:
        response = error
    with response:
        return response.status, urllib.parse.urlsplit(response.headers.get("Location", "")).path, response.read()


def sign_in(port, index):
    ip = f"203.0.113.{index + 1}"
    opener = browser()
    start = time.monotonic()
    status, _, page = request(opener, port, "/session/new", ip=ip)
    if status != 200:
        return index, ip, status, "", time.monotonic() - start
    csrf = re.search(rb'<meta name=[\'\"]csrf-token[\'\"] content=[\'\"]([^\'\"]+)', page)
    assert csrf, "Sign-in form has no CSRF token"
    body = urllib.parse.urlencode({"email_address": "benchmark@example.invalid", "password": "benchmark-password"}).encode()
    status, location, _ = request(opener, port, "/session", body, ip, csrf.group(1).decode())
    return index, ip, status, location, time.monotonic() - start


def trial(port, database, clients, count):
    started = time.monotonic()
    with concurrent.futures.ThreadPoolExecutor(max_workers=clients) as executor:
        rows = sorted(executor.map(lambda index: sign_in(port, index), range(count)))
    elapsed = time.monotonic() - started
    failures = [(index, status, location) for index, _, status, location, _ in rows if (status, location) != (302, "/")]
    with sqlite3.connect(database) as db:
        saved = sorted(row[0] for row in db.execute("SELECT ip_address FROM sessions WHERE ip_address LIKE '203.0.113.%'"))
    expected = sorted(row[1] for row in rows)
    assert saved == expected, (len(saved), len(expected), saved[:4], expected[:4])
    assert not failures, failures[:12]
    latencies = sorted(row[4] * 1000 for row in rows)
    return {"requests": count, "clients": clients, "successes": count, "errors": 0,
            "elapsed_s": elapsed, "logins_per_s": count / elapsed,
            "p50_ms": statistics.median(latencies), "p95_ms": latencies[int((len(latencies) - 1) * .95)]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--clients", type=int, default=32)
    parser.add_argument("--requests", type=int, default=128)
    parser.add_argument("--campfire-workers", type=int, default=22)
    parser.add_argument("--rustfire-first", action="store_true")
    parser.add_argument("--report", type=pathlib.Path)
    args = parser.parse_args()
    assert 1 <= args.clients <= args.requests <= 253
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPOSITORY, text=True).strip() == REVISION
    with tempfile.TemporaryDirectory(prefix="paired-login-capacity-") as scratch:
        temp = pathlib.Path(scratch)
        rust_db, camp_db = temp / "rust.sqlite3", temp / "camp.sqlite3"
        rust_port, camp_port, redis_port = free_port(), free_port(), free_port()
        seed_rustfire(rust_db, rust_port, [])
        checkout = isolated_campfire(temp, redis_port)
        camp_env = seed_campfire(checkout, RUBY, BUNDLE, REPOSITORY / "storage/db/production.sqlite3", camp_db, [], camp_port, temp)
        camp_env["REDIS_URL"] = f"redis://127.0.0.1:{redis_port}"
        camp_env["WEB_CONCURRENCY"] = str(args.campfire_workers)
        with sqlite3.connect(camp_db) as db:
            digest = db.execute("SELECT password_digest FROM users WHERE id=1").fetchone()[0]
        with sqlite3.connect(rust_db) as db:
            db.execute("UPDATE users SET email_address='benchmark@example.invalid',password_digest=? WHERE id=1", [digest])
        redis, redis_log = start_redis(temp, redis_port)
        try:
            def run_rust():
                process = start_server(rust_db, rust_port, {"RUSTFIRE_TRUSTED_PROXY_IPS": "127.0.0.1"})
                try:
                    return trial(rust_port, rust_db, args.clients, args.requests)
                finally:
                    stop_server(process)

            def run_camp():
                with open(temp / "puma.log", "w+") as log:
                    process = subprocess.Popen([str(RUBY), str(RUBY.parent / "bundle"), "exec", "puma", "-C", "config/puma.rb"],
                                               cwd=checkout, env=camp_env, stdout=log, stderr=log)
                    try:
                        wait_for_server(camp_port, process)
                        return trial(camp_port, camp_db, args.clients, args.requests)
                    except Exception:
                        log.flush()
                        log.seek(0)
                        print(log.read()[-2500:])
                        raise
                    finally:
                        stop_server(process)

            if args.rustfire_first:
                rust, camp = run_rust(), run_camp()
            else:
                camp, rust = run_camp(), run_rust()
        finally:
            redis.terminate()
            redis.wait(timeout=10)
            redis_log.close()
    binary = pathlib.Path(__file__).resolve().parents[1] / "target/release/rustfire"
    report = {"source_revision": REVISION, "rustfire_revision": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
              "rustfire_binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
              "rustfire_first": args.rustfire_first, "campfire_workers": args.campfire_workers,
              "rustfire": rust, "campfire": camp}
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(json.dumps(report, sort_keys=True))
    print("PASS checked concurrent sign-in responses and saved source IPs")


if __name__ == "__main__":
    main()
