"""Compare saved subscription outcomes for controlled push-service responses.

The source pool receives synthetic web-push response exceptions. Rustfire's
production response handler is exercised against a disposable SQLite pool.
This isolates response handling; no outbound HTTP or TLS occurs.
"""

import json
import pathlib
import sqlite3
import subprocess
import tempfile

from direct_lookup import free_port
from paired_direct_lookup import seed_campfire
from paired_push_subscriptions_page import BUNDLE, REPOSITORY, REVISION, RUBY


ROOT = pathlib.Path(__file__).resolve().parents[1]
SOURCE = """require 'json'
require 'timeout'
class SyntheticPushNotification
  def initialize(id); @id = id; end
  def deliver(connection: nil)
    response = Struct.new(:body).new('synthetic')
    case @id
    when 1, 6 then raise WebPush::InvalidSubscription.new(response, 'fcm.googleapis.com')
    when 2, 5, 7 then raise WebPush::ExpiredSubscription.new(response, 'fcm.googleapis.com')
    when 3 then raise WebPush::PushServiceError.new(response, 'fcm.googleapis.com')
    when 4 then nil
    end
  end
end
class Push::Subscription
  def notification(**params); SyntheticPushNotification.new(id); end
end
pool = Rails.configuration.x.web_push_pool
pool.queue({ title: 'Test', body: 'Body', path: '/rooms/1' }, Push::Subscription.where(id: 1..4))
Timeout.timeout(10) do
  sleep 0.02 until pool.delivery_pool.completed_task_count >= 4 && pool.invalidation_pool.completed_task_count >= 1
end
background = Push::Subscription.order(:id).pluck(:id)
begin; SyntheticPushNotification.new(5).deliver; rescue WebPush::ExpiredSubscription; end
begin; SyntheticPushNotification.new(6).deliver; rescue WebPush::InvalidSubscription; end
direct = Push::Subscription.order(:id).pluck(:id)
Push::Subscription.where.not(id: 7).delete_all
pool.queue({ title: 'Test', body: 'Body', path: '/rooms/1' }, Push::Subscription.where(id: 7))
Timeout.timeout(10) do
  sleep 0.02 until pool.delivery_pool.completed_task_count >= 5 && pool.invalidation_pool.completed_task_count >= 2
end
puts JSON.generate({ background: background, direct: direct, after_last: Push::Subscription.order(:id).pluck(:id) })
"""


def main():
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPOSITORY, text=True).strip() == REVISION
    with tempfile.TemporaryDirectory(prefix="paired-push-response-") as scratch:
        temp = pathlib.Path(scratch)
        database = temp / "camp.sqlite3"
        env = seed_campfire(
            REPOSITORY, RUBY, BUNDLE, REPOSITORY / "storage/db/production.sqlite3",
            database, [], free_port(), temp,
        )
        with sqlite3.connect(database) as db:
            db.execute("DELETE FROM push_subscriptions")
            db.executemany(
                "INSERT INTO push_subscriptions(id,user_id,endpoint,p256dh_key,auth_key,created_at,updated_at) VALUES(?1,1,?2,'unused','unused','2026-01-01 00:00:00','2026-01-01 00:00:00')",
                [(id, f"https://fcm.googleapis.com/fcm/send/{id}") for id in range(1, 8)],
            )
        source = subprocess.run(
            [str(RUBY), str(RUBY.parent / "bundle"), "exec", "rails", "runner", SOURCE],
            cwd=REPOSITORY, env=env, capture_output=True, text=True, timeout=30,
        )
        assert source.returncode == 0, source.stdout + source.stderr
        observed = json.loads(source.stdout.strip().splitlines()[-1])
        assert observed == {
            "background": [1, 3, 4, 5, 6, 7],
            "direct": [1, 3, 4, 5, 6, 7],
            "after_last": [],
        }, observed
        rust = subprocess.run(
            ["cargo", "test", "push_service_expiry_deletes_only_background_http_410"],
            cwd=ROOT, capture_output=True, text=True, timeout=120,
        )
        assert rust.returncode == 0, rust.stdout + rust.stderr
    print("PASS 404, 410, 503, and 204 subscription outcomes match Campfire's background/direct handling")


if __name__ == "__main__":
    main()
