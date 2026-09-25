"""Compare matched open-room creation or update and signed sidebar Turbo fanout.

Requires the pinned Campfire checkout and bundled Ruby. Uses disposable SQLite
databases, an isolated Campfire checkout, and an isolated Redis server.
Run `cargo build --release` first.
"""

import argparse
import json
import pathlib
import re
import shutil
import sqlite3
import subprocess
import tempfile
import uuid

from direct_lookup import ROOT, free_port, start_server, stop_server
from paired_banned_content import REPOSITORY, RUBY, BUNDLE, REVISION, isolated_campfire, start_redis
from paired_direct_lookup import login_campfire, seed_campfire, seed_rustfire, wait_for_server


def capture(port, cookie, csrf, sockets, rooms, run_id, sample_file, operation):
    command = [
        "node", "bench/sidebar_fanout.mjs", "--base", f"http://127.0.0.1:{port}",
        "--cookie", cookie, "--csrf", csrf, "--sockets", str(sockets),
        "--rooms", str(rooms), "--run-id", run_id, "--sample-file", str(sample_file), "--operation", operation,
    ]
    result = subprocess.run(command, cwd=ROOT, text=True, capture_output=True, timeout=180)
    if result.returncode:
        raise RuntimeError(f"Sidebar fanout failed: {result.stdout}\n{result.stderr}")
    metrics = json.loads(result.stdout.strip().splitlines()[-1])
    if any(metrics[key] for key in ("missed", "unexpected", "closed_early")):
        raise AssertionError(metrics)
    return metrics


def created_state(database, run_id, expected_rooms):
    with sqlite3.connect(database) as db:
        rows = db.execute(
            "SELECT r.id,COUNT(m.id) FROM rooms r LEFT JOIN memberships m ON m.room_id=r.id WHERE r.name LIKE ? GROUP BY r.id ORDER BY r.name",
            (f"sidebar-fanout-{run_id}-%",),
        ).fetchall()
        users = db.execute("SELECT COUNT(*) FROM users WHERE status=0").fetchone()[0]
    assert len(rows) == expected_rooms, (len(rows), expected_rooms)
    assert all(count == users for _, count in rows), (rows, users)
    return {"rooms": len(rows), "memberships_per_room": users}


def updated_state(database, run_id, rooms):
    with sqlite3.connect(database) as db:
        rows = db.execute("SELECT r.id,r.name,COUNT(m.id) FROM rooms r LEFT JOIN memberships m ON m.room_id=r.id WHERE r.name LIKE ? GROUP BY r.id", (f"sidebar-fanout-{run_id}-%",)).fetchall()
        users = db.execute("SELECT COUNT(*) FROM users WHERE status=0").fetchone()[0]
    assert len(rows) == 1 and rows[0][1] == f"sidebar-fanout-{run_id}-{rooms - 1}" and rows[0][2] == users, (rows, users)
    return {"rooms": 1, "memberships_per_room": users, "last_update": rooms - 1}


def sample_structure(path, operation):
    html = path.read_text()
    action = "prepend" if operation == "create" else "replace"
    target = "shared_rooms" if operation == "create" else r"list_rooms_open_\d+"
    assert re.search(rf'<turbo-stream\b[^>]*action="{action}"[^>]*target="{target}"', html), html[:300]
    assert re.search(r"\bid=['\"]list_rooms_open_\d+['\"]", html), html[:300]
    assert "sidebar-fanout-" in html
    return {"stream_action": action, "target": "shared_rooms" if operation == "create" else "list_rooms_open_ID", "room_id_prefix": "list_rooms_open_", "bytes": len(html.encode())}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sockets", type=int, default=200)
    parser.add_argument("--rooms", type=int, default=10)
    parser.add_argument("--campfire-workers", type=int, default=22)
    parser.add_argument("--campfire-first", action="store_true")
    parser.add_argument("--operation", choices=("create", "update"), default="create")
    parser.add_argument("--sample-dir", type=pathlib.Path, help="Copy one delivered Turbo event from each app")
    args = parser.parse_args()
    if min(args.sockets, args.rooms, args.campfire_workers) < 1:
        parser.error("sockets, rooms, and campfire workers must be positive")
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPOSITORY, text=True).strip()
    assert revision == REVISION, revision
    run_id = uuid.uuid4().hex[:12]
    with tempfile.TemporaryDirectory(prefix="paired-sidebar-fanout-") as temporary:
        temp = pathlib.Path(temporary)
        rust_db, camp_db = temp / "rustfire.sqlite3", temp / "campfire.sqlite3"
        rust_port, camp_port, redis_port = free_port(), free_port(), free_port()
        checkout = isolated_campfire(temp, redis_port)
        seed_rustfire(rust_db, rust_port, [])
        camp_env = seed_campfire(checkout, RUBY, BUNDLE, REPOSITORY / "storage/db/production.sqlite3", camp_db, [], camp_port, temp)
        camp_env["WEB_CONCURRENCY"] = str(args.campfire_workers)
        camp_env["REDIS_URL"] = f"redis://127.0.0.1:{redis_port}"

        def run_rustfire():
            process = start_server(rust_db, rust_port)
            try:
                metrics = capture(rust_port, "session_token=benchmark-session", "benchmark-csrf", args.sockets, args.rooms, run_id, temp / "rust-sidebar.html", args.operation)
                state = created_state(rust_db, run_id, args.rooms) if args.operation == "create" else updated_state(rust_db, run_id, args.rooms)
                return metrics, state
            finally:
                stop_server(process)

        def run_campfire():
            redis, redis_log = start_redis(temp, redis_port)
            log = open(temp / "puma.log", "w+")
            process = None
            try:
                process = subprocess.Popen([str(RUBY), str(RUBY.parent / "bundle"), "exec", "puma", "-C", "config/puma.rb"], cwd=checkout, env=camp_env, stdout=log, stderr=log)
                wait_for_server(camp_port, process)
                cookie, csrf = login_campfire(camp_port)
                metrics = capture(camp_port, cookie, csrf, args.sockets, args.rooms, run_id, temp / "camp-sidebar.html", args.operation)
                state = created_state(camp_db, run_id, args.rooms) if args.operation == "create" else updated_state(camp_db, run_id, args.rooms)
                return metrics, state
            finally:
                if process is not None:
                    stop_server(process)
                redis.terminate()
                try:
                    redis.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    redis.kill()
                    redis.wait(timeout=10)
                redis_log.close()
                log.close()

        if args.campfire_first:
            camp_result, rust_result = run_campfire(), run_rustfire()
        else:
            rust_result, camp_result = run_rustfire(), run_campfire()
        rust_sample = sample_structure(temp / "rust-sidebar.html", args.operation)
        camp_sample = sample_structure(temp / "camp-sidebar.html", args.operation)
        assert (temp / "rust-sidebar.html").read_bytes() == (temp / "camp-sidebar.html").read_bytes(), "Matched shared-room Turbo samples differ"
        if args.sample_dir:
            args.sample_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(temp / "rust-sidebar.html", args.sample_dir / "rustfire.html")
            shutil.copy2(temp / "camp-sidebar.html", args.sample_dir / "campfire.html")
        assert rust_result[1] == camp_result[1], (rust_result[1], camp_result[1])
        print(f"PASS paired shared-room {args.operation} and signed sidebar Turbo delivery")
        print(json.dumps({
            "campfire_workers": args.campfire_workers,
            "campfire_first": args.campfire_first,
            "rustfire": {"metrics": rust_result[0], "state": rust_result[1], "sample": rust_sample},
            "campfire": {"metrics": camp_result[0], "state": camp_result[1], "sample": camp_sample},
        }, sort_keys=True))


if __name__ == "__main__":
    main()
