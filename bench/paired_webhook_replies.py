"""Compare Campfire and Rustfire webhook text, HTML, blank, and file replies."""

import base64
import html
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import pathlib
import queue
import sqlite3
import subprocess
import tempfile
import threading
import time
import urllib.parse
import os
import signal

from direct_lookup import free_port, start_server, stop_server
from paired_banned_content import BUNDLE, REPOSITORY, REVISION, RUBY, isolated_campfire, start_redis, wait_for_worker
from paired_direct_lookup import login_campfire, seed_campfire, seed_rustfire, wait_for_server
from paired_mention_webhook import BOT_ID, bot_index, bot_sgid, seed_bot


PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/lXcAAAAASUVORK5CYII=")
LARGE_REPLY = b"rustfire-webhook-file\n" * ((26 * 1024 * 1024 // 22) + 1)
REPLIES = {
    "text": (200, "text/plain", b"Hello back!"),
    "html": (200, "text/html", b"<strong>Bold reply</strong>"),
    "slow_text": (200, "text/plain", b"slow stream reply"),
    "stalled_text": (200, "text/plain", b"late reply"),
    "stalled_file": (200, "application/zip", b"partial-and-never-completes"),
    "empty": (200, "text/plain", b""),
    "error_text": (500, "text/plain", b"Error body"),
    "image": (200, "image/png", PNG),
    "large_zip": (200, "application/zip", LARGE_REPLY),
    "octet_stream": (200, "application/octet-stream", b"generic binary reply"),
    "unknown_type": (200, "application/x-rustfire-test", b"unregistered mime reply"),
    "js_alias": (200, "application/javascript", b"console.log('hello')"),
    "aac_alias": (200, "audio/mp4", b"sample audio alias"),
    "html_alias": (200, "application/xhtml+xml", b"<p>sample HTML alias</p>"),
    "error_no_type": (500, None, b"Error body"),
}


class Receiver(BaseHTTPRequestHandler):
    received = queue.Queue()

    def do_POST(self):
        import json
        payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        case = payload["message"]["body"]["plain"].strip().removeprefix("case:")
        status, kind, data = REPLIES[case]
        self.send_response(status)
        if kind is not None:
            self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if case in ("stalled_text", "stalled_file"):
            self.received.put((case, payload))
            if case == "stalled_file":
                self.wfile.write(data[:7])
                self.wfile.flush()
            time.sleep(8)
            try:
                self.wfile.write(data if case == "stalled_text" else data[7:])
            except (BrokenPipeError, ConnectionResetError):
                pass
            return
        if case == "slow_text":
            for index, chunk in enumerate((b"slow ", b"stream ", b"reply")):
                if index:
                    time.sleep(4)
                self.wfile.write(chunk)
                self.wfile.flush()
        elif data:
            self.wfile.write(data)
        self.received.put((case, payload))

    def log_message(self, *_):
        pass


def post_trigger(port, cookie, csrf, sgid, case):
    attachment = f'<action-text-attachment sgid="{html.escape(sgid, quote=True)}" content-type="application/vnd.campfire.mention"></action-text-attachment>'
    body = urllib.parse.urlencode({
        "message[body]": f"<div>{attachment} case:{case}</div>",
        "message[client_message_id]": f"paired-reply-{case}",
        "authenticity_token": csrf,
    })
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
    try:
        connection.request("POST", "/rooms/1/messages", body, {"Cookie": cookie, "Content-Type": "application/x-www-form-urlencoded", "Accept": "text/vnd.turbo-stream.html, text/html"})
        response = connection.getresponse()
        payload = response.read()
        assert response.status == 200, (response.status, payload[:300])
    finally:
        connection.close()


def bot_reply_rows(database, campfire):
    with sqlite3.connect(database) as db:
        if campfire:
            return db.execute("SELECT m.id,b.filename,b.content_type,b.key FROM messages m LEFT JOIN active_storage_attachments a ON a.record_type='Message' AND a.record_id=m.id AND a.name='attachment' LEFT JOIN active_storage_blobs b ON b.id=a.blob_id WHERE m.creator_id=52 ORDER BY m.id").fetchall()
        return db.execute("SELECT m.id,a.filename,a.content_type,a.stored_name FROM messages m LEFT JOIN attachments a ON a.message_id=m.id WHERE m.creator_id=52 ORDER BY m.id").fetchall()


def original_bytes(root, campfire, stored):
    if campfire:
        return (root / "storage/files" / stored[:2] / stored[2:4] / stored).read_bytes()
    return (root / stored).read_bytes()


def workflow(port, cookie, csrf, database, campfire, storage_root):
    sgid = bot_sgid(port, cookie, campfire)
    records = []
    expected_replies = 0
    for case in REPLIES:
        post_trigger(port, cookie, csrf, sgid, case)
        actual_case, payload = Receiver.received.get(timeout=15)
        assert actual_case == case, (case, actual_case)
        records.append(payload)
        if case != "error_no_type":
            expected_replies += 1
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                if len(bot_reply_rows(database, campfire)) >= expected_replies:
                    break
                time.sleep(0.05)
            else:
                raise AssertionError((case, bot_reply_rows(database, campfire)))
        else:
            time.sleep(0.5)
            assert len(bot_reply_rows(database, campfire)) == expected_replies
    rows = bot_reply_rows(database, campfire)
    files = [(row[1], row[2], original_bytes(storage_root, campfire, row[3])) for row in rows if row[3]]
    return sgid, records, rows, files, bot_index(port, 1)


def main():
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPOSITORY, text=True).strip() == REVISION
    receiver_port = free_port()
    receiver = ThreadingHTTPServer(("127.0.0.1", receiver_port), Receiver)
    thread = threading.Thread(target=receiver.serve_forever, daemon=True)
    thread.start()
    try:
        with tempfile.TemporaryDirectory(prefix="paired-webhook-replies-") as scratch:
            temp = pathlib.Path(scratch)
            rust_db, camp_db = temp / "rust.sqlite3", temp / "camp.sqlite3"
            rust_port, camp_port, redis_port = free_port(), free_port(), free_port()
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
                rust_uploads = temp / "rust-uploads"
                rust = start_server(rust_db, rust_port, {"RUSTFIRE_CAMPFIRE_SECRET_KEY_BASE": camp_env["SECRET_KEY_BASE"], "RUSTFIRE_DISABLE_WEBHOOKS": "0", "RUSTFIRE_UPLOAD_DIR": str(rust_uploads)})
                try:
                    rust_result = workflow(rust_port, "session_token=benchmark-session", "benchmark-csrf", rust_db, False, rust_uploads)
                    assert not list(rust_uploads.glob("webhook-reply-*")), "stalled attachment left a temporary file"
                finally:
                    stop_server(rust)
                with open(temp / "puma.log", "w+") as puma_log, open(temp / "worker.log", "w+") as worker_log:
                    camp = subprocess.Popen([str(RUBY), str(RUBY.parent / "bundle"), "exec", "puma", "-C", "config/puma.rb"], cwd=checkout, env=camp_env, stdout=puma_log, stderr=puma_log)
                    worker = subprocess.Popen([str(RUBY), str(RUBY.parent / "bundle"), "exec", "rake", "resque:work"], cwd=checkout, env=camp_env, stdout=worker_log, stderr=worker_log, start_new_session=True)
                    try:
                        wait_for_server(camp_port, camp)
                        wait_for_worker(redis_port, worker)
                        cookie, csrf = login_campfire(camp_port)
                        camp_result = workflow(camp_port, cookie, csrf, camp_db, True, checkout)
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
    rust_sgid, rust_payloads, rust_rows, rust_files, rust_messages = rust_result
    camp_sgid, camp_payloads, camp_rows, camp_files, camp_messages = camp_result
    assert rust_sgid == camp_sgid
    assert rust_payloads == camp_payloads, (rust_payloads, camp_payloads)
    assert [(row[0], row[1], row[2]) for row in rust_rows] == [(row[0], row[1], row[2]) for row in camp_rows], (rust_rows, camp_rows)
    assert rust_files == camp_files, (rust_files, camp_files)
    assert rust_messages == camp_messages, next(((left, right) for left, right in zip(rust_messages, camp_messages) if left != right), None)
    assert len(rust_rows) == 14 and len(rust_messages) == 29
    print("PASS paired webhook replies: text, HTML, streamed replies, timeouts, blank text, non-200 text attachment, PNG and 26 MiB ZIP bytes, generic, unregistered, and aliased MIME attachments, and no-content-type error match pinned Campfire")


if __name__ == "__main__":
    main()
