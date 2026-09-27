"""Compare background and direct-test handling of invalid push encryption keys.

Run after cargo build --release. Both apps use disposable databases. The keys
fail during local encryption before any push HTTP request leaves a process.
"""

import base64
import hashlib
import http.client
import pathlib
import sqlite3
import subprocess
import tempfile
import time
import urllib.parse

from direct_lookup import free_port, start_server, stop_server
from paired_direct_lookup import login_campfire, seed_campfire, seed_rustfire, wait_for_server
from paired_push_subscriptions_page import BUNDLE, REPOSITORY, REVISION, RUBY
from paired_room_shell import post_message


ENDPOINT = "https://fcm.googleapis.com/fcm/send/invalid-point"
ROOT = pathlib.Path(__file__).resolve().parents[1]
STAMP = "2026-01-01 00:00:00"
INVALID_POINT = base64.urlsafe_b64encode(bytes([4]) + bytes(64)).rstrip(b"=").decode()
SHORT_POINT = base64.urlsafe_b64encode(bytes([4]) + bytes(10)).rstrip(b"=").decode()
AUTH = base64.urlsafe_b64encode(bytes(16)).rstrip(b"=").decode()
SOURCE_POOL = """require 'timeout'
require 'base64'
recipient = OpenSSL::PKey::EC.generate('prime256v1')
public_key = Base64.urlsafe_encode64(recipient.public_key.to_bn.to_s(2), padding: false)
[1, 16, 32].each do |length|
  auth = Base64.urlsafe_encode64('a' * length, padding: false)
  raise 'empty encrypted payload' if WebPush::Encryption.encrypt('sample', public_key, auth).empty?
end
compressed_key = Base64.urlsafe_encode64(recipient.public_key.to_octet_string(:compressed), padding: false)
raise 'compressed point failed' if WebPush::Encryption.encrypt('sample', compressed_key, Base64.urlsafe_encode64('a' * 16, padding: false)).empty?
normal_auth = Base64.urlsafe_encode64('a' * 16, padding: false)
raise 'large payload length differs' unless WebPush::Encryption.encrypt('x' * 4_078, public_key, normal_auth).bytesize == 4_182
begin
  WebPush::Encryption.encrypt('x' * 4_079, public_key, normal_auth)
  raise 'oversized payload accepted'
rescue ArgumentError => error
  raise unless error.message == 'encrypted payload is too big'
end
puts 'CRYPTO_OK'
class Push::Subscription
  def resolved_endpoint_ip; '1.1.1.1'; end
end
pool = Rails.configuration.x.web_push_pool
pool.queue({ title: 'Test', body: 'Body', path: '/rooms/1' }, Push::Subscription.where(id: [1, 2, 6, 8]))
Timeout.timeout(10) do
  sleep 0.02 until pool.delivery_pool.completed_task_count >= 4 && pool.invalidation_pool.completed_task_count >= 2
end
puts 'POOL_DONE'
"""


def seed_subscriptions(database, rustfire):
    with sqlite3.connect(database) as db:
        db.execute("DELETE FROM push_subscriptions")
        if rustfire:
            db.execute("UPDATE memberships SET involvement='everything',connected_at=NULL WHERE room_id=1 AND user_id=2")
        db.executemany(
            "INSERT INTO push_subscriptions(id,user_id,endpoint,p256dh_key,auth_key,created_at,updated_at) VALUES(?1,?2,?3,?4,?5,?6,?6)",
            [
                (1, 2, ENDPOINT, INVALID_POINT, AUTH, STAMP),
                (2, 2, ENDPOINT, "not-base64", "auth", STAMP),
                (3, 1, ENDPOINT, INVALID_POINT, AUTH, STAMP),
                (4, 1, ENDPOINT, "not-base64", "auth", STAMP),
                (5, 1, "https://attacker.example.invalid/steal", "not-base64", "auth", STAMP),
                (6, 2, ENDPOINT, SHORT_POINT, AUTH, STAMP),
                (7, 1, ENDPOINT, SHORT_POINT, AUTH, STAMP),
                (8, 2, ENDPOINT, INVALID_POINT, "", STAMP),
                (9, 1, ENDPOINT, INVALID_POINT, "", STAMP),
            ],
        )


def ids(database):
    with sqlite3.connect(database) as db:
        return [row[0] for row in db.execute("SELECT id FROM push_subscriptions ORDER BY id")]


def test_notification(port, cookie, csrf, subscription_id, expected_status):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
    try:
        connection.request(
            "POST",
            f"/users/me/push_subscriptions/{subscription_id}/test_notifications",
            urllib.parse.urlencode({"authenticity_token": csrf}),
            {
                "Cookie": cookie,
                "X-CSRF-Token": csrf,
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "text/html",
            },
        )
        response = connection.getresponse()
        body = response.read()
        assert response.status == expected_status, (subscription_id, response.status, body[:200])
        redirect = urllib.parse.urlsplit(response.getheader("Location") or "").path
        return response.status, response.headers.get_content_type(), redirect, hashlib.sha256(body).hexdigest()
    finally:
        connection.close()


def wait_for_ids(database, expected):
    for _ in range(100):
        if ids(database) == expected:
            return
        time.sleep(0.05)
    raise AssertionError((ids(database), expected))


def main():
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPOSITORY, text=True).strip() == REVISION
    with tempfile.TemporaryDirectory(prefix="paired-push-invalid-keys-") as scratch:
        temp = pathlib.Path(scratch)
        camp_db, rust_db = temp / "camp.sqlite3", temp / "rust.sqlite3"
        camp_port, rust_port = free_port(), free_port()
        camp_env = seed_campfire(
            REPOSITORY, RUBY, BUNDLE, REPOSITORY / "storage/db/production.sqlite3",
            camp_db, [], camp_port, temp,
        )
        seed_rustfire(rust_db, rust_port, [])
        seed_subscriptions(camp_db, False)
        seed_subscriptions(rust_db, True)

        source = subprocess.run(
            [str(RUBY), str(RUBY.parent / "bundle"), "exec", "rails", "runner", SOURCE_POOL],
            cwd=REPOSITORY, env=camp_env, capture_output=True, text=True, timeout=30,
        )
        assert source.returncode == 0 and "POOL_DONE" in source.stdout and "CRYPTO_OK" in source.stdout, source.stdout + source.stderr
        assert ids(camp_db) == [2, 3, 4, 5, 7, 8, 9], ids(camp_db)
        crypto = subprocess.run(
            ["cargo", "test", "push_keys_build_encrypted_vapid_request"],
            cwd=ROOT, capture_output=True, text=True, timeout=120,
        )
        assert crypto.returncode == 0, crypto.stdout + crypto.stderr

        camp_env["WEB_CONCURRENCY"] = "1"
        with open(temp / "puma.log", "w+") as log:
            camp = subprocess.Popen(
                [str(RUBY), str(RUBY.parent / "bundle"), "exec", "puma", "-C", "config/puma.rb"],
                cwd=REPOSITORY, env=camp_env, stdout=log, stderr=log,
            )
            rust = start_server(rust_db, rust_port, {"RUSTFIRE_DISABLE_PUSH": "0"})
            try:
                wait_for_server(camp_port, camp)
                camp_cookie, camp_csrf = login_campfire(camp_port)
                rust_cookie, rust_csrf = "session_token=benchmark-session", "benchmark-csrf"

                post_message(rust_port, rust_cookie, rust_csrf, "Push worker probe", "push-worker-1")
                wait_for_ids(rust_db, [2, 3, 4, 5, 7, 8, 9])

                for subscription_id, status in ((3, 500), (4, 500), (5, 302), (7, 500), (9, 500)):
                    source_response = test_notification(camp_port, camp_cookie, camp_csrf, subscription_id, status)
                    target_response = test_notification(rust_port, rust_cookie, rust_csrf, subscription_id, status)
                    assert target_response == source_response, (subscription_id, source_response, target_response)
                assert ids(camp_db) == ids(rust_db) == [2, 3, 4, 5, 7, 8, 9]
            except Exception:
                log.flush()
                log.seek(0)
                print(log.read()[-1500:])
                raise
            finally:
                stop_server(camp)
                stop_server(rust)
    print("PASS background invalid-point deletion, direct-test failures, and invalid-endpoint skip match Campfire")


if __name__ == "__main__":
    main()
