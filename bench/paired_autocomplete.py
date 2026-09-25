"""Compare Campfire and Rustfire user autocomplete on matched disposable fixtures.

Example:
  cargo build --release
  python bench/paired_autocomplete.py --users 10000 --iterations 30
"""

import argparse
import http.client
import json
import pathlib
import sqlite3
import statistics
import subprocess
import tempfile
import time

from direct_lookup import free_port, p95, start_server, stop_server
from paired_direct_lookup import login_campfire, seed_campfire, seed_rustfire, wait_for_server


def add_users(database, first, last):
    stamp = "2026-01-01T00:00:00Z"
    with sqlite3.connect(database) as db:
        db.executemany(
            "INSERT INTO users(id,name,role,status,created_at,updated_at) VALUES(?1,?2,0,0,?3,?3)",
            ((uid, f"User {uid}", stamp) for uid in range(first, last + 1)),
        )


def measure(port, path, cookie, iterations):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    samples = []
    lengths = set()
    selected = None
    try:
        for iteration in range(iterations + 2):
            started = time.perf_counter()
            connection.request("GET", path, headers={"Cookie": cookie, "Accept": "application/json"})
            response = connection.getresponse()
            body = response.read()
            elapsed = (time.perf_counter() - started) * 1000
            if response.status != 200:
                raise AssertionError((response.status, body[:300]))
            users = json.loads(body)
            if len(users) != 20 or any(set(user) != {"name", "value", "avatar_url", "sgid"} for user in users):
                raise AssertionError((len(users), users[:1]))
            identities = [(user["value"], user["name"]) for user in users]
            if selected is None:
                selected = identities
            elif identities != selected:
                raise AssertionError("Autocomplete results changed during trial")
            if iteration >= 2:
                samples.append(elapsed)
                lengths.add(len(body))
    finally:
        connection.close()
    return statistics.median(samples), p95(samples), lengths, selected


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--users", type=int, default=10000)
    parser.add_argument("--iterations", type=int, default=30)
    parser.add_argument("--campfire-repo", type=pathlib.Path, default=pathlib.Path("/tmp/once-campfire-reference"))
    parser.add_argument("--ruby", type=pathlib.Path, default=pathlib.Path("/tmp/rustfire-baseline/local/bin/ruby"))
    parser.add_argument("--bundle-path", type=pathlib.Path, default=pathlib.Path("/tmp/rustfire-baseline/bundle"))
    args = parser.parse_args()
    if args.users < 300 or args.iterations < 1:
        parser.error("users must be at least 300 and iterations must be positive")
    repository = args.campfire_repo.resolve()
    ruby = args.ruby.resolve()
    bundle_path = args.bundle_path.resolve()
    source_database = repository / "storage/db/production.sqlite3"
    if not source_database.is_file():
        parser.error("Campfire production fixture database is missing")
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repository, text=True).strip()
    if revision != "91d294f4a09f9bbe37f9548959bfcb43645678fb":
        parser.error(f"Campfire source is at {revision}, not the pinned compatibility target")

    with tempfile.TemporaryDirectory(prefix="paired-autocomplete-bench-") as temporary:
        temp = pathlib.Path(temporary)
        rust_db, camp_db = temp / "rustfire.sqlite3", temp / "campfire.sqlite3"
        rust_port, camp_port = free_port(), free_port()
        seed_rustfire(rust_db, rust_port, [])
        camp_env = seed_campfire(repository, ruby, bundle_path, source_database, camp_db, [], camp_port, temp)
        if args.users > 51:
            add_users(rust_db, 52, args.users)
            add_users(camp_db, 52, args.users)

        rust_process = start_server(rust_db, rust_port)
        try:
            rust = measure(rust_port, "/autocompletable/users?query=User%203", "session_token=benchmark-session", args.iterations)
        finally:
            stop_server(rust_process)

        log = open(temp / "puma.log", "w+")
        process = subprocess.Popen(
            [str(ruby), str(ruby.parent / "bundle"), "exec", "puma", "-C", "config/puma.rb"],
            cwd=repository, env=camp_env, stdout=log, stderr=log,
        )
        try:
            wait_for_server(camp_port, process)
            cookie, _ = login_campfire(camp_port)
            camp = measure(camp_port, "/autocompletable/users.json?query=User%203", cookie, args.iterations)
        except Exception:
            log.flush()
            log.seek(0)
            print(log.read()[-2000:])
            raise
        finally:
            stop_server(process)
            log.close()

        if rust[3] != camp[3]:
            raise AssertionError((rust[3], camp[3]))
        print(f"users={args.users} iterations={args.iterations} warmup=2 clients=1 results={len(rust[3])}")
        print(f"rustfire_median_ms={rust[0]:.3f} rustfire_p95_ms={rust[1]:.3f} body_bytes={sorted(rust[2])}")
        print(f"campfire_median_ms={camp[0]:.3f} campfire_p95_ms={camp[1]:.3f} body_bytes={sorted(camp[2])}")


if __name__ == "__main__":
    main()
