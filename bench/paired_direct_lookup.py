"""Compare existing-ping reuse on the pinned Campfire and Rustfire builds.

Example:
  python bench/paired_direct_lookup.py --rooms 1000 --iterations 10

Both servers use disposable SQLite fixtures with the same users, room memberships,
selected participants, and one keep-alive HTTP client. Campfire's direct-room broadcast
still does more rendering work, so this is a core-path comparison until parity.
"""

import argparse
import http.client
import http.cookiejar
import itertools
import os
import pathlib
import re
import secrets
import sqlite3
import statistics
import subprocess
import tempfile
import time
import urllib.parse
import urllib.request

from direct_lookup import ROOT, free_port, p95, start_server, stop_server


def wait_for_server(port, process):
    for _ in range(300):
        if process.poll() is not None:
            raise RuntimeError("Campfire exited during startup")
        try:
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=1)
            connection.request("GET", "/up")
            response = connection.getresponse()
            response.read()
            connection.close()
            if response.status == 200:
                return
        except OSError:
            time.sleep(0.1)
    raise RuntimeError("Campfire did not start")


def seed_rustfire(database, port, sets):
    process = start_server(database, port)
    stop_server(process)
    stamp = "2026-01-01T00:00:00Z"
    with sqlite3.connect(database) as db:
        db.execute("INSERT INTO accounts(id,name,join_code,created_at,updated_at) VALUES(1,'Benchmark','benchmark',?1,?1)", [stamp])
        db.executemany(
            "INSERT INTO users(id,name,role,status,created_at,updated_at) VALUES(?1,?2,?3,0,?4,?4)",
            [(uid, f"User {uid}", 1 if uid == 1 else 0, stamp) for uid in range(1, 52)],
        )
        db.execute("INSERT INTO sessions(user_id,token,csrf_token,created_at,last_active_at) VALUES(1,'benchmark-session','benchmark-csrf',?1,?1)", [stamp])
        db.execute("INSERT INTO rooms(id,name,type,creator_id,created_at,updated_at) VALUES(1,'Campfire','Rooms::Open',1,?1,?1)", [stamp])
        db.executemany("INSERT INTO memberships(room_id,user_id,involvement,created_at) VALUES(1,?1,'mentions',?2)", [(1, stamp), (2, stamp)])
        db.executemany(
            "INSERT INTO rooms(id,name,type,creator_id,created_at,updated_at) VALUES(?1,NULL,'Rooms::Direct',1,?2,?2)",
            [(number, stamp) for number in range(2, len(sets) + 2)],
        )
        db.executemany(
            "INSERT INTO memberships(room_id,user_id,involvement,created_at) VALUES(?1,?2,'everything',?3)",
            ((number, uid, stamp) for number, others in enumerate(sets, 2) for uid in (1, *others)),
        )


def campfire_env(repository, ruby, bundle_path, database, port, temp):
    env = dict(os.environ)
    env.update({
        "PATH": str(ruby.parent) + os.pathsep + env.get("PATH", ""),
        "BUNDLE_PATH": str(bundle_path),
        "BUNDLE_WITHOUT": "development:test",
        "RAILS_ENV": "production",
        "DISABLE_SSL": "1",
        "SECRET_KEY_BASE": secrets.token_hex(64),
        "DATABASE_URL": "sqlite3:" + str(database),
        "PORT": str(port),
        "WEB_CONCURRENCY": "1",
        "PIDFILE": str(temp / "puma.pid"),
        "REDIS_URL": "redis://127.0.0.1:6379",
    })
    return env


def seed_campfire(repository, ruby, bundle_path, source_database, database, sets, port, temp):
    with sqlite3.connect(source_database) as source, sqlite3.connect(database) as target:
        source.backup(target)
    env = campfire_env(repository, ruby, bundle_path, database, port, temp)
    bundle = ruby.parent / "bundle"
    digest = subprocess.check_output(
        [str(ruby), str(bundle), "exec", "ruby", "-rbcrypt", "-e", 'print BCrypt::Password.create("benchmark-password")'],
        cwd=repository, env=env, text=True, stderr=subprocess.DEVNULL,
    )
    stamp = "2026-01-01T00:00:00Z"
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT count(*) FROM rooms").fetchone() == (1,)
        assert db.execute("SELECT count(*) FROM users").fetchone() == (2,)
        db.execute("DELETE FROM messages")
        db.execute("DELETE FROM action_text_rich_texts WHERE record_type='Message'")
        db.execute("DELETE FROM message_search_index")
        db.execute("UPDATE users SET email_address='benchmark@example.invalid',password_digest=?1 WHERE id=1", [digest])
        db.executemany(
            "INSERT INTO users(id,name,role,status,created_at,updated_at) VALUES(?1,?2,0,0,?3,?3)",
            [(uid, f"User {uid}", stamp) for uid in range(3, 52)],
        )
        db.executemany(
            "INSERT INTO rooms(id,name,type,creator_id,created_at,updated_at) VALUES(?1,NULL,'Rooms::Direct',1,?2,?2)",
            [(number, stamp) for number in range(2, len(sets) + 2)],
        )
        db.executemany(
            "INSERT INTO memberships(room_id,user_id,involvement,created_at,updated_at) VALUES(?1,?2,'everything',?3,?3)",
            ((number, uid, stamp) for number, others in enumerate(sets, 2) for uid in (1, *others)),
        )
    with sqlite3.connect(database) as db:
        db.execute("VACUUM")
    return env


def login_campfire(port, email="benchmark@example.invalid", password="benchmark-password"):
    cookies = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cookies))
    base = f"http://127.0.0.1:{port}"
    with opener.open(base + "/session/new") as response:
        page = response.read().decode()
    csrf = re.search(r'<meta name="csrf-token" content="([^"]+)', page)
    assert csrf, "Campfire sign-in CSRF token missing"
    body = urllib.parse.urlencode({
        "email_address": email,
        "password": password,
        "authenticity_token": csrf.group(1),
    }).encode()
    with opener.open(base + "/session", data=body) as response:
        assert response.status == 200, response.url
        response.read()
    with opener.open(base + "/rooms/1") as response:
        assert response.status == 200
        room_page = response.read().decode()
    csrf = re.search(r'<meta name="csrf-token" content="([^"]+)', room_page)
    assert csrf, "Campfire room CSRF token missing"
    cookie = "; ".join(f"{item.name}={item.value}" for item in cookies)
    return cookie, csrf.group(1)


def time_reuse(port, target, cookie, csrf, iterations):
    body = urllib.parse.urlencode([("user_ids[]", uid) for uid in target[1:]] + [("authenticity_token", csrf)])
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=120)
    samples = []
    statuses = set()
    lengths = set()
    try:
        for iteration in range(iterations + 2):
            started = time.perf_counter()
            connection.request("POST", "/rooms/directs", body, {
                "Cookie": cookie,
                "X-CSRF-Token": csrf,
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "text/html",
            })
            response = connection.getresponse()
            payload = response.read()
            elapsed = (time.perf_counter() - started) * 1000
            expected_locations = {f"http://127.0.0.1:{port}/rooms/{target[0]}", f"/rooms/{target[0]}"}
            if response.status != 302 or response.getheader("Location") not in expected_locations:
                raise AssertionError((response.status, response.getheader("Location"), payload[:300]))
            if iteration >= 2:
                samples.append(elapsed)
                statuses.add(response.status)
                lengths.add(len(payload))
    finally:
        connection.close()
    return statistics.median(samples), p95(samples), statuses, lengths


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--rooms", type=int, default=1000)
    parser.add_argument("--iterations", type=int, default=10)
    parser.add_argument("--campfire-repo", type=pathlib.Path, default=pathlib.Path("/tmp/once-campfire-reference"))
    parser.add_argument("--ruby", type=pathlib.Path, default=pathlib.Path("/tmp/rustfire-baseline/local/bin/ruby"))
    parser.add_argument("--bundle-path", type=pathlib.Path, default=pathlib.Path("/tmp/rustfire-baseline/bundle"))
    args = parser.parse_args()
    if not 1 <= args.rooms <= 19600 or args.iterations < 1:
        parser.error("rooms must be 1–19600 and iterations must be positive")
    repository = args.campfire_repo.resolve()
    ruby = args.ruby.resolve()
    bundle_path = args.bundle_path.resolve()
    source_database = repository / "storage/db/production.sqlite3"
    if not source_database.is_file():
        parser.error("Campfire production fixture database is missing")
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repository, text=True).strip()
    if revision != "91d294f4a09f9bbe37f9548959bfcb43645678fb":
        parser.error(f"Campfire source is at {revision}, not the pinned compatibility target")
    sets = list(itertools.islice(itertools.combinations(range(2, 52), 3), args.rooms))
    target = (len(sets) + 1, 1, *sets[-1])
    with tempfile.TemporaryDirectory(prefix="paired-direct-bench-") as temporary:
        temp = pathlib.Path(temporary)
        rust_db = temp / "rustfire.sqlite3"
        camp_db = temp / "campfire.sqlite3"
        rust_port, camp_port = free_port(), free_port()
        seed_rustfire(rust_db, rust_port, sets)
        camp_env = seed_campfire(repository, ruby, bundle_path, source_database, camp_db, sets, camp_port, temp)

        rust_process = start_server(rust_db, rust_port)
        try:
            with sqlite3.connect(rust_db) as db:
                assert db.execute("SELECT count(*) FROM direct_room_sets").fetchone() == (args.rooms,)
            rust = time_reuse(rust_port, target, "session_token=benchmark-session", "benchmark-csrf", args.iterations)
        finally:
            stop_server(rust_process)

        log = open(temp / "puma.log", "w+")
        bundle = ruby.parent / "bundle"
        camp_process = subprocess.Popen(
            [str(ruby), str(bundle), "exec", "puma", "-C", "config/puma.rb"],
            cwd=repository, env=camp_env, stdout=log, stderr=log,
        )
        try:
            wait_for_server(camp_port, camp_process)
            cookie, csrf = login_campfire(camp_port)
            camp = time_reuse(camp_port, target, cookie, csrf, args.iterations)
        except Exception:
            log.flush()
            log.seek(0)
            print(log.read()[-2000:])
            raise
        finally:
            stop_server(camp_process)
            log.close()
        print(f"rooms={args.rooms} iterations={args.iterations} warmup=2 clients=1")
        print(f"rustfire_median_ms={rust[0]:.3f} rustfire_p95_ms={rust[1]:.3f} statuses={sorted(rust[2])} body_bytes={sorted(rust[3])}")
        print(f"campfire_median_ms={camp[0]:.3f} campfire_p95_ms={camp[1]:.3f} statuses={sorted(camp[2])} body_bytes={sorted(camp[3])}")


if __name__ == "__main__":
    main()
