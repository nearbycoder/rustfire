"""Compare push-subscription cleanup after invalid encryption keys.

Run after cargo build --release. Both apps use disposable databases. The bad
P-256 point fails locally before any push HTTP request leaves the process.
"""

import base64
import http.client
import pathlib
import sqlite3
import subprocess
import tempfile
import urllib.parse

from direct_lookup import free_port, start_server, stop_server
from paired_direct_lookup import seed_campfire, seed_rustfire
from paired_push_subscriptions_page import BUNDLE, REPOSITORY, REVISION, RUBY


ENDPOINT = "https://fcm.googleapis.com/fcm/send/invalid-point"
STAMP = "2026-01-01 00:00:00"
INVALID_POINT = base64.urlsafe_b64encode(b"\x04" + bytes(64)).rstrip(b"=").decode()
AUTH = base64.urlsafe_b64encode(bytes(16)).rstrip(b"=").decode()
SOURCE = """require 'timeout'
class Push::Subscription
  def resolved_endpoint_ip; '1.1.1.1'; end
end
pool = Rails.configuration.x.web_push_pool
pool.queue({ title: 'Test', body: 'Body', path: '/rooms/1' }, Push::Subscription.where(id: [1, 2]))
Timeout.timeout(10) do
  sleep 0.02 until pool.delivery_pool.completed_task_count >= 2 && pool.invalidation_pool.completed_task_count >= 1
end
puts 'POOL_DONE'
"""


def seed_subscriptions(database):
    with sqlite3.connect(database) as db:
        db.execute("DELETE FROM push_subscriptions")
        db.executemany(
            "INSERT INTO push_subscriptions(id,user_id,endpoint,p256dh_key,auth_key,created_at,updated_at) VALUES(?1,1,?2,?3,?4,?5,?5)",
            [(1, ENDPOINT, INVALID_POINT, AUTH, STAMP), (2, ENDPOINT, "not-base64", "auth", STAMP)],
        )


def ids(database):
    with sqlite3.connect(database) as db:
        return [row[0] for row in db.execute("SELECT id FROM push_subscriptions ORDER BY id")]


def test_notification(port, subscription_id):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
    try:
        connection.request(
            "POST",
            f"/users/me/push_subscriptions/{subscription_id}/test_notifications",
            urllib.parse.urlencode({"authenticity_token": "benchmark-csrf"}),
            {
                "Cookie": "session_token=benchmark-session",
                "X-CSRF-Token": "benchmark-csrf",
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        response = connection.getresponse()
        body = response.read()
        assert response.status == 502, (subscription_id, response.status, body[:200])
    finally:
        connection.close()


def main():
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPOSITORY, text=True).strip() == REVISION
    with tempfile.TemporaryDirectory(prefix="paired-push-invalid-keys-") as scratch:
        temp = pathlib.Path(scratch)
        camp_db, rust_db = temp / "camp.sqlite3", temp / "rust.sqlite3"
        camp_env = seed_campfire(
            REPOSITORY, RUBY, BUNDLE, REPOSITORY / "storage/db/production.sqlite3",
            camp_db, [], free_port(), temp,
        )
        rust_port = free_port()
        seed_rustfire(rust_db, rust_port, [])
        for database in (camp_db, rust_db):
            seed_subscriptions(database)

        source = subprocess.run(
            [str(RUBY), str(RUBY.parent / "bundle"), "exec", "rails", "runner", SOURCE],
            cwd=REPOSITORY, env=camp_env, capture_output=True, text=True, timeout=30,
        )
        assert source.returncode == 0 and "POOL_DONE" in source.stdout, source.stdout + source.stderr
        assert ids(camp_db) == [2], ids(camp_db)

        rust = start_server(rust_db, rust_port, {"RUSTFIRE_DISABLE_PUSH": "0"})
        try:
            test_notification(rust_port, 1)
            test_notification(rust_port, 2)
        finally:
            stop_server(rust)
        assert ids(rust_db) == [2], ids(rust_db)
    print("PASS invalid P-256 push subscription removed; malformed base64 retained in both apps")


if __name__ == "__main__":
    main()
