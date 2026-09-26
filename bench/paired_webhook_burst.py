"""Compare complete webhook burst delivery on matched Campfire and Rustfire fixtures."""

import argparse
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import pathlib
import queue
import signal
import sqlite3
import subprocess
import tempfile
import threading
import time
import urllib.parse

from direct_lookup import free_port, start_server, stop_server
from paired_banned_content import BUNDLE, REDIS_CLI, REPOSITORY, REVISION, RUBY, isolated_campfire, start_redis
from paired_direct_lookup import login_campfire, seed_campfire, seed_rustfire, wait_for_server


BOT_IDS = range(52, 132)


class Receiver(BaseHTTPRequestHandler):
    received = queue.Queue()
    delay = 0.1

    def do_POST(self):
        payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        time.sleep(self.delay)
        self.send_response(204)
        self.end_headers()
        self.received.put((self.path, payload))

    def log_message(self, *_):
        pass


def seed_bots(database, campfire, receiver_port):
    stamp = "2026-01-01 00:00:00.000000" if campfire else "2026-01-01T00:00:00Z"
    with sqlite3.connect(database) as db:
        if campfire:
            db.execute("DELETE FROM sqlite_sequence WHERE name='messages'")
        db.executemany(
            "INSERT INTO users(id,name,role,status,bot_token,created_at,updated_at) VALUES(?1,?2,2,0,?3,?4,?4)",
            ((bot_id, f"Bot {bot_id}", f"token{bot_id}", stamp) for bot_id in BOT_IDS),
        )
        db.execute("INSERT INTO rooms(id,name,type,creator_id,created_at,updated_at) VALUES(2,NULL,'Rooms::Direct',1,?1,?1)", (stamp,))
        if campfire:
            db.executemany(
                "INSERT INTO memberships(room_id,user_id,involvement,created_at,updated_at) VALUES(2,?1,'everything',?2,?2)",
                ((user_id, stamp) for user_id in (1, *BOT_IDS)),
            )
            db.executemany(
                "INSERT INTO webhooks(user_id,url,created_at,updated_at) VALUES(?1,?2,?3,?3)",
                ((bot_id, f"http://127.0.0.1:{receiver_port}/hook/{bot_id}", stamp) for bot_id in BOT_IDS),
            )
        else:
            db.executemany(
                "INSERT INTO memberships(room_id,user_id,involvement,created_at) VALUES(2,?1,'everything',?2)",
                ((user_id, stamp) for user_id in (1, *BOT_IDS)),
            )
            db.executemany(
                "INSERT INTO webhooks(user_id,url) VALUES(?1,?2)",
                ((bot_id, f"http://127.0.0.1:{receiver_port}/hook/{bot_id}") for bot_id in BOT_IDS),
            )


def post(port, cookie, csrf):
    body = urllib.parse.urlencode({
        "message[body]": "<div>Webhook burst</div>",
        "message[client_message_id]": "paired-webhook-burst",
        "authenticity_token": csrf,
    })
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    try:
        connection.request("POST", "/rooms/2/messages", body, {
            "Cookie": cookie,
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "text/vnd.turbo-stream.html, text/html",
        })
        response = connection.getresponse()
        result = response.read()
        assert response.status == 200, (response.status, result[:500])
    finally:
        connection.close()


def measure(port, cookie, csrf, timeout):
    started = time.perf_counter()
    post(port, cookie, csrf)
    received = []
    deadline = time.monotonic() + timeout
    while len(received) < len(BOT_IDS):
        try:
            received.append(Receiver.received.get(timeout=max(0.01, deadline - time.monotonic())))
        except queue.Empty as error:
            raise AssertionError(f"received {len(received)}/{len(BOT_IDS)} webhooks before timeout") from error
    elapsed = time.perf_counter() - started
    result = sorted(received, key=lambda item: item[0])
    assert {path for path, _ in result} == {f"/hook/{bot_id}" for bot_id in BOT_IDS}
    assert Receiver.received.empty(), "unexpected extra webhook"
    return elapsed, result


def wait_for_workers(redis_port, workers):
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if any(worker.poll() is not None for worker in workers):
            raise RuntimeError("Campfire Resque worker exited during startup")
        result = subprocess.run([str(REDIS_CLI), "-p", str(redis_port), "SMEMBERS", "resque:workers"], capture_output=True, text=True, check=True)
        if len(result.stdout.splitlines()) >= len(workers):
            return
        time.sleep(.05)
    raise RuntimeError("Campfire Resque workers did not all register")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bots", type=int, default=80)
    parser.add_argument("--campfire-workers", type=int, default=8)
    parser.add_argument("--delay-ms", type=int, default=100)
    parser.add_argument("--campfire-first", action="store_true")
    parser.add_argument("--timeout", type=float, default=90)
    args = parser.parse_args()
    if args.bots < 1 or args.campfire_workers < 1 or args.delay_ms < 0 or args.timeout <= 0:
        parser.error("bots, workers, and timeout must be positive; delay must be nonnegative")
    global BOT_IDS
    BOT_IDS = range(52, 52 + args.bots)
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPOSITORY, text=True).strip() == REVISION
    Receiver.delay = args.delay_ms / 1000
    receiver_port, rust_port, camp_port, redis_port = (free_port() for _ in range(4))
    receiver = ThreadingHTTPServer(("127.0.0.1", receiver_port), Receiver)
    threading.Thread(target=receiver.serve_forever, daemon=True).start()
    try:
        with tempfile.TemporaryDirectory(prefix="paired-webhook-burst-") as scratch:
            temp = pathlib.Path(scratch)
            rust_db, camp_db = temp / "rust.sqlite3", temp / "camp.sqlite3"
            seed_rustfire(rust_db, rust_port, [])
            camp_env = seed_campfire(REPOSITORY, RUBY, BUNDLE, REPOSITORY / "storage/db/production.sqlite3", camp_db, [], camp_port, temp)
            checkout = isolated_campfire(temp, redis_port)
            camp_env.update({"REDIS_URL": f"redis://127.0.0.1:{redis_port}", "QUEUE": "default", "INTERVAL": "0.25"})
            seed_bots(rust_db, False, receiver_port)
            seed_bots(camp_db, True, receiver_port)
            with sqlite3.connect(camp_db) as camp, sqlite3.connect(rust_db) as rust:
                rust.execute("UPDATE users SET name=?2,updated_at=?3 WHERE id=?1", camp.execute("SELECT id,name,updated_at FROM users WHERE id=1").fetchone())
            redis, redis_log = start_redis(temp, redis_port)
            try:
                def rust_trial():
                    process = start_server(rust_db, rust_port, {"RUSTFIRE_DISABLE_WEBHOOKS": "0"})
                    try:
                        elapsed, received = measure(rust_port, "session_token=benchmark-session", "benchmark-csrf", args.timeout)
                        deadline = time.monotonic() + 5
                        while True:
                            with sqlite3.connect(rust_db) as db:
                                remaining = db.execute("SELECT COUNT(*) FROM webhook_jobs").fetchone()[0]
                            if remaining == 0:
                                break
                            assert time.monotonic() < deadline, f"{remaining} webhook jobs remain after delivery"
                            time.sleep(.01)
                        return elapsed, received
                    finally:
                        stop_server(process)

                def camp_trial():
                    with open(temp / "puma.log", "w+") as puma_log, open(temp / "worker.log", "w+") as worker_log:
                        camp = subprocess.Popen([str(RUBY), str(RUBY.parent / "bundle"), "exec", "puma", "-C", "config/puma.rb"], cwd=checkout, env=camp_env, stdout=puma_log, stderr=puma_log)
                        workers = [subprocess.Popen([str(RUBY), str(RUBY.parent / "bundle"), "exec", "rake", "resque:work"], cwd=checkout, env=camp_env, stdout=worker_log, stderr=worker_log, start_new_session=True) for _ in range(args.campfire_workers)]
                        try:
                            wait_for_server(camp_port, camp)
                            wait_for_workers(redis_port, workers)
                            cookie, csrf = login_campfire(camp_port)
                            return measure(camp_port, cookie, csrf, args.timeout)
                        finally:
                            stop_server(camp)
                            for worker in workers:
                                if worker.poll() is None:
                                    os.killpg(worker.pid, signal.SIGTERM)
                                worker.wait(timeout=10)

                if args.campfire_first:
                    camp_elapsed, camp_received = camp_trial()
                    rust_elapsed, rust_received = rust_trial()
                else:
                    rust_elapsed, rust_received = rust_trial()
                    camp_elapsed, camp_received = camp_trial()
            finally:
                redis.terminate()
                redis.wait(timeout=10)
                redis_log.close()
    finally:
        receiver.shutdown()
        receiver.server_close()
    assert rust_received == camp_received, (rust_received[:2], camp_received[:2])
    print(json.dumps({
        "bots": len(BOT_IDS),
        "receiver_delay_ms": args.delay_ms,
        "campfire_resque_workers": args.campfire_workers,
        "campfire_seconds": round(camp_elapsed, 3),
        "rustfire_seconds": round(rust_elapsed, 3),
        "campfire_deliveries": len(camp_received),
        "rustfire_deliveries": len(rust_received),
        "rustfire_elapsed_speedup": round(camp_elapsed / rust_elapsed, 2),
    }))


if __name__ == "__main__":
    main()
