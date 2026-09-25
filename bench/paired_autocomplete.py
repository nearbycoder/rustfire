"""Compare Campfire and Rustfire user autocomplete on matched disposable fixtures.

Example:
  cargo build --release
  python bench/paired_autocomplete.py --users 10000 --iterations 30
"""

import argparse
import concurrent.futures
import http.client
import json
import pathlib
import sqlite3
import statistics
import subprocess
import tempfile
import threading
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


def measure_concurrent(port, path, cookie, clients, duration, expected):
    ready = threading.Barrier(clients + 1, timeout=30)
    start = threading.Event()
    clock = {}

    def worker():
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
        samples = []
        errors = 0
        expected_body = None
        try:
            for _ in range(2):
                connection.request("GET", path, headers={"Cookie": cookie, "Accept": "application/json"})
                response = connection.getresponse()
                body = response.read()
                if response.status != 200 or [(user["value"], user["name"]) for user in json.loads(body)] != expected:
                    raise AssertionError(("warmup", response.status, body[:300]))
                if expected_body is not None and body != expected_body:
                    raise AssertionError("Concurrent warmup response changed")
                expected_body = body
            ready.wait()
            start.wait()
            while time.perf_counter() < clock["deadline"]:
                started = time.perf_counter()
                try:
                    connection.request("GET", path, headers={"Cookie": cookie, "Accept": "application/json"})
                    response = connection.getresponse()
                    body = response.read()
                    elapsed = (time.perf_counter() - started) * 1000
                    if response.status != 200 or body != expected_body:
                        errors += 1
                    else:
                        samples.append(elapsed)
                except (OSError, ValueError, KeyError):
                    errors += 1
                    connection.close()
                    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
        finally:
            connection.close()
        return samples, errors, time.perf_counter()

    with concurrent.futures.ThreadPoolExecutor(max_workers=clients) as executor:
        futures = [executor.submit(worker) for _ in range(clients)]
        ready.wait()
        clock["started"] = time.perf_counter()
        clock["deadline"] = clock["started"] + duration
        start.set()
        results = [future.result() for future in futures]
    samples = [sample for worker_samples, _, _ in results for sample in worker_samples]
    errors = sum(worker_errors for _, worker_errors, _ in results)
    elapsed = max(ended for _, _, ended in results) - clock["started"]
    if not samples:
        raise AssertionError("Concurrent trial had no successful requests")
    return len(samples) / elapsed, statistics.median(samples), p95(samples), errors, len(samples)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--users", type=int, default=10000)
    parser.add_argument("--iterations", type=int, default=30)
    parser.add_argument("--clients", type=int, nargs="*", default=[], help="optional concurrent client counts")
    parser.add_argument("--seconds", type=float, default=3.0, help="duration of each concurrent trial")
    parser.add_argument("--campfire-repo", type=pathlib.Path, default=pathlib.Path("/tmp/once-campfire-reference"))
    parser.add_argument("--ruby", type=pathlib.Path, default=pathlib.Path("/tmp/rustfire-baseline/local/bin/ruby"))
    parser.add_argument("--bundle-path", type=pathlib.Path, default=pathlib.Path("/tmp/rustfire-baseline/bundle"))
    args = parser.parse_args()
    if args.users < 300 or args.iterations < 1 or any(client < 1 for client in args.clients) or args.seconds <= 0:
        parser.error("users must be at least 300, iterations and client counts positive, and seconds greater than zero")
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
            rust_concurrent = {
                clients: measure_concurrent(rust_port, "/autocompletable/users?query=User%203", "session_token=benchmark-session", clients, args.seconds, rust[3])
                for clients in args.clients
            }
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
            camp_concurrent = {
                clients: measure_concurrent(camp_port, "/autocompletable/users.json?query=User%203", cookie, clients, args.seconds, camp[3])
                for clients in args.clients
            }
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
        for clients in args.clients:
            rust_rate, rust_median, rust_p95, rust_errors, rust_count = rust_concurrent[clients]
            camp_rate, camp_median, camp_p95, camp_errors, camp_count = camp_concurrent[clients]
            print(f"clients={clients} seconds={args.seconds:g} rustfire_rps={rust_rate:.1f} rustfire_median_ms={rust_median:.3f} rustfire_p95_ms={rust_p95:.3f} rustfire_successes={rust_count} rustfire_errors={rust_errors}")
            print(f"clients={clients} seconds={args.seconds:g} campfire_rps={camp_rate:.1f} campfire_median_ms={camp_median:.3f} campfire_p95_ms={camp_p95:.3f} campfire_successes={camp_count} campfire_errors={camp_errors}")


if __name__ == "__main__":
    main()
