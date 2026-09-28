"""Measure existing direct-room lookup with checked live sidebar fanout in both apps.

Requires the release Rustfire binary and pinned Campfire bundle. Each app gets a
disposable 1,000-room SQLite fixture; Campfire gets an isolated Redis server.
"""

import argparse
import itertools
import json
import pathlib
import sqlite3
import subprocess
import tempfile

from direct_lookup import ROOT, free_port, start_server, stop_server
from paired_banned_content import REPOSITORY, RUBY, BUNDLE, REVISION, isolated_campfire, start_redis
from paired_direct_lookup import login_campfire, seed_campfire, seed_rustfire, wait_for_server
from paired_direct_sidebar import SECRET


def checked_state(database, room_count, target_id, peers):
    with sqlite3.connect(database) as db:
        room_rows = db.execute("SELECT COUNT(*) FROM rooms WHERE type='Rooms::Direct'").fetchone()[0]
        memberships = [row[0] for row in db.execute("SELECT user_id FROM memberships WHERE room_id=? ORDER BY user_id", [target_id])]
        target = db.execute("SELECT type,name FROM rooms WHERE id=?", [target_id]).fetchone()
    assert room_rows == room_count and target == ("Rooms::Direct", None) and memberships == [1, *peers], (room_rows, target, memberships)
    return room_rows, target, memberships


def measure(port, cookie, csrf, database, room_count, target_id, peers, sockets, requests, sample):
    before = checked_state(database, room_count, target_id, peers)
    command = [
        "node", "bench/direct_reuse_fanout.mjs", "--base", f"http://127.0.0.1:{port}",
        "--cookie", cookie, "--csrf", csrf, "--room-id", str(target_id),
        "--peer-ids", ",".join(map(str, peers)), "--sockets", str(sockets),
        "--requests", str(requests), "--sample-file", str(sample),
    ]
    result = subprocess.run(command, cwd=ROOT, text=True, capture_output=True, timeout=240)
    if result.returncode:
        raise RuntimeError(f"Direct reuse fanout failed: {result.stdout}\n{result.stderr}")
    metrics = json.loads(result.stdout.strip().splitlines()[-1])
    assert metrics["expected"] == metrics["received"] == sockets * requests, metrics
    assert not any(metrics[key] for key in ("missed", "unexpected", "closed_early")), metrics
    assert checked_state(database, room_count, target_id, peers) == before
    assert sample.is_file(), "No sample sidebar frame was delivered"
    return metrics


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rooms", type=int, default=1000)
    parser.add_argument("--sockets", type=int, default=1000)
    parser.add_argument("--requests", type=int, default=10)
    parser.add_argument("--campfire-workers", type=int, default=22)
    parser.add_argument("--campfire-first", action="store_true")
    parser.add_argument("--report", type=pathlib.Path)
    args = parser.parse_args()
    if not 1 <= args.rooms <= 19600 or min(args.sockets, args.requests, args.campfire_workers) < 1:
        parser.error("rooms must be 1–19600; sockets, requests, and workers must be positive")
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPOSITORY, text=True).strip()
    assert revision == REVISION, revision
    sets = list(itertools.islice(itertools.combinations(range(2, 52), 3), args.rooms))
    target_id = args.rooms + 1
    peers = list(sets[-1])
    with tempfile.TemporaryDirectory(prefix="paired-direct-reuse-fanout-") as temporary:
        temp = pathlib.Path(temporary)
        rust_db, camp_db = temp / "rustfire.sqlite3", temp / "campfire.sqlite3"
        rust_port, camp_port, redis_port = free_port(), free_port(), free_port()
        checkout = isolated_campfire(temp, redis_port)
        seed_rustfire(rust_db, rust_port, sets)
        camp_env = seed_campfire(checkout, RUBY, BUNDLE, REPOSITORY / "storage/db/production.sqlite3", camp_db, sets, camp_port, temp)
        camp_env["WEB_CONCURRENCY"] = str(args.campfire_workers)
        camp_env["REDIS_URL"] = f"redis://127.0.0.1:{redis_port}"
        camp_env["SECRET_KEY_BASE"] = SECRET
        with sqlite3.connect(camp_db) as db:
            db.execute("UPDATE users SET name='User '||id,updated_at='2026-01-01 00:00:00.000000' WHERE id IN (1,2)")

        def run_rustfire():
            process = start_server(rust_db, rust_port, {"RUSTFIRE_CAMPFIRE_SECRET_KEY_BASE": SECRET})
            try:
                return measure(rust_port, "session_token=benchmark-session", "benchmark-csrf", rust_db,
                               args.rooms, target_id, peers, args.sockets, args.requests, temp / "rust.html")
            finally:
                stop_server(process)

        def run_campfire():
            redis, redis_log = start_redis(temp, redis_port)
            log = open(temp / "puma.log", "w+")
            process = None
            try:
                process = subprocess.Popen([str(RUBY), str(RUBY.parent / "bundle"), "exec", "puma", "-C", "config/puma.rb"],
                                           cwd=checkout, env=camp_env, stdout=log, stderr=log)
                wait_for_server(camp_port, process)
                cookie, csrf = login_campfire(camp_port)
                return measure(camp_port, cookie, csrf, camp_db, args.rooms, target_id, peers,
                               args.sockets, args.requests, temp / "camp.html")
            finally:
                if process is not None:
                    stop_server(process)
                redis.terminate()
                redis.wait(timeout=10)
                redis_log.close()
                log.close()

        if args.campfire_first:
            camp_metrics, rust_metrics = run_campfire(), run_rustfire()
        else:
            rust_metrics, camp_metrics = run_rustfire(), run_campfire()
        rust_sample = (temp / "rust.html").read_bytes()
        camp_sample = (temp / "camp.html").read_bytes()
        assert rust_sample == camp_sample, "Sidebar Turbo frame bytes differ between the two apps"
        report = {
            "rustfire_revision": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
            "campfire_revision": revision,
            "order": "campfire-first" if args.campfire_first else "rustfire-first",
            "fixture": {"direct_rooms": args.rooms, "users": 51, "target_room_id": target_id,
                        "members": [1, *peers], "signed_sidebar_sockets": args.sockets,
                        "measured_reuse_requests": args.requests, "campfire_workers": args.campfire_workers},
            "sample_bytes": len(rust_sample), "sample_payload_equal": True,
            "rustfire": rust_metrics, "campfire": camp_metrics,
        }
        if args.report:
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
        print("PASS paired existing direct-room reuse and signed sidebar fanout")
        print(json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    main()
