"""Compare bot-mention webhook triggers and payloads with pinned Campfire."""

import argparse
import html
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
from paired_banned_content import BUNDLE, REPOSITORY, REVISION, RUBY, isolated_campfire, start_redis, wait_for_worker
from paired_direct_lookup import login_campfire, seed_campfire, seed_rustfire, wait_for_server


BOT_ID = 52
BOT_TOKEN = "pairedBot123"
PEER_ID = 53
PEER_TOKEN = "peerBot12345"
CASES = ("plain", "signed", "duplicate", "trix-figure", "direct")


class Receiver(BaseHTTPRequestHandler):
    received = queue.Queue()

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        self.received.put((self.path, json.loads(body)))
        self.send_response(204)
        self.end_headers()

    def log_message(self, *_):
        pass


def seed_bot(database, campfire, url, with_webhooks=True):
    stamp = "2026-01-01 00:00:00.000000" if campfire else "2026-01-01T00:00:00Z"
    with sqlite3.connect(database) as db:
        if campfire:
            db.execute("DELETE FROM sqlite_sequence WHERE name='messages'")
        db.execute("INSERT INTO users(id,name,bio,role,status,bot_token,created_at,updated_at) VALUES(52,'Probe Bot','Team / Ops & Co',2,0,?1,?2,?2)", (BOT_TOKEN, stamp))
        db.execute("INSERT INTO users(id,name,role,status,bot_token,created_at,updated_at) VALUES(53,'Peer Bot',2,0,?1,?2,?2)", (PEER_TOKEN, stamp))
        db.execute("INSERT INTO rooms(id,name,type,creator_id,created_at,updated_at) VALUES(2,NULL,'Rooms::Direct',1,?1,?1)", (stamp,))
        db.execute("INSERT INTO rooms(id,name,type,creator_id,created_at,updated_at) VALUES(3,NULL,'Rooms::Direct',52,?1,?1)", (stamp,))
        if campfire:
            db.execute("INSERT INTO memberships(room_id,user_id,involvement,created_at,updated_at) VALUES(1,52,'mentions',?1,?1)", (stamp,))
            db.execute("INSERT INTO memberships(room_id,user_id,involvement,created_at,updated_at) VALUES(1,53,'mentions',?1,?1)", (stamp,))
            db.executemany("INSERT INTO memberships(room_id,user_id,involvement,created_at,updated_at) VALUES(2,?1,'everything',?2,?2)", [(1, stamp), (BOT_ID, stamp)])
            db.executemany("INSERT INTO memberships(room_id,user_id,involvement,created_at,updated_at) VALUES(3,?1,'everything',?2,?2)", [(BOT_ID, stamp), (PEER_ID, stamp)])
            if with_webhooks:
                db.execute("INSERT INTO webhooks(user_id,url,created_at,updated_at) VALUES(52,?1,?2,?2)", (url, stamp))
                db.execute("INSERT INTO webhooks(user_id,url,created_at,updated_at) VALUES(53,?1,?2,?2)", (url.replace("/hook", "/peer"), stamp))
        else:
            db.execute("INSERT INTO memberships(room_id,user_id,involvement,created_at) VALUES(1,52,'mentions',?1)", (stamp,))
            db.execute("INSERT INTO memberships(room_id,user_id,involvement,created_at) VALUES(1,53,'mentions',?1)", (stamp,))
            db.executemany("INSERT INTO memberships(room_id,user_id,involvement,created_at) VALUES(2,?1,'everything',?2)", [(1, stamp), (BOT_ID, stamp)])
            db.executemany("INSERT INTO memberships(room_id,user_id,involvement,created_at) VALUES(3,?1,'everything',?2)", [(BOT_ID, stamp), (PEER_ID, stamp)])
            if with_webhooks:
                db.execute("INSERT INTO webhooks(user_id,url) VALUES(52,?1)", (url,))
                db.execute("INSERT INTO webhooks(user_id,url) VALUES(53,?1)", (url.replace("/hook", "/peer"),))


def bot_sgid(port, cookie, campfire, bot_id=BOT_ID):
    name = "Probe%20Bot" if bot_id == BOT_ID else "Peer%20Bot"
    path = f"/autocompletable/users.json?query={name}" if campfire else f"/autocompletable/users?query={name}"
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
    try:
        connection.request("GET", path, headers={"Cookie": cookie, "Accept": "application/json"})
        response = connection.getresponse()
        body = response.read()
        assert response.status == 200, (response.status, body[:300])
        return next(user["sgid"] for user in json.loads(body) if user["value"] == bot_id)
    finally:
        connection.close()


def post(port, cookie, csrf, sgid, case):
    attachment = f'<action-text-attachment sgid="{html.escape(sgid, quote=True)}" content-type="application/vnd.campfire.mention"></action-text-attachment>'
    if case == "direct":
        message = "<div>Direct ping</div>"
    elif case == "plain":
        message = "<div>Hello @Probe Bot!</div>"
    elif case == "trix-figure":
        content = '<span class="mention">Probe &amp; Bot</span>'
        data = html.escape(json.dumps({"contentType": "application/vnd.campfire.mention", "sgid": sgid, "content": content}), quote=True)
        message = f'<div>Hello <figure data-trix-attachment="{data}">{content}</figure>!</div>'
    else:
        message = f"<div>Hello {attachment}{' ' + attachment if case == 'duplicate' else ''}!</div>"
    body = urllib.parse.urlencode({"message[body]": message, "message[client_message_id]": f"paired-webhook-{case}", "authenticity_token": csrf})
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
    try:
        room_id = 2 if case == "direct" else 1
        connection.request("POST", f"/rooms/{room_id}/messages", body, {"Cookie": cookie, "Content-Type": "application/x-www-form-urlencoded", "Accept": "text/vnd.turbo-stream.html, text/html"})
        response = connection.getresponse()
        result = response.read()
        assert response.status == 200, (response.status, result[:300])
    finally:
        connection.close()


def bot_post(port, room_id, body):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
    try:
        connection.request("POST", f"/rooms/{room_id}/{BOT_ID}-{BOT_TOKEN}/messages", body, {"Content-Type": "text/plain"})
        response = connection.getresponse()
        payload = response.read()
        assert response.status == 201, (response.status, payload[:300])
    finally:
        connection.close()


def bot_index(port, room_id):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
    try:
        connection.request("GET", f"/rooms/{room_id}/{BOT_ID}-{BOT_TOKEN}/messages", headers={"Accept": "application/json"})
        response = connection.getresponse()
        payload = response.read()
        assert response.status == 200, (response.status, payload[:300])
        messages = json.loads(payload)
        for message in messages:
            message.pop("created_at")
            for obj, key in ((message, "url"), (message["creator"], "avatar_url")):
                url = urllib.parse.urlsplit(obj[key])
                obj[key] = url.path + ("?" + url.query if url.query else "")
        return messages
    finally:
        connection.close()


def workflow(port, cookie, csrf, campfire):
    sgid = bot_sgid(port, cookie, campfire)
    peer_sgid = bot_sgid(port, cookie, campfire, PEER_ID)
    for case in CASES:
        post(port, cookie, csrf, sgid, case)
    bot_post(port, 2, "Talking to myself")
    bot_post(port, 3, "Hello peer")
    own = f'<action-text-attachment sgid="{html.escape(sgid, quote=True)}" content-type="application/vnd.campfire.mention"></action-text-attachment>'
    peer = f'<action-text-attachment sgid="{html.escape(peer_sgid, quote=True)}" content-type="application/vnd.campfire.mention"></action-text-attachment>'
    bot_post(port, 1, f"<div>Hey {own} {peer}</div>")
    received = [Receiver.received.get(timeout=15) for _ in range(6)]
    time.sleep(0.5)
    assert Receiver.received.empty(), "unexpected extra bot webhook"
    return (sgid, peer_sgid), sorted(received, key=lambda item: item[1]["message"]["id"]), {room_id: bot_index(port, room_id) for room_id in (1, 2, 3)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sample-dir", type=pathlib.Path, help="save normalized bot message JSON from both apps")
    args = parser.parse_args()
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPOSITORY, text=True).strip() == REVISION
    receiver_port = free_port()
    receiver = ThreadingHTTPServer(("127.0.0.1", receiver_port), Receiver)
    thread = threading.Thread(target=receiver.serve_forever, daemon=True)
    thread.start()
    try:
        with tempfile.TemporaryDirectory(prefix="paired-mention-webhook-") as scratch:
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
                rust = start_server(rust_db, rust_port, {"RUSTFIRE_CAMPFIRE_SECRET_KEY_BASE": camp_env["SECRET_KEY_BASE"], "RUSTFIRE_DISABLE_WEBHOOKS": "0"})
                try:
                    rust_sgid, rust_received, rust_bot_messages = workflow(rust_port, "session_token=benchmark-session", "benchmark-csrf", False)
                finally:
                    stop_server(rust)
                with open(temp / "puma.log", "w+") as puma_log, open(temp / "worker.log", "w+") as worker_log:
                    camp = subprocess.Popen([str(RUBY), str(RUBY.parent / "bundle"), "exec", "puma", "-C", "config/puma.rb"], cwd=checkout, env=camp_env, stdout=puma_log, stderr=puma_log)
                    worker = subprocess.Popen([str(RUBY), str(RUBY.parent / "bundle"), "exec", "rake", "resque:work"], cwd=checkout, env=camp_env, stdout=worker_log, stderr=worker_log, start_new_session=True)
                    try:
                        wait_for_server(camp_port, camp)
                        wait_for_worker(redis_port, worker)
                        cookie, csrf = login_campfire(camp_port)
                        camp_sgid, camp_received, camp_bot_messages = workflow(camp_port, cookie, csrf, True)
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
    if args.sample_dir:
        args.sample_dir.mkdir(parents=True, exist_ok=True)
        (args.sample_dir / "rustfire-bot-messages.json").write_text(json.dumps(rust_bot_messages, indent=2, ensure_ascii=False))
        (args.sample_dir / "campfire-bot-messages.json").write_text(json.dumps(camp_bot_messages, indent=2, ensure_ascii=False))
    assert rust_sgid == camp_sgid, (rust_sgid, camp_sgid)
    assert rust_received == camp_received, (rust_received, camp_received)
    assert rust_bot_messages == camp_bot_messages, (rust_bot_messages, camp_bot_messages)
    assert [message["id"] for message in rust_bot_messages[3]] == [7], rust_bot_messages[3]
    assert rust_bot_messages[1][-1]["id"] == 8, rust_bot_messages[1]
    assert rust_bot_messages[1][-1]["body"]["plain_text"] == "Hey @Probe Bot @Peer Bot", rust_bot_messages[1][-1]
    assert [payload["message"]["id"] for path, payload in rust_received] == [2, 3, 4, 5, 7, 8]
    assert [path for path, _ in rust_received] == ["/hook"] * 4 + ["/peer"] * 2
    print("PASS mention, direct-room, and bot-originated webhook payloads and full bot API JSON match pinned Campfire; plain @bot and bot self-messages trigger none")


if __name__ == "__main__":
    main()
