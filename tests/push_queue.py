"""Verify a burst above 50 eligible push subscriptions is fully scheduled."""

import http.client
import os
import pathlib
import sqlite3
import subprocess
import tempfile
import time
import urllib.parse
from datetime import datetime, timezone

from sys import path as python_path

ROOT = pathlib.Path(__file__).resolve().parents[1]
python_path.insert(0, str(ROOT / "bench"))
from direct_lookup import free_port, stop_server  # noqa: E402
from paired_direct_lookup import seed_rustfire, wait_for_server  # noqa: E402


SUBSCRIPTIONS = 200


def main():
    with tempfile.TemporaryDirectory(prefix="rustfire-push-queue-") as scratch:
        temp = pathlib.Path(scratch)
        database = temp / "rustfire.sqlite3"
        port = free_port()
        seed_rustfire(database, port, [])
        stamp = "2026-01-01T00:00:00Z"
        with sqlite3.connect(database) as db:
            db.execute("UPDATE memberships SET involvement='everything' WHERE room_id=1 AND user_id=2")
            db.execute("UPDATE memberships SET involvement='everything' WHERE room_id=1 AND user_id=1")
            db.executemany(
                "INSERT INTO memberships(room_id,user_id,involvement,created_at) VALUES(1,?1,?2,?3)",
                [(3, "mentions", stamp), (4, "invisible", stamp), (5, "everything", stamp)],
            )
            db.executemany(
                "INSERT INTO push_subscriptions(user_id,endpoint,p256dh_key,auth_key,created_at,updated_at) VALUES(2,?1,'invalid','invalid',?2,?2)",
                [(f"https://fcm.googleapis.com/push/{index}", stamp) for index in range(SUBSCRIPTIONS)],
            )
            db.executemany(
                "INSERT INTO push_subscriptions(user_id,endpoint,p256dh_key,auth_key,created_at,updated_at) VALUES(?1,?2,'invalid','invalid',?3,?3)",
                [(uid, f"https://fcm.googleapis.com/other/{uid}", stamp) for uid in (1, 3, 4, 5)],
            )
        env = dict(os.environ, RUSTFIRE_DB=str(database), RUSTFIRE_ADDR=f"127.0.0.1:{port}", RUSTFIRE_DISABLE_PUSH="0", RUSTFIRE_DISABLE_WEBHOOKS="1")
        with open(temp / "server.log", "w+") as log:
            server = subprocess.Popen([str(ROOT / "target/release/rustfire")], cwd=ROOT, env=env, stdout=log, stderr=log)
            try:
                wait_for_server(port, server)
                with sqlite3.connect(database) as db:
                    connected_at = datetime.now(timezone.utc).isoformat()
                    db.execute("UPDATE memberships SET connections=1,connected_at=? WHERE room_id=1 AND user_id=5", [connected_at])
                body = urllib.parse.urlencode({"message[body]": "Push scheduling check", "message[client_message_id]": "push-queue-check"})
                connection = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
                try:
                    connection.request("POST", "/rooms/1/messages", body, {
                        "Cookie": "session_token=benchmark-session",
                        "X-CSRF-Token": "benchmark-csrf",
                        "Content-Type": "application/x-www-form-urlencoded",
                        "Accept": "text/vnd.turbo-stream.html, text/html",
                    })
                    response = connection.getresponse()
                    payload = response.read()
                    assert response.status == 200, (response.status, payload[:200])
                finally:
                    connection.close()
                deadline = time.monotonic() + 10
                count = 0
                while time.monotonic() < deadline:
                    log.flush()
                    count = (temp / "server.log").read_text().count("Rustfire push delivery error: invalid subscription")
                    if count >= SUBSCRIPTIONS:
                        break
                    time.sleep(0.05)
                assert count == SUBSCRIPTIONS, (count, SUBSCRIPTIONS)
                time.sleep(0.1)
                log.flush()
                assert (temp / "server.log").read_text().count("Rustfire push delivery error: invalid subscription") == SUBSCRIPTIONS
            finally:
                stop_server(server)
    print(f"PASS {SUBSCRIPTIONS} eligible push deliveries were scheduled despite 50 active slots; author, unmentioned, invisible, and connected subscriptions were excluded")


if __name__ == "__main__":
    main()
