"""Compare Campfire and Rustfire user autocomplete on matched disposable fixtures.

Example:
  cargo build --release
  python bench/paired_autocomplete.py --users 10000 --iterations 30
"""

import argparse
import concurrent.futures
import hashlib
import http.client
import json
import pathlib
import sqlite3
import statistics
import subprocess
import tempfile
import threading
import time
import urllib.parse
import xml.etree.ElementTree as ET

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
    selected_rows = None
    expected_body = None
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
                selected_rows = users
                expected_body = body
            elif identities != selected:
                raise AssertionError("Autocomplete results changed during trial")
            elif body != expected_body:
                raise AssertionError("Autocomplete response bytes changed during trial")
            if iteration >= 2:
                samples.append(elapsed)
                lengths.add(len(body))
    finally:
        connection.close()
    return statistics.median(samples), p95(samples), lengths, selected, selected_rows, expected_body


def process_tree_pss(pid):
    import psutil

    try:
        parent = psutil.Process(pid)
        processes = [parent, *parent.children(recursive=True)]
    except psutil.Error:
        return None
    total = 0
    for process in processes:
        try:
            memory = process.memory_full_info()
            total += getattr(memory, "pss", memory.rss)
        except psutil.Error:
            continue
    return total


def measure_concurrent(port, path, cookie, clients, duration, expected, process_pid=None):
    ready = threading.Barrier(clients + 1, timeout=30)
    start = threading.Event()
    clock = {}
    sampler_stop = threading.Event()
    peak_pss = [None]

    def sample_memory():
        while True:
            observed = process_tree_pss(process_pid)
            if observed is not None:
                peak_pss[0] = max(peak_pss[0] or 0, observed)
            if sampler_stop.wait(1):
                break

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
        sampler = None
        if process_pid is not None:
            sampler = threading.Thread(target=sample_memory, daemon=True)
            sampler.start()
        start.set()
        try:
            results = [future.result() for future in futures]
        finally:
            if sampler is not None:
                sampler_stop.set()
                sampler.join()
                observed = process_tree_pss(process_pid)
                if observed is not None:
                    peak_pss[0] = max(peak_pss[0] or 0, observed)
    samples = [sample for worker_samples, _, _ in results for sample in worker_samples]
    errors = sum(worker_errors for _, worker_errors, _ in results)
    elapsed = max(ended for _, _, ended in results) - clock["started"]
    if not samples:
        raise AssertionError("Concurrent trial had no successful requests")
    return len(samples) / elapsed, statistics.median(samples), p95(samples), errors, len(samples), peak_pss[0]


def measure_go_concurrent(binary, port, path, cookie, clients, duration, expected_body, server_pid=None):
    command = (str(binary), "--base", f"http://127.0.0.1:{port}", "--path", path,
        "--cookie", cookie, "--expected-sha256", hashlib.sha256(expected_body).hexdigest(),
        "--clients", str(clients), "--seconds", str(duration))
    peak_pss = [None]
    sampler_stop = threading.Event()

    def sample_memory():
        while True:
            observed = process_tree_pss(server_pid)
            if observed is not None:
                peak_pss[0] = max(peak_pss[0] or 0, observed)
            if sampler_stop.wait(1):
                break

    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    sampler = None
    if server_pid is not None:
        sampler = threading.Thread(target=sample_memory, daemon=True)
        sampler.start()
    try:
        stdout, stderr = process.communicate(timeout=max(60, duration + 45))
    except subprocess.TimeoutExpired:
        process.kill()
        process.communicate()
        raise RuntimeError(f"Go client timed out at {clients} clients")
    finally:
        if sampler is not None:
            sampler_stop.set()
            sampler.join()
            observed = process_tree_pss(server_pid)
            if observed is not None:
                peak_pss[0] = max(peak_pss[0] or 0, observed)
    try:
        report = json.loads(stdout)
    except json.JSONDecodeError as error:
        raise RuntimeError(f"Go client failed at {clients} clients: {stderr}") from error
    if process.returncode not in (0, 1) or (process.returncode == 1 and report["errors"] == 0):
        raise RuntimeError(f"Go client failed at {clients} clients: {stderr}")
    return report["rps"], report["median_ms"], report["p95_ms"], report["errors"], report["successes"], peak_pss[0]


def probe_initials_avatar(port, cookie, url):
    parsed = urllib.parse.urlsplit(url)
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        connection.request("GET", parsed.path + "?" + parsed.query, headers={"Cookie": cookie})
        response = connection.getresponse()
        body = response.read()
        if response.status != 200 or response.getheader("Content-Type", "").split(";", 1)[0] != "image/svg+xml":
            raise AssertionError(("avatar response", response.status, response.getheader("Content-Type")))
        document = ET.fromstring(body)
        namespace = "{http://www.w3.org/2000/svg}"
        text = document.find(f".//{namespace}text")
        rectangle = document.find(f".//{namespace}rect")
        if text is None or rectangle is None:
            raise AssertionError("Initials avatar lacks its text or background")
        return (text.text or "").strip(), rectangle.get("fill")
    finally:
        connection.close()


def check_cached_avatar_version(database, port, selected_user, expected_body):
    user_id = selected_user["value"]
    with sqlite3.connect(database) as db:
        original_stamp = db.execute("SELECT updated_at FROM users WHERE id=?", (user_id,)).fetchone()[0]
        changed_stamp = "2027-01-02T03:04:05Z"
        if original_stamp == changed_stamp:
            raise AssertionError("Cache invalidation fixture did not change the timestamp")
        db.execute("UPDATE users SET updated_at=? WHERE id=?", (changed_stamp, user_id))
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        def fetch():
            connection.request("GET", "/autocompletable/users?query=User%203", headers={"Cookie": "session_token=benchmark-session", "Accept": "application/json"})
            response = connection.getresponse()
            body = response.read()
            if response.status != 200:
                raise AssertionError(("cache invalidation", response.status, body[:300]))
            return body

        changed = json.loads(fetch())
        if changed[0]["avatar_url"] == selected_user["avatar_url"] or changed[0]["sgid"] != selected_user["sgid"]:
            raise AssertionError("Avatar version did not refresh after the user timestamp changed")
        with sqlite3.connect(database) as db:
            db.execute("UPDATE users SET updated_at=? WHERE id=?", (original_stamp, user_id))
        if fetch() != expected_body:
            raise AssertionError("Autocomplete body did not restore after the timestamp was restored")
    finally:
        connection.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--users", type=int, default=10000)
    parser.add_argument("--iterations", type=int, default=30)
    parser.add_argument("--clients", type=int, nargs="*", default=[], help="optional concurrent client counts")
    parser.add_argument("--seconds", type=float, default=3.0, help="duration of each concurrent trial")
    parser.add_argument("--campfire-workers", type=int, default=1, help="Puma worker count; use 22 for the packaged configuration on the 32-CPU test host")
    parser.add_argument("--rustfire-autocomplete-slots", type=int, default=4, help="Rustfire autocomplete request concurrency limit")
    parser.add_argument("--memory", action="store_true", help="sample peak process-tree PSS during concurrent trials (requires psutil)")
    parser.add_argument("--engine", choices=("python", "go"), default="python", help="concurrent load generator; Go reduces Python client overhead")
    parser.add_argument("--slo-ms", type=float, help="report largest sampled client count with zero errors and p95 below this latency")
    parser.add_argument("--campfire-repo", type=pathlib.Path, default=pathlib.Path("/tmp/once-campfire-reference"))
    parser.add_argument("--ruby", type=pathlib.Path, default=pathlib.Path("/tmp/rustfire-baseline/local/bin/ruby"))
    parser.add_argument("--bundle-path", type=pathlib.Path, default=pathlib.Path("/tmp/rustfire-baseline/bundle"))
    args = parser.parse_args()
    if args.users < 300 or args.iterations < 1 or any(client < 1 for client in args.clients) or args.seconds <= 0 or args.campfire_workers < 1 or not 1 <= args.rustfire_autocomplete_slots <= 128 or (args.slo_ms is not None and args.slo_ms <= 0):
        parser.error("users must be at least 300, iterations and client counts positive, and seconds greater than zero")
    if args.memory:
        try:
            import psutil  # noqa: F401
        except ImportError:
            parser.error("--memory requires the psutil Python package")
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
        go_binary = temp / "checked-get"
        if args.engine == "go":
            subprocess.run(("go", "build", "-o", str(go_binary), str(pathlib.Path(__file__).with_name("checked_get.go"))), check=True)
        rust_db, camp_db = temp / "rustfire.sqlite3", temp / "campfire.sqlite3"
        rust_port, camp_port = free_port(), free_port()
        seed_rustfire(rust_db, rust_port, [])
        camp_env = seed_campfire(repository, ruby, bundle_path, source_database, camp_db, [], camp_port, temp)
        camp_env["WEB_CONCURRENCY"] = str(args.campfire_workers)
        if args.users > 51:
            add_users(rust_db, 52, args.users)
            add_users(camp_db, 52, args.users)

        rust_process = start_server(rust_db, rust_port, {"RUSTFIRE_CAMPFIRE_SECRET_KEY_BASE": camp_env["SECRET_KEY_BASE"], "RUSTFIRE_AUTOCOMPLETE_SLOTS": str(args.rustfire_autocomplete_slots)})
        try:
            rust = measure(rust_port, "/autocompletable/users?query=User%203", "session_token=benchmark-session", args.iterations)
            rust_concurrent = {clients: (
                measure_go_concurrent(go_binary, rust_port, "/autocompletable/users?query=User%203", "session_token=benchmark-session", clients, args.seconds, rust[5], rust_process.pid if args.memory else None)
                if args.engine == "go" else
                measure_concurrent(rust_port, "/autocompletable/users?query=User%203", "session_token=benchmark-session", clients, args.seconds, rust[3], rust_process.pid if args.memory else None)
            ) for clients in args.clients}
            rust_initials = probe_initials_avatar(rust_port, "session_token=benchmark-session", rust[4][0]["avatar_url"])
            check_cached_avatar_version(rust_db, rust_port, rust[4][0], rust[5])
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
            camp_concurrent = {clients: (
                measure_go_concurrent(go_binary, camp_port, "/autocompletable/users.json?query=User%203", cookie, clients, args.seconds, camp[5], process.pid if args.memory else None)
                if args.engine == "go" else
                measure_concurrent(camp_port, "/autocompletable/users.json?query=User%203", cookie, clients, args.seconds, camp[3], process.pid if args.memory else None)
            ) for clients in args.clients}
            camp_initials = probe_initials_avatar(camp_port, cookie, camp[4][0]["avatar_url"])
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
        for rust_user, camp_user in zip(rust[4], camp[4]):
            if rust_user["sgid"] != camp_user["sgid"]:
                raise AssertionError("Signed mention IDs differ despite the shared signing secret")
            rust_avatar_url = urllib.parse.urlsplit(rust_user["avatar_url"])
            camp_avatar_url = urllib.parse.urlsplit(camp_user["avatar_url"])
            if (rust_avatar_url.path, rust_avatar_url.query) != (camp_avatar_url.path, camp_avatar_url.query):
                raise AssertionError(("Avatar URLs differ beyond origin", rust_avatar_url, camp_avatar_url))
        if rust_initials != camp_initials:
            raise AssertionError(("Initials avatar differs", rust_initials, camp_initials))
        print(f"users={args.users} iterations={args.iterations} warmup=2 clients=1 results={len(rust[3])} campfire_workers={args.campfire_workers} rustfire_autocomplete_slots={args.rustfire_autocomplete_slots} engine={args.engine}")
        print(f"rustfire_median_ms={rust[0]:.3f} rustfire_p95_ms={rust[1]:.3f} body_bytes={sorted(rust[2])}")
        print(f"campfire_median_ms={camp[0]:.3f} campfire_p95_ms={camp[1]:.3f} body_bytes={sorted(camp[2])}")
        for clients in args.clients:
            rust_rate, rust_median, rust_p95, rust_errors, rust_count, rust_pss = rust_concurrent[clients]
            camp_rate, camp_median, camp_p95, camp_errors, camp_count, camp_pss = camp_concurrent[clients]
            rust_memory = f" rustfire_peak_pss_mib={rust_pss / 1048576:.1f}" if rust_pss is not None else ""
            camp_memory = f" campfire_peak_pss_mib={camp_pss / 1048576:.1f}" if camp_pss is not None else ""
            print(f"clients={clients} seconds={args.seconds:g} rustfire_rps={rust_rate:.1f} rustfire_median_ms={rust_median:.3f} rustfire_p95_ms={rust_p95:.3f} rustfire_successes={rust_count} rustfire_errors={rust_errors}{rust_memory}")
            print(f"clients={clients} seconds={args.seconds:g} campfire_rps={camp_rate:.1f} campfire_median_ms={camp_median:.3f} campfire_p95_ms={camp_p95:.3f} campfire_successes={camp_count} campfire_errors={camp_errors}{camp_memory}")
        if args.slo_ms is not None:
            for name, values in (("rustfire", rust_concurrent), ("campfire", camp_concurrent)):
                passing = [clients for clients, result in values.items() if result[3] == 0 and result[2] <= args.slo_ms]
                print(f"{name}_largest_sampled_passing_clients={max(passing) if passing else 0} slo_p95_ms={args.slo_ms:g}")


if __name__ == "__main__":
    main()
