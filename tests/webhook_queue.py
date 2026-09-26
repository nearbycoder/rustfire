"""Exercise a webhook burst and recovery of pending and expired claims."""

import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import pathlib
import queue
import sqlite3
import sys
import tempfile
import threading
import time
import urllib.parse

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "bench"))
from direct_lookup import free_port, start_server, stop_server
from paired_direct_lookup import seed_rustfire


class Receiver(BaseHTTPRequestHandler):
    received = queue.Queue()
    reply_for = set()

    def do_POST(self):
        self.rfile.read(int(self.headers["Content-Length"]))
        time.sleep(.35)
        if self.path in self.reply_for:
            body = b"Reply from deactivated bot"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(204)
            self.end_headers()
        self.received.put(self.path)

    def log_message(self, *_):
        pass


def post(port):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
    body = urllib.parse.urlencode({
        "message[body]": "<div>Webhook burst</div>",
        "message[client_message_id]": "webhook-burst",
        "authenticity_token": "benchmark-csrf",
    })
    try:
        connection.request("POST", "/rooms/2/messages", body, {
            "Cookie": "session_token=benchmark-session",
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "text/vnd.turbo-stream.html, text/html",
        })
        response = connection.getresponse()
        result = response.read()
        assert response.status == 200, (response.status, result[:300])
    finally:
        connection.close()


def receive(count, timeout=15):
    return [Receiver.received.get(timeout=timeout) for _ in range(count)]


def wait_jobs_empty(database):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        with sqlite3.connect(database) as db:
            if db.execute("SELECT COUNT(*) FROM webhook_jobs").fetchone()[0] == 0:
                return
        time.sleep(.05)
    raise AssertionError("completed webhook jobs remain queued")


def main():
    with tempfile.TemporaryDirectory(prefix="rustfire-webhook-queue-") as scratch:
        database = pathlib.Path(scratch) / "rustfire.sqlite3"
        port, receiver_port = free_port(), free_port()
        bot_ids = range(52, 132)
        seed_rustfire(database, port, [])
        stamp = "2026-01-01T00:00:00Z"
        with sqlite3.connect(database) as db:
            db.executemany(
                "INSERT INTO users(id,name,role,status,bot_token,created_at,updated_at) VALUES(?1,?2,2,0,?3,?4,?4)",
                ((bot_id, f"Bot {bot_id}", f"token{bot_id}", stamp) for bot_id in bot_ids),
            )
            db.execute("INSERT INTO rooms(id,name,type,creator_id,created_at,updated_at) VALUES(2,NULL,'Rooms::Direct',1,?1,?1)", (stamp,))
            db.executemany(
                "INSERT INTO memberships(room_id,user_id,involvement,created_at) VALUES(2,?1,'everything',?2)",
                ((user_id, stamp) for user_id in (1, *bot_ids)),
            )
            db.executemany(
                "INSERT INTO webhooks(user_id,url) VALUES(?1,?2)",
                ((bot_id, f"http://127.0.0.1:{receiver_port}/hook/{bot_id}") for bot_id in bot_ids),
            )
        receiver = ThreadingHTTPServer(("127.0.0.1", receiver_port), Receiver)
        thread = threading.Thread(target=receiver.serve_forever, daemon=True)
        thread.start()
        try:
            process = start_server(database, port, {"RUSTFIRE_DISABLE_WEBHOOKS": "0"})
            try:
                post(port)
                paths = receive(len(bot_ids))
                assert set(paths) == {f"/hook/{bot_id}" for bot_id in bot_ids}, paths
                wait_jobs_empty(database)
            finally:
                stop_server(process)

            with sqlite3.connect(database) as db:
                message_id = db.execute("SELECT id FROM messages WHERE client_message_id='webhook-burst'").fetchone()[0]
                db.execute("INSERT INTO webhook_jobs(bot_id,message_id,created_at) VALUES(52,?1,?2)", (message_id, stamp))
                db.execute("INSERT INTO webhook_jobs(bot_id,message_id,created_at,claimed_at) VALUES(53,?1,?2,unixepoch()-31)", (message_id, stamp))
                db.execute("UPDATE users SET status=1 WHERE id=52")
            Receiver.reply_for = {"/hook/52"}
            process = start_server(database, port, {"RUSTFIRE_DISABLE_WEBHOOKS": "0"})
            try:
                assert set(receive(2)) == {"/hook/52", "/hook/53"}
                wait_jobs_empty(database)
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    with sqlite3.connect(database) as db:
                        reply = db.execute("SELECT body FROM messages WHERE creator_id=52 ORDER BY id DESC LIMIT 1").fetchone()
                        if reply == ("Reply from deactivated bot",):
                            break
                    time.sleep(.05)
                else:
                    raise AssertionError("deactivated bot reply was not saved")
            finally:
                stop_server(process)
        finally:
            receiver.shutdown()
            receiver.server_close()
    print("PASS all 80 burst deliveries retained; pending and expired jobs resume, including a deactivated bot reply")


if __name__ == "__main__":
    main()
