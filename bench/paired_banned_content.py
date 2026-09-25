"""Compare queued banned-content cleanup and Turbo delivery with pinned Campfire.

Campfire runs from a disposable source copy with its own Redis server and Resque
worker, leaving the shared Redis instance and pinned checkout untouched.
"""

import pathlib
import argparse
import json
import os
import sqlite3
import shutil
import signal
import subprocess
import tempfile
import time

from direct_lookup import free_port, start_server, stop_server
from paired_bans import seed_sessions
from paired_bot_admin import request
from paired_direct_lookup import login_campfire, seed_campfire, seed_rustfire, wait_for_server


REPOSITORY = pathlib.Path("/tmp/once-campfire-reference")
RUBY = pathlib.Path("/tmp/rustfire-baseline/local/bin/ruby")
BUNDLE = pathlib.Path("/tmp/rustfire-baseline/bundle")
REVISION = "91d294f4a09f9bbe37f9548959bfcb43645678fb"
COUNT = 55
REDIS_SERVER = pathlib.Path("/tmp/rustfire-baseline/redis-7.2.11/src/redis-server")
REDIS_CLI = REDIS_SERVER.with_name("redis-cli")


def seed_messages(database, campfire):
    stamp = "2026-01-01 00:00:00.000000" if campfire else "2026-01-01T00:00:00Z"
    with sqlite3.connect(database) as db:
        if campfire:
            db.executemany(
                "INSERT INTO messages(id,client_message_id,created_at,creator_id,room_id,updated_at) VALUES(?1,?2,?3,2,1,?3)",
                ((index, f"banned-{index}", stamp) for index in range(1, COUNT + 1)),
            )
            db.executemany(
                "INSERT INTO action_text_rich_texts(name,body,record_type,record_id,created_at,updated_at) VALUES('body',?1,'Message',?2,?3,?3)",
                ((f"Banned message {index}", index, stamp) for index in range(1, COUNT + 1)),
            )
            db.executemany(
                "INSERT INTO message_search_index(rowid,body) VALUES(?1,?2)",
                ((index, f"Banned message {index}") for index in range(1, COUNT + 1)),
            )
            db.execute("INSERT INTO boosts(id,booster_id,content,created_at,message_id,updated_at) VALUES(1,1,'+1',?1,1,?1)", (stamp,))
        else:
            db.executemany(
                "INSERT INTO messages(id,room_id,creator_id,body,client_message_id,created_at,updated_at) VALUES(?1,1,2,?2,?3,?4,?4)",
                ((index, f"Banned message {index}", f"banned-{index}", stamp) for index in range(1, COUNT + 1)),
            )
            db.execute("INSERT INTO boosts(id,message_id,booster_id,content,created_at) VALUES(1,1,1,'+1',?1)", (stamp,))


def state(database, campfire):
    with sqlite3.connect(database) as db:
        counts = tuple(
            db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("messages", "message_search_index", "boosts")
        )
        rich = db.execute("SELECT COUNT(*) FROM action_text_rich_texts WHERE record_type='Message'").fetchone()[0] if campfire else None
        jobs = db.execute("SELECT COUNT(*) FROM background_jobs").fetchone()[0] if not campfire else None
        room_touched = db.execute("SELECT updated_at FROM rooms WHERE id=1").fetchone()[0]
    return counts, rich, jobs, room_touched


def capture(port, cookie, sockets, sample_file):
    process = subprocess.Popen(
        ["node", "bench/capture_ban_removals.mjs", "--base", f"http://127.0.0.1:{port}", "--cookie", cookie, "--sockets", str(sockets), "--messages", str(COUNT), "--sample-file", str(sample_file)],
        cwd=pathlib.Path(__file__).resolve().parents[1], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    ready = process.stdout.readline().strip()
    if ready != "READY":
        try:
            _, error = process.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            _, error = process.communicate()
        raise RuntimeError(f"Socket capture did not start: {ready}\n{error[-2000:]}")
    return process


def captured(process):
    try:
        output, error = process.communicate(timeout=35)
    except subprocess.TimeoutExpired:
        process.kill()
        output, error = process.communicate()
        raise AssertionError(f"Socket capture timed out: {output}\n{error[-2000:]}")
    assert process.returncode == 0, (output, error[-2000:])
    result = json.loads(output.strip().splitlines()[-1])
    assert result["missed"] == result["unexpected"] == 0, result
    return result


def isolated_campfire(temp, redis_port):
    checkout = temp / "source"
    shutil.copytree(REPOSITORY, checkout, ignore=shutil.ignore_patterns(".git", "storage", "test", "log", "tmp"))
    for directory in ("storage", "log", "tmp/pids"):
        (checkout / directory).mkdir(parents=True, exist_ok=True)
    cable = checkout / "config/cable.yml"
    cable.write_text(cable.read_text().replace("redis://localhost:6379", f"redis://127.0.0.1:{redis_port}"))
    (checkout / "config/initializers/isolated_resque.rb").write_text('require "resque"\nResque.redis = ENV.fetch("REDIS_URL")\n')
    return checkout


def start_redis(temp, port):
    log = open(temp / "redis.log", "w+")
    try:
        process = subprocess.Popen(
            [str(REDIS_SERVER), "--bind", "127.0.0.1", "--port", str(port), "--save", "", "--appendonly", "no", "--dir", str(temp)],
            stdout=log, stderr=log,
        )
    except Exception:
        log.close()
        raise
    try:
        for _ in range(100):
            if process.poll() is not None:
                raise RuntimeError("Isolated Redis exited during startup")
            result = subprocess.run([str(REDIS_CLI), "-p", str(port), "PING"], capture_output=True, text=True)
            if result.returncode == 0 and result.stdout.strip() == "PONG":
                return process, log
            time.sleep(.05)
        raise RuntimeError("Isolated Redis did not start")
    except Exception:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=10)
        log.close()
        raise


def wait_for_worker(port, worker):
    for _ in range(200):
        if worker.poll() is not None:
            raise RuntimeError("Campfire Resque worker exited during startup")
        result = subprocess.run([str(REDIS_CLI), "-p", str(port), "SMEMBERS", "resque:workers"], capture_output=True, text=True)
        if result.returncode == 0 and result.stdout.strip():
            return
        time.sleep(.05)
    raise RuntimeError("Campfire Resque worker did not register")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--sockets", type=int, default=10)
    parser.add_argument("--campfire-workers", type=int, default=1)
    parser.add_argument("--campfire-first", action="store_true", help="reverse the serial trial order")
    args = parser.parse_args()
    if args.sockets < 1 or args.campfire_workers < 1:
        parser.error("sockets and Campfire workers must be positive")
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPOSITORY, text=True).strip()
    assert revision == REVISION, revision
    with tempfile.TemporaryDirectory(prefix="paired-banned-content-") as temporary:
        temp = pathlib.Path(temporary)
        rust_db, camp_db = temp / "rustfire.sqlite3", temp / "campfire.sqlite3"
        rust_port, camp_port, redis_port = free_port(), free_port(), free_port()
        checkout = isolated_campfire(temp, redis_port)
        seed_rustfire(rust_db, rust_port, [])
        env = seed_campfire(checkout, RUBY, BUNDLE, REPOSITORY / "storage/db/production.sqlite3", camp_db, [], camp_port, temp)
        env["REDIS_URL"] = f"redis://127.0.0.1:{redis_port}"
        env["QUEUE"] = "default"
        env["INTERVAL"] = "0.25"
        env["WEB_CONCURRENCY"] = str(args.campfire_workers)
        seed_messages(rust_db, False)
        seed_messages(camp_db, True)
        seed_sessions(rust_db, False)
        seed_sessions(camp_db, True)
        rust_before, camp_before = state(rust_db, False), state(camp_db, True)
        assert rust_before[0] == camp_before[0] == (COUNT, COUNT, 1)
        assert rust_before[2] == 0 and camp_before[1] == COUNT

        def run_rustfire():
            process = start_server(rust_db, rust_port)
            try:
                rust_capture = capture(rust_port, "session_token=benchmark-session", args.sockets, temp / "rust-remove.html")
                try:
                    status, location, body = request(rust_port, "POST", "/users/2/ban", "session_token=benchmark-session", "benchmark-csrf")
                    assert status == 302 and location.endswith("/users/2"), (status, body[:200])
                    delivery = captured(rust_capture)
                finally:
                    if rust_capture.poll() is None:
                        rust_capture.kill()
                        rust_capture.communicate()
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    after = state(rust_db, False)
                    if after[:3] == ((0, 0, 0), None, 0):
                        break
                    time.sleep(.05)
                else:
                    raise AssertionError(("Rustfire cleanup timed out", after))
                return after, delivery
            finally:
                stop_server(process)

        def run_campfire():
            redis_process, redis_log = start_redis(temp, redis_port)
            log = open(temp / "puma.log", "w+")
            worker_log = open(temp / "worker.log", "w+")
            camp_process = None
            worker = None
            try:
                camp_process = subprocess.Popen([str(RUBY), str(RUBY.parent / "bundle"), "exec", "puma", "-C", "config/puma.rb"], cwd=checkout, env=env, stdout=log, stderr=log)
                worker = subprocess.Popen([str(RUBY), str(RUBY.parent / "bundle"), "exec", "rake", "resque:work"], cwd=checkout, env=env, stdout=worker_log, stderr=worker_log, start_new_session=True)
                wait_for_server(camp_port, camp_process)
                wait_for_worker(redis_port, worker)
                cookie, csrf = login_campfire(camp_port)
                camp_capture = capture(camp_port, cookie, args.sockets, temp / "camp-remove.html")
                try:
                    status, location, body = request(camp_port, "POST", "/users/2/ban", cookie, csrf)
                    assert status == 302 and location.endswith("/users/2"), (status, body[:200])
                    delivery = captured(camp_capture)
                finally:
                    if camp_capture.poll() is None:
                        camp_capture.kill()
                        camp_capture.communicate()
                return state(camp_db, True), delivery
            finally:
                if camp_process is not None:
                    stop_server(camp_process)
                if worker is not None:
                    try:
                        os.killpg(worker.pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                    try:
                        worker.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        os.killpg(worker.pid, signal.SIGKILL)
                        worker.wait(timeout=10)
                redis_process.terminate()
                try:
                    redis_process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    redis_process.kill()
                    redis_process.wait(timeout=10)
                log.close()
                worker_log.close()
                redis_log.close()

        if args.campfire_first:
            camp_after, camp_delivery = run_campfire()
            rust_after, rust_delivery = run_rustfire()
        else:
            rust_after, rust_delivery = run_rustfire()
            camp_after, camp_delivery = run_campfire()
        camp_after = state(camp_db, True)
        assert camp_after[:2] == ((0, 0, 0), 0), camp_after
        assert rust_after[3] != rust_before[3] and camp_after[3] != camp_before[3]
        assert (temp / "rust-remove.html").read_bytes() == (temp / "camp-remove.html").read_bytes()
        print(f"PASS paired {COUNT}-message banned-content removal: messages, search entries, rich bodies, and boosts removed; rooms touched")
        print(f"campfire_workers={args.campfire_workers} campfire_first={args.campfire_first} rustfire_delivery={rust_delivery} campfire_delivery={camp_delivery}")


if __name__ == "__main__":
    main()
