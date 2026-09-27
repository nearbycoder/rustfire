"""Measure matched user posts, bot webhooks, and saved text replies on both apps."""

import argparse
import concurrent.futures
import html
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import os
import pathlib
import queue
import select
import signal
import sqlite3
import subprocess
import tempfile
import threading
import time
import urllib.parse

from direct_lookup import ROOT, free_port, start_server, stop_server
from paired_banned_content import BUNDLE, REPOSITORY, REVISION, RUBY, isolated_campfire, start_redis, wait_for_worker
from paired_direct_lookup import login_campfire, seed_campfire, seed_rustfire, wait_for_server
from paired_message_cache import resource_snapshot
from paired_mention_webhook import bot_sgid, seed_bot
from paired_webhook_sustained import wait_for_queue


REPLY = b"Acknowledged"


class Receiver(BaseHTTPRequestHandler):
    received = queue.Queue()
    delay = 0

    def do_POST(self):
        payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        time.sleep(self.delay)
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(REPLY)))
        self.end_headers()
        self.wfile.write(REPLY)
        self.received.put((time.perf_counter(), payload))

    def log_message(self, *_):
        pass


def percentile(values, percent):
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * percent / 100) - 1)]


def post(port, cookie, csrf, sgid, index):
    attachment = f'<action-text-attachment sgid="{html.escape(sgid, quote=True)}" content-type="application/vnd.campfire.mention"></action-text-attachment>'
    body = urllib.parse.urlencode({
        "message[body]": f"<div>{attachment} load:{index:05d}</div>",
        "message[client_message_id]": f"paired-reply-load-{index:05d}",
        "authenticity_token": csrf,
    })
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    try:
        connection.request("POST", "/rooms/1/messages", body, {
            "Cookie": cookie,
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "text/vnd.turbo-stream.html, text/html",
        })
        response = connection.getresponse()
        payload = response.read()
        assert response.status == 200, (response.status, payload[:300])
    finally:
        connection.close()


def saved_replies(database, campfire):
    with sqlite3.connect(database) as db:
        if campfire:
            return db.execute("SELECT m.id,t.body FROM messages m JOIN action_text_rich_texts t ON t.record_type='Message' AND t.record_id=m.id AND t.name='body' WHERE m.creator_id=52 ORDER BY m.id").fetchall()
        return db.execute("SELECT id,body FROM messages WHERE creator_id=52 ORDER BY id").fetchall()


def trial(app, port, database, cookie, csrf, posts, rate, timeout, redis_port=None):
    Receiver.received = queue.Queue()
    sgid = bot_sgid(port, cookie, app == "campfire")
    interval = 1 / rate
    begun = time.perf_counter()

    def timed_post(index, due):
        started = time.perf_counter()
        post(port, cookie, csrf, sgid, index)
        return index, started, time.perf_counter(), max(0, started - due)

    with concurrent.futures.ThreadPoolExecutor(max_workers=min(64, posts)) as executor:
        futures = []
        for index in range(1, posts + 1):
            due = begun + (index - 1) * interval
            time.sleep(max(0, due - time.perf_counter()))
            futures.append(executor.submit(timed_post, index, due))
        completed = [future.result(timeout=35) for future in futures]
    starts = {index: started for index, started, _, _ in completed}
    sending_end = max(ended for _, _, ended, _ in completed)
    post_durations = [ended - started for _, started, ended, _ in completed]
    lag = [delay for _, _, _, delay in completed]
    received = []
    deadline = time.monotonic() + timeout
    while len(received) < posts:
        received.append(Receiver.received.get(timeout=max(0.01, deadline - time.monotonic())))
    with sqlite3.connect(database) as db:
        trigger_ids = {
            int(client_id.rsplit("-", 1)[1]): message_id
            for message_id, client_id in db.execute("SELECT id,client_message_id FROM messages WHERE creator_id=1 AND client_message_id LIKE 'paired-reply-load-%'")
        }
    assert len(trigger_ids) == posts, (app, len(trigger_ids), posts)
    seen = set()
    signatures = []
    delivery_latencies = []
    for arrived, payload in received:
        plain = payload["message"]["body"]["plain"]
        assert plain.startswith("load:"), (app, plain)
        index = int(plain.removeprefix("load:"))
        assert index in starts and index not in seen, (app, index)
        message_id = trigger_ids[index]
        assert payload["message"]["id"] == message_id and payload["message"]["path"] == f"/rooms/1/@{message_id}", (app, index, payload["message"])
        assert payload["room"]["id"] == 1 and payload["room"]["path"] == "/rooms/1/52-pairedBot123/messages", (app, index, payload["room"])
        seen.add(index)
        delivery_latencies.append(arrived - starts[index])
        signatures.append((index, payload["user"], payload["room"]["name"], payload["message"]["body"]))
    assert len(seen) == posts and Receiver.received.empty(), (app, len(seen))
    while time.monotonic() < deadline:
        replies = saved_replies(database, app == "campfire")
        if len(replies) >= posts:
            break
        time.sleep(0.05)
    else:
        raise AssertionError((app, len(saved_replies(database, app == "campfire")), posts))
    saved_end = time.perf_counter()
    assert len(replies) == posts and all(body == REPLY.decode() for _, body in replies), (app, replies[:3], len(replies))
    wait_for_queue(app, database, redis_port)
    with sqlite3.connect(database) as db:
        if app == "rustfire":
            queued = db.execute("SELECT COUNT(*) FROM webhook_jobs").fetchone()[0]
            failed = db.execute("SELECT COUNT(*) FROM failed_webhook_jobs").fetchone()[0]
            assert (queued, failed) == (0, 0), (queued, failed)
    time.sleep(0.2)
    assert Receiver.received.empty(), f"{app} sent extra webhooks"
    return {
        "app": app,
        "posts": posts,
        "webhooks": len(received),
        "replies": len(replies),
        "completed_posts_per_second": round(posts / (sending_end - begun), 2),
        "webhook_p95_ms": round(percentile(delivery_latencies, 95) * 1000, 2),
        "post_p95_ms": round(percentile(post_durations, 95) * 1000, 2),
        "schedule_lag_p95_ms": round(percentile(lag, 95) * 1000, 2),
        "reply_drain_seconds": round(max(0, saved_end - sending_end), 3),
        "total_seconds": round(saved_end - begun, 3),
    }, sorted(signatures)


def sampled_trial(process_pids, *args):
    if not process_pids:
        return trial(*args)
    stop = threading.Event()
    samples = [resource_snapshot(process_pids)]
    sample_errors = []

    def sample():
        while not stop.wait(0.2):
            try:
                samples.append(resource_snapshot(process_pids))
            except Exception as error:
                sample_errors.append(error)
                return

    sampler = threading.Thread(target=sample, daemon=True)
    sampler.start()
    try:
        report, signatures = trial(*args)
    finally:
        stop.set()
        sampler.join(timeout=2)
    if sample_errors:
        raise RuntimeError("Server resource sampling failed") from sample_errors[0]
    samples.append(resource_snapshot(process_pids))
    report["sampled_peak_server_pss_mib"] = round(max(item[0] for item in samples) / 1048576, 2)
    report["sampled_peak_server_processes"] = max(item[2] for item in samples)
    report["resource_samples"] = len(samples)
    return report, signatures


def captured_trial(process_pids, app, port, database, cookie, csrf, posts, rate, timeout, sockets, redis_port=None):
    if not sockets:
        return sampled_trial(process_pids, app, port, database, cookie, csrf, posts, rate, timeout, redis_port)
    command = [
        "node", "bench/capture_message_appends.mjs", "--base", f"http://127.0.0.1:{port}",
        "--cookie", cookie, "--room", "1", "--client-prefix", "paired-reply-load",
        "--first-id", "0", "--sockets", str(sockets), "--messages", str(posts),
        "--bot-replies", str(posts), "--timeout", str(round((timeout + posts / rate + 30) * 1000)),
        "--await-start", "1",
    ]
    capture = subprocess.Popen(command, cwd=ROOT, text=True, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        ready, _, _ = select.select([capture.stdout], [], [], 60)
        marker = capture.stdout.readline().strip() if ready else ""
        assert marker == "READY", (app, marker)
        capture.stdin.write("START\n")
        capture.stdin.flush()
        capture.stdin.close()
        capture.stdin = None
        report, signatures = sampled_trial(process_pids, app, port, database, cookie, csrf, posts, rate, timeout, redis_port)
        stdout, stderr = capture.communicate(timeout=timeout + posts / rate + 35)
        assert capture.returncode == 0, (app, stdout, stderr)
        delivery = json.loads(stdout.strip().splitlines()[-1])
        assert delivery["received"] == posts * sockets and delivery["reply_received"] == posts * sockets, (app, delivery)
        report["socket_delivery"] = delivery
        return report, signatures
    finally:
        if capture.poll() is None:
            capture.kill()
            capture.communicate(timeout=5)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--posts", type=int, default=120)
    parser.add_argument("--rate", type=float, default=24)
    parser.add_argument("--delay-ms", type=int, default=25)
    parser.add_argument("--campfire-workers", type=int, default=4)
    parser.add_argument("--timeout", type=float, default=60)
    parser.add_argument("--campfire-first", action="store_true")
    parser.add_argument("--sockets", type=int, default=0, help="subscribe room-message sockets and check both user and bot appends")
    parser.add_argument("--resources", action="store_true", help="sample server-process PSS (requires psutil)")
    parser.add_argument("--report", type=pathlib.Path)
    args = parser.parse_args()
    if min(args.posts, args.rate, args.campfire_workers, args.timeout) <= 0 or args.delay_ms < 0 or args.sockets < 0:
        parser.error("posts, rate, workers, and timeout must be positive; delay and sockets must be nonnegative")
    if args.resources:
        try:
            import psutil  # noqa: F401
        except ImportError:
            parser.error("--resources requires psutil")
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPOSITORY, text=True).strip() == REVISION
    Receiver.delay = args.delay_ms / 1000
    receiver_port, rust_port, camp_port, redis_port = (free_port() for _ in range(4))
    receiver = ThreadingHTTPServer(("127.0.0.1", receiver_port), Receiver)
    thread = threading.Thread(target=receiver.serve_forever, daemon=True)
    thread.start()
    try:
        with tempfile.TemporaryDirectory(prefix="paired-webhook-reply-load-") as scratch:
            temp = pathlib.Path(scratch)
            rust_db, camp_db = temp / "rust.sqlite3", temp / "camp.sqlite3"
            seed_rustfire(rust_db, rust_port, [])
            camp_env = seed_campfire(REPOSITORY, RUBY, BUNDLE, REPOSITORY / "storage/db/production.sqlite3", camp_db, [], camp_port, temp)
            checkout = isolated_campfire(temp, redis_port)
            camp_env.update({"REDIS_URL": f"redis://127.0.0.1:{redis_port}", "QUEUE": "default", "INTERVAL": "0.25"})
            url = f"http://127.0.0.1:{receiver_port}/hook"
            seed_bot(rust_db, False, url)
            seed_bot(camp_db, True, url)
            with sqlite3.connect(camp_db) as camp, sqlite3.connect(rust_db) as rust:
                rust.executemany("UPDATE users SET name=?2,updated_at=?3 WHERE id=?1", camp.execute("SELECT id,name,updated_at FROM users WHERE id IN(1,2)").fetchall())
                rust.execute("UPDATE rooms SET name=? WHERE id=1", [camp.execute("SELECT name FROM rooms WHERE id=1").fetchone()[0]])
            redis, redis_log = start_redis(temp, redis_port)
            try:
                def rust_trial():
                    server = start_server(rust_db, rust_port, {"RUSTFIRE_CAMPFIRE_SECRET_KEY_BASE": camp_env["SECRET_KEY_BASE"], "RUSTFIRE_DISABLE_WEBHOOKS": "0"})
                    try:
                        return captured_trial((server.pid,) if args.resources else (), "rustfire", rust_port, rust_db, "session_token=benchmark-session", "benchmark-csrf", args.posts, args.rate, args.timeout, args.sockets)
                    finally:
                        stop_server(server)

                def camp_trial():
                    camp_env["WEB_CONCURRENCY"] = "1"
                    with open(temp / "puma.log", "w+") as puma_log, open(temp / "worker.log", "w+") as worker_log:
                        camp = subprocess.Popen([str(RUBY), str(RUBY.parent / "bundle"), "exec", "puma", "-C", "config/puma.rb"], cwd=checkout, env=camp_env, stdout=puma_log, stderr=puma_log)
                        workers = [subprocess.Popen([str(RUBY), str(RUBY.parent / "bundle"), "exec", "rake", "resque:work"], cwd=checkout, env=camp_env, stdout=worker_log, stderr=worker_log, start_new_session=True) for _ in range(args.campfire_workers)]
                        try:
                            wait_for_server(camp_port, camp)
                            for worker in workers:
                                wait_for_worker(redis_port, worker)
                            cookie, csrf = login_campfire(camp_port)
                            process_pids = (camp.pid, redis.pid, *(worker.pid for worker in workers)) if args.resources else ()
                            return captured_trial(process_pids, "campfire", camp_port, camp_db, cookie, csrf, args.posts, args.rate, args.timeout, args.sockets, redis_port)
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
    assert rust_signatures == camp_signatures, next(((left, right) for left, right in zip(rust_signatures, camp_signatures) if left != right), None)
    report = {"source_revision": REVISION, "posts": args.posts, "rate": args.rate, "reply_delay_ms": args.delay_ms, "campfire_resque_workers": args.campfire_workers, "campfire_first": args.campfire_first, "sockets": args.sockets, "payloads_match": True, "rustfire": rust_report, "campfire": camp_report}
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    main()
