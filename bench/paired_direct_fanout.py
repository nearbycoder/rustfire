"""Compare matched direct-room creation and signed per-user sidebar Turbo fanout.

Requires a release Rustfire build and the pinned Campfire checkout/bundle.
Each app gets a disposable database; Campfire also gets isolated Redis.
"""

import argparse
import json
import pathlib
import re
import shutil
import sqlite3
import subprocess
import tempfile
import time

from direct_lookup import ROOT, free_port, start_server, stop_server
from paired_banned_content import REPOSITORY, RUBY, BUNDLE, REVISION, isolated_campfire, start_redis
from paired_direct_lookup import login_campfire, seed_campfire, seed_rustfire, wait_for_server
from paired_direct_sidebar import SECRET


def capture(port, cookie, csrf, sockets, rooms, sample_file):
    command = [
        "node", "bench/direct_fanout.mjs", "--base", f"http://127.0.0.1:{port}",
        "--cookie", cookie, "--csrf", csrf, "--sockets", str(sockets),
        "--rooms", str(rooms), "--sample-file", str(sample_file),
    ]
    result = subprocess.run(command, cwd=ROOT, text=True, capture_output=True, timeout=180)
    if result.returncode:
        raise RuntimeError(f"Direct fanout failed: {result.stdout}\n{result.stderr}")
    metrics = json.loads(result.stdout.strip().splitlines()[-1])
    if any(metrics[key] for key in ("missed", "unexpected", "closed_early")):
        raise AssertionError(metrics)
    return metrics


def created_state(database, rooms):
    with sqlite3.connect(database) as db:
        rows = db.execute(
            "SELECT r.id,r.type,r.name,GROUP_CONCAT(m.user_id) FROM rooms r "
            "JOIN memberships m ON m.room_id=r.id WHERE r.id BETWEEN 2 AND ? "
            "GROUP BY r.id ORDER BY r.id", (rooms + 5,),
        ).fetchall()
    assert len(rows) == rooms + 4, (len(rows), rooms + 4)
    for room_id, room_type, name, members in rows:
        assert room_type == "Rooms::Direct" and name is None, (room_id, room_type, name)
        assert sorted(map(int, members.split(","))) == [1, room_id], (room_id, members)
    return {"direct_rooms": len(rows), "memberships_per_room": 2}


def normalized_sample(path):
    html = path.read_text()
    assert html.startswith('<turbo-stream action="prepend" target="direct_rooms"><template>'), html[:200]
    assert 'id="list_rooms_direct_6"' in html, html[:300]
    epoch = re.search(r'data-sorted-list-number="(\d+)"', html)
    assert epoch and abs(int(epoch.group(1)) - int(time.time() * 1000)) < 60_000, "Direct-room sort time is not current epoch milliseconds"
    return re.sub(r'data-sorted-list-number="\d+"', 'data-sorted-list-number="<epoch-ms>"', html)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sockets", type=int, default=200)
    parser.add_argument("--rooms", type=int, default=10)
    parser.add_argument("--campfire-workers", type=int, default=22)
    parser.add_argument("--campfire-first", action="store_true")
    parser.add_argument("--sample-dir", type=pathlib.Path)
    args = parser.parse_args()
    if min(args.sockets, args.rooms, args.campfire_workers) < 1 or args.rooms > 46:
        parser.error("sockets and workers must be positive; rooms must be between 1 and 46")
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPOSITORY, text=True).strip()
    assert revision == REVISION, revision
    with tempfile.TemporaryDirectory(prefix="paired-direct-fanout-") as temporary:
        temp = pathlib.Path(temporary)
        rust_db, camp_db = temp / "rustfire.sqlite3", temp / "campfire.sqlite3"
        rust_port, camp_port, redis_port = free_port(), free_port(), free_port()
        checkout = isolated_campfire(temp, redis_port)
        seed_rustfire(rust_db, rust_port, [])
        camp_env = seed_campfire(checkout, RUBY, BUNDLE, REPOSITORY / "storage/db/production.sqlite3", camp_db, [], camp_port, temp)
        camp_env["WEB_CONCURRENCY"] = str(args.campfire_workers)
        camp_env["REDIS_URL"] = f"redis://127.0.0.1:{redis_port}"
        camp_env["SECRET_KEY_BASE"] = SECRET
        with sqlite3.connect(camp_db) as db:
            db.execute("UPDATE users SET name='User '||id,updated_at='2026-01-01 00:00:00.000000' WHERE id IN (1,2)")

        def run_rustfire():
            process = start_server(rust_db, rust_port, {"RUSTFIRE_CAMPFIRE_SECRET_KEY_BASE": SECRET})
            try:
                metrics = capture(rust_port, "session_token=benchmark-session", "benchmark-csrf", args.sockets, args.rooms, temp / "rust-direct.html")
                return metrics, created_state(rust_db, args.rooms)
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
                metrics = capture(camp_port, cookie, csrf, args.sockets, args.rooms, temp / "camp-direct.html")
                return metrics, created_state(camp_db, args.rooms)
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
        if args.sample_dir:
            args.sample_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(temp / "rust-direct.html", args.sample_dir / "rustfire.html")
            shutil.copy2(temp / "camp-direct.html", args.sample_dir / "campfire.html")
        rust_sample = normalized_sample(temp / "rust-direct.html")
        camp_sample = normalized_sample(temp / "camp-direct.html")
        assert rust_sample == camp_sample, "Matched direct-room Turbo samples differ; use --sample-dir to inspect"
        assert rust_result[1] == camp_result[1], (rust_result[1], camp_result[1])
        print("PASS paired direct-room creation and signed per-user sidebar Turbo delivery")
        print(json.dumps({
            "campfire_workers": args.campfire_workers, "campfire_first": args.campfire_first,
            "rustfire": {"metrics": rust_result[0], "state": rust_result[1]},
            "campfire": {"metrics": camp_result[0], "state": camp_result[1]},
            "sample_bytes": len(rust_sample.encode()),
        }, sort_keys=True))


if __name__ == "__main__":
    main()
