"""Offer the same sustained bot-webhook load to Campfire and Rustfire."""

import argparse
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
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

import paired_webhook_burst as burst
from direct_lookup import free_port, start_server, stop_server
from paired_banned_content import BUNDLE, REDIS_CLI, REPOSITORY, REVISION, RUBY, isolated_campfire, start_redis
from paired_direct_lookup import login_campfire, seed_campfire, seed_rustfire, wait_for_server


class Receiver(BaseHTTPRequestHandler):
    received = queue.Queue()
    delay = 0.1

    def do_POST(self):
        payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        time.sleep(self.delay)
        self.send_response(204)
        self.end_headers()
        self.received.put((time.perf_counter(), self.path, payload))

    def log_message(self, *_):
        pass


def percentile(values, percent):
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * percent / 100) - 1)]


def post(port, cookie, csrf, client_id, index):
    body = urllib.parse.urlencode({
        "message[body]": f"<div>Sustained webhook {index:05d}</div>",
        "message[client_message_id]": client_id,
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


def receive(count, timeout):
    deadline = time.monotonic() + timeout
    received = []
    while len(received) < count:
        try:
            received.append(Receiver.received.get(timeout=max(0.01, deadline - time.monotonic())))
        except queue.Empty as error:
            raise AssertionError(f"received {len(received)}/{count} webhooks before timeout") from error
    return received


def wait_for_queue(app, database, redis_port=None):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if app == "rustfire":
            with sqlite3.connect(database) as db:
                queued = db.execute("SELECT COUNT(*) FROM webhook_jobs").fetchone()[0]
                failed = db.execute("SELECT COUNT(*) FROM failed_webhook_jobs").fetchone()[0]
        else:
            result = subprocess.run([str(REDIS_CLI), "-p", str(redis_port), "LLEN", "resque:queue:default"], capture_output=True, text=True, check=True)
            queued = int(result.stdout.strip())
            result = subprocess.run([str(REDIS_CLI), "-p", str(redis_port), "LLEN", "resque:failed"], capture_output=True, text=True, check=True)
            failed = int(result.stdout.strip())
        assert failed == 0, f"{app} retained {failed} failed webhook jobs"
        if queued == 0:
            return
        time.sleep(0.05)
    raise AssertionError(f"{app} retained {queued} queued webhook jobs")


def trial(app, database, port, cookie, csrf, bots, posts, rate, timeout, redis_port=None):
    Receiver.received = queue.Queue()
    post(port, cookie, csrf, "paired-webhook-sustained-warmup", -1)
    warmup = receive(bots, timeout)
    assert len({path for _, path, _ in warmup}) == bots
    wait_for_queue(app, database, redis_port)
    assert Receiver.received.empty(), "unexpected warmup delivery"

    interval = 1 / rate
    begun = time.perf_counter()
    post_started = {}
    post_ended = []
    schedule_lag = []
    post_durations = []
    for index in range(posts):
        due = begun + index * interval
        time.sleep(max(0, due - time.perf_counter()))
        started = time.perf_counter()
        client_id = f"paired-webhook-sustained-{index:05d}"
        post_started[client_id] = started
        schedule_lag.append(max(0, started - due))
        post(port, cookie, csrf, client_id, index)
        ended = time.perf_counter()
        post_ended.append(ended)
        post_durations.append(ended - started)
    sending_end = post_ended[-1]
    deliveries = receive(bots * posts, timeout)
    wait_for_queue(app, database, redis_port)
    time.sleep(0.25)
    assert Receiver.received.empty(), "unexpected extra delivery"

    with sqlite3.connect(database) as db:
        message_ids = dict(db.execute("SELECT id,client_message_id FROM messages WHERE client_message_id LIKE 'paired-webhook-sustained-%'"))
    assert len(message_ids) == posts + 1, (app, len(message_ids))
    signatures = []
    latencies = []
    seen = set()
    for arrived, path, payload in deliveries:
        message_id = payload["message"]["id"]
        client_id = message_ids[message_id]
        assert client_id in post_started, (app, client_id)
        identity = (message_id, path)
        assert identity not in seen, (app, identity)
        seen.add(identity)
        latencies.append(arrived - post_started[client_id])
        signatures.append((path, message_id, json.dumps(payload, sort_keys=True)))
    assert {path for _, path in seen} == {f"/hook/{bot_id}" for bot_id in burst.BOT_IDS}
    assert {message_id for message_id, _ in seen} == {mid for mid, client_id in message_ids.items() if client_id in post_started}
    arrived_during_posting = sum(arrived <= sending_end for arrived, _, _ in deliveries)
    final_delivery = max(arrived for arrived, _, _ in deliveries)
    return {
        "app": app,
        "posts": posts,
        "deliveries": len(deliveries),
        "offered_posts_per_second": round(posts / (sending_end - begun), 2),
        "deliveries_during_posting": arrived_during_posting,
        "delivery_rate_during_posting": round(arrived_during_posting / (sending_end - begun), 2),
        "remaining_after_posting": bots * posts - arrived_during_posting,
        "drain_seconds": round(max(0, final_delivery - sending_end), 3),
        "total_seconds": round(max(sending_end, final_delivery) - begun, 3),
        "delivery_p50_ms": round(percentile(latencies, 50) * 1000, 2),
        "delivery_p95_ms": round(percentile(latencies, 95) * 1000, 2),
        "post_p95_ms": round(percentile(post_durations, 95) * 1000, 2),
        "schedule_lag_p95_ms": round(percentile(schedule_lag, 95) * 1000, 2),
        "schedule_lag_max_ms": round(max(schedule_lag) * 1000, 2),
    }, sorted(signatures)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bots", type=int, default=8)
    parser.add_argument("--posts", type=int, default=360)
    parser.add_argument("--rate", type=float, default=12, help="offered message posts per second")
    parser.add_argument("--delay-ms", type=int, default=100)
    parser.add_argument("--campfire-workers", type=int, default=8)
    parser.add_argument("--timeout", type=float, default=90)
    parser.add_argument("--campfire-first", action="store_true")
    args = parser.parse_args()
    if min(args.bots, args.posts, args.rate, args.campfire_workers, args.timeout) <= 0 or args.delay_ms < 0:
        parser.error("bots, posts, rate, workers, and timeout must be positive; delay must be nonnegative")
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPOSITORY, text=True).strip() == REVISION
    burst.BOT_IDS = range(52, 52 + args.bots)
    Receiver.delay = args.delay_ms / 1000
    receiver_port, rust_port, camp_port, redis_port = (free_port() for _ in range(4))
    receiver = ThreadingHTTPServer(("127.0.0.1", receiver_port), Receiver)
    thread = threading.Thread(target=receiver.serve_forever, daemon=True)
    thread.start()
    try:
        with tempfile.TemporaryDirectory(prefix="paired-webhook-sustained-") as scratch:
            temp = pathlib.Path(scratch)
            rust_db, camp_db = temp / "rust.sqlite3", temp / "camp.sqlite3"
            seed_rustfire(rust_db, rust_port, [])
            camp_env = seed_campfire(REPOSITORY, RUBY, BUNDLE, REPOSITORY / "storage/db/production.sqlite3", camp_db, [], camp_port, temp)
            checkout = isolated_campfire(temp, redis_port)
            camp_env.update({"REDIS_URL": f"redis://127.0.0.1:{redis_port}", "QUEUE": "default", "INTERVAL": "0.25"})
            burst.seed_bots(rust_db, False, receiver_port)
            burst.seed_bots(camp_db, True, receiver_port)
            with sqlite3.connect(camp_db) as camp, sqlite3.connect(rust_db) as rust:
                rust.execute("UPDATE users SET name=?2,updated_at=?3 WHERE id=?1", camp.execute("SELECT id,name,updated_at FROM users WHERE id=1").fetchone())
            redis, redis_log = start_redis(temp, redis_port)
            try:
                def rust_trial():
                    process = start_server(rust_db, rust_port, {"RUSTFIRE_DISABLE_WEBHOOKS": "0"})
                    try:
                        return trial("rustfire", rust_db, rust_port, "session_token=benchmark-session", "benchmark-csrf", args.bots, args.posts, args.rate, args.timeout)
                    finally:
                        stop_server(process)

                def camp_trial():
                    with open(temp / "puma.log", "w+") as puma_log, open(temp / "worker.log", "w+") as worker_log:
                        camp = subprocess.Popen([str(RUBY), str(RUBY.parent / "bundle"), "exec", "puma", "-C", "config/puma.rb"], cwd=checkout, env=camp_env, stdout=puma_log, stderr=puma_log)
                        workers = [subprocess.Popen([str(RUBY), str(RUBY.parent / "bundle"), "exec", "rake", "resque:work"], cwd=checkout, env=camp_env, stdout=worker_log, stderr=worker_log, start_new_session=True) for _ in range(args.campfire_workers)]
                        try:
                            wait_for_server(camp_port, camp)
                            burst.wait_for_workers(redis_port, workers)
                            cookie, csrf = login_campfire(camp_port)
                            return trial("campfire", camp_db, camp_port, cookie, csrf, args.bots, args.posts, args.rate, args.timeout, redis_port)
                        finally:
                            stop_server(camp)
                            for worker in workers:
                                if worker.poll() is None:
                                    os.killpg(worker.pid, signal.SIGTERM)
                                worker.wait(timeout=10)

                if args.campfire_first:
                    (camp_report, camp_signatures), (rust_report, rust_signatures) = camp_trial(), rust_trial()
                else:
                    (rust_report, rust_signatures), (camp_report, camp_signatures) = rust_trial(), camp_trial()
            finally:
                redis.terminate()
                redis.wait(timeout=10)
                redis_log.close()
    finally:
        receiver.shutdown()
        receiver.server_close()
        thread.join(timeout=5)
    assert rust_signatures == camp_signatures, next(
        ((index, left, right) for index, (left, right) in enumerate(zip(rust_signatures, camp_signatures)) if left != right),
        (len(rust_signatures), len(camp_signatures)),
    )
    print(json.dumps({"bots": args.bots, "posts": args.posts, "rate": args.rate, "receiver_delay_ms": args.delay_ms,
                      "campfire_resque_workers": args.campfire_workers, "campfire_first": args.campfire_first,
                      "payloads_match": True, "rustfire": rust_report, "campfire": camp_report}, sort_keys=True))


if __name__ == "__main__":
    main()
