"""Compare webhook timeout and refused-connection behavior with Campfire."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
import pathlib
import queue
import signal
import sqlite3
import subprocess
import tempfile
import threading
import time

from direct_lookup import free_port, start_server, stop_server
from paired_banned_content import BUNDLE, REDIS_CLI, REPOSITORY, REVISION, RUBY, isolated_campfire, start_redis, wait_for_worker
from paired_direct_lookup import login_campfire, seed_campfire, seed_rustfire, wait_for_server
from paired_mention_webhook import bot_index, bot_sgid, seed_bot
from paired_webhook_replies import bot_reply_rows, post_trigger


class SlowReceiver(BaseHTTPRequestHandler):
    received = queue.Queue()

    def do_POST(self):
        import json
        payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.received.put(payload)
        time.sleep(8.2)
        try:
            self.send_response(204)
            self.end_headers()
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, *_):
        pass


def wait_for(predicate, label, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(.05)
    raise AssertionError(f"Timed out waiting for {label}")


def failed_jobs(redis_port):
    result = subprocess.run([str(REDIS_CLI), "-p", str(redis_port), "LLEN", "resque:failed"], check=True, capture_output=True, text=True)
    return int(result.stdout.strip())


def rust_job_counts(database):
    with sqlite3.connect(database) as db:
        queued = db.execute("SELECT COUNT(*) FROM webhook_jobs").fetchone()[0]
        failed = db.execute("SELECT bot_id,message_id,error FROM failed_webhook_jobs").fetchall()
    return queued, failed


def workflow(port, cookie, csrf, database, campfire, refused_port, redis_port=None):
    sgid = bot_sgid(port, cookie, campfire)
    post_trigger(port, cookie, csrf, sgid, "timeout")
    timeout_payload = SlowReceiver.received.get(timeout=15)
    wait_for(lambda: len(bot_reply_rows(database, campfire)) == 1, "timeout reply")
    timeout_messages = bot_index(port, 1)
    with sqlite3.connect(database) as db:
        db.execute("UPDATE webhooks SET url=?1 WHERE user_id=52", (f"http://127.0.0.1:{refused_port}/refused",))
    post_trigger(port, cookie, csrf, sgid, "refused")
    if campfire:
        wait_for(lambda: failed_jobs(redis_port) == 1, "failed Resque job")
        failure_count = failed_jobs(redis_port)
    else:
        wait_for(lambda: rust_job_counts(database)[0] == 0, "webhook queue drain")
        queued, failed = rust_job_counts(database)
        assert queued == 0 and len(failed) == 1 and failed[0][:2] == (52, 3), (queued, failed)
        assert failed[0][2].startswith("Webhook request failed:"), failed
        failure_count = len(failed)
    assert len(bot_reply_rows(database, campfire)) == 1
    return timeout_payload, timeout_messages, bot_index(port, 1), failure_count


def main():
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPOSITORY, text=True).strip() == REVISION
    receiver_port, refused_port = free_port(), free_port()
    receiver = ThreadingHTTPServer(("127.0.0.1", receiver_port), SlowReceiver)
    threading.Thread(target=receiver.serve_forever, daemon=True).start()
    try:
        with tempfile.TemporaryDirectory(prefix="paired-webhook-failures-") as scratch:
            temp = pathlib.Path(scratch)
            rust_db, camp_db = temp / "rust.sqlite3", temp / "camp.sqlite3"
            rust_port, camp_port, redis_port = free_port(), free_port(), free_port()
            seed_rustfire(rust_db, rust_port, [])
            camp_env = seed_campfire(REPOSITORY, RUBY, BUNDLE, REPOSITORY / "storage/db/production.sqlite3", camp_db, [], camp_port, temp)
            checkout = isolated_campfire(temp, redis_port)
            camp_env.update({"REDIS_URL": f"redis://127.0.0.1:{redis_port}", "QUEUE": "default", "INTERVAL": "0.25"})
            url = f"http://127.0.0.1:{receiver_port}/slow"
            seed_bot(rust_db, False, url)
            seed_bot(camp_db, True, url)
            with sqlite3.connect(camp_db) as camp, sqlite3.connect(rust_db) as rust:
                rust.executemany("UPDATE users SET name=?2,updated_at=?3 WHERE id=?1", camp.execute("SELECT id,name,updated_at FROM users WHERE id IN(1,2)").fetchall())
                rust.execute("UPDATE rooms SET name=? WHERE id=1", [camp.execute("SELECT name FROM rooms WHERE id=1").fetchone()[0]])
            redis, redis_log = start_redis(temp, redis_port)
            try:
                rust = start_server(rust_db, rust_port, {"RUSTFIRE_CAMPFIRE_SECRET_KEY_BASE": camp_env["SECRET_KEY_BASE"], "RUSTFIRE_DISABLE_WEBHOOKS": "0"})
                try:
                    rust_result = workflow(rust_port, "session_token=benchmark-session", "benchmark-csrf", rust_db, False, refused_port)
                finally:
                    stop_server(rust)
                with open(temp / "puma.log", "w+") as puma_log, open(temp / "worker.log", "w+") as worker_log:
                    camp = subprocess.Popen([str(RUBY), str(RUBY.parent / "bundle"), "exec", "puma", "-C", "config/puma.rb"], cwd=checkout, env=camp_env, stdout=puma_log, stderr=puma_log)
                    worker = subprocess.Popen([str(RUBY), str(RUBY.parent / "bundle"), "exec", "rake", "resque:work"], cwd=checkout, env=camp_env, stdout=worker_log, stderr=worker_log, start_new_session=True)
                    try:
                        wait_for_server(camp_port, camp)
                        wait_for_worker(redis_port, worker)
                        cookie, csrf = login_campfire(camp_port)
                        camp_result = workflow(camp_port, cookie, csrf, camp_db, True, refused_port, redis_port)
                    finally:
                        stop_server(camp)
                        if worker.poll() is None:
                            os.killpg(worker.pid, signal.SIGTERM)
                        worker.wait(timeout=10)
            finally:
                redis.terminate()
                redis.wait(timeout=10)
                redis_log.close()
    finally:
        receiver.shutdown()
        receiver.server_close()
    assert rust_result == camp_result, (rust_result, camp_result)
    assert rust_result[1][-1]["body"]["plain_text"] == "Failed to respond within 7 seconds"
    print("PASS timeout reply and refused-connection state match Campfire; both retain one failed job record")


if __name__ == "__main__":
    main()
