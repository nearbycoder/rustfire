"""Measure indexed direct-room reuse on a disposable large fixture.

Run after cargo build --release: python bench/direct_lookup.py --rooms 10000
This compares Rustfire's current indexed SQL with its previous grouped SQL, not Campfire.
"""

import argparse
import http.client
import itertools
import json
import math
import os
import pathlib
import socket
import sqlite3
import statistics
import subprocess
import tempfile
import time
import urllib.parse


ROOT = pathlib.Path(__file__).resolve().parents[1]


def free_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def start_server(database, port):
    env = dict(
        os.environ,
        RUSTFIRE_DB=str(database),
        RUSTFIRE_ADDR=f"127.0.0.1:{port}",
        RUSTFIRE_DISABLE_PUSH="1",
        RUSTFIRE_DISABLE_WEBHOOKS="1",
    )
    process = subprocess.Popen(
        [str(ROOT / "target/release/rustfire")], cwd=ROOT, env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    for _ in range(100):
        try:
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=1)
            connection.request("GET", "/up")
            response = connection.getresponse()
            response.read()
            connection.close()
            if response.status == 200:
                return process
        except OSError:
            time.sleep(0.05)
    process.terminate()
    process.wait(timeout=10)
    raise RuntimeError("Rustfire did not start")


def stop_server(process):
    process.terminate()
    process.wait(timeout=10)


def p95(samples):
    return sorted(samples)[math.ceil(0.95 * len(samples)) - 1]


def measure_sql(connection, statement, arguments, iterations):
    samples = []
    for _ in range(iterations):
        started = time.perf_counter()
        result = connection.execute(statement, arguments).fetchone()
        samples.append((time.perf_counter() - started) * 1000)
        if result is None:
            raise AssertionError("lookup missed the seeded room")
    return statistics.median(samples), p95(samples)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--rooms", type=int, default=10000)
    parser.add_argument("--iterations", type=int, default=100)
    args = parser.parse_args()
    if not 1 <= args.rooms <= 19600 or args.iterations < 1:
        parser.error("rooms must be 1–19600 and iterations must be positive")

    with tempfile.TemporaryDirectory(prefix="rustfire-direct-bench-") as temp:
        database = pathlib.Path(temp) / "direct.db"
        port = free_port()
        server = start_server(database, port)
        stop_server(server)

        with sqlite3.connect(database) as db:
            stamp = "2026-01-01T00:00:00Z"
            db.executemany(
                "INSERT INTO users(id,name,role,status,created_at,updated_at) VALUES(?1,?2,?3,0,?4,?4)",
                [(uid, f"User {uid}", 1 if uid == 1 else 0, stamp) for uid in range(1, 52)],
            )
            db.execute(
                "INSERT INTO sessions(user_id,token,csrf_token,created_at,last_active_at) VALUES(1,'benchmark-session','benchmark-csrf',?1,?1)",
                [stamp],
            )
            sets = list(itertools.islice(itertools.combinations(range(2, 52), 3), args.rooms))
            rooms = [(number, "Ping", "Rooms::Direct", 1, stamp, stamp) for number in range(1, args.rooms + 1)]
            db.executemany(
                "INSERT INTO rooms(id,name,type,creator_id,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                rooms,
            )
            memberships = (
                (number, uid, stamp)
                for number, others in enumerate(sets, 1)
                for uid in (1, *others)
            )
            db.executemany(
                "INSERT INTO memberships(room_id,user_id,involvement,created_at) VALUES(?1,?2,'everything',?3)",
                memberships,
            )

        server = start_server(database, port)
        try:
            target = (1, *sets[-1])
            key = ",".join(map(str, target))
            with sqlite3.connect(database) as db:
                count = db.execute("SELECT count(*) FROM direct_room_sets").fetchone()[0]
                assert count == args.rooms, (count, args.rooms)
                indexed = "SELECT room_id FROM direct_room_sets WHERE member_ids=?1 ORDER BY room_id LIMIT 1"
                grouped = "SELECT r.id FROM rooms r JOIN memberships m ON m.room_id=r.id WHERE r.type='Rooms::Direct' GROUP BY r.id HAVING count(*)=?1 AND sum(CASE WHEN m.user_id IN (SELECT value FROM json_each(?2)) THEN 1 ELSE 0 END)=?1 LIMIT 1"
                assert db.execute(indexed, [key]).fetchone() == (args.rooms,)
                assert db.execute(grouped, [len(target), json.dumps(target)]).fetchone() == (args.rooms,)
                plan = " ".join(row[3] for row in db.execute("EXPLAIN QUERY PLAN " + indexed, [key]))
                assert "idx_direct_room_sets_members" in plan, plan
                index_median, index_p95 = measure_sql(db, indexed, [key], args.iterations)
                group_median, group_p95 = measure_sql(db, grouped, [len(target), json.dumps(target)], args.iterations)

            body = urllib.parse.urlencode([("user_ids", uid) for uid in target[1:]])
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            http_samples = []
            try:
                for _ in range(args.iterations):
                    started = time.perf_counter()
                    connection.request(
                        "POST", "/rooms/directs", body,
                        {"Cookie": "session_token=benchmark-session", "X-CSRF-Token": "benchmark-csrf", "Content-Type": "application/x-www-form-urlencoded"},
                    )
                    response = connection.getresponse()
                    response.read()
                    http_samples.append((time.perf_counter() - started) * 1000)
                    assert response.status == 303 and response.getheader("Location") == f"/rooms/{args.rooms}", response.status
            finally:
                connection.close()
            print(
                f"rooms={args.rooms} iterations={args.iterations} "
                f"indexed_sql_median_ms={index_median:.3f} indexed_sql_p95_ms={index_p95:.3f} "
                f"prior_grouped_sql_median_ms={group_median:.3f} prior_grouped_sql_p95_ms={group_p95:.3f} "
                f"current_http_median_ms={statistics.median(http_samples):.3f} current_http_p95_ms={p95(http_samples):.3f}"
            )
        finally:
            stop_server(server)


if __name__ == "__main__":
    main()
