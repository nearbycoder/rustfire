"""Compare the test-notification JSON with the pinned Campfire encoder."""

import json
import os
import pathlib
import subprocess
import tempfile

from direct_lookup import free_port
from paired_direct_lookup import seed_campfire
from paired_push_subscriptions_page import BUNDLE, REPOSITORY, REVISION, RUBY


ROOT = pathlib.Path(__file__).resolve().parents[1]
SOURCE = """path=Rails.application.routes.url_helpers.user_push_subscriptions_url(host: 'rustfire.test', port: 4242)
notification=WebPush::Notification.new(title: 'Campfire Test', body: 'fixed-body', path: path, badge: 3,
  endpoint: 'https://fcm.googleapis.com/fcm/send/test', endpoint_ip_resolver: -> { nil },
  p256dh_key: 'unused', auth_key: 'unused')
puts notification.send(:encoded_message)"""


def main():
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPOSITORY, text=True).strip() == REVISION
    with tempfile.TemporaryDirectory(prefix="paired-push-payload-") as scratch:
        temp = pathlib.Path(scratch)
        environment = seed_campfire(REPOSITORY, RUBY, BUNDLE, REPOSITORY / "storage/db/production.sqlite3", temp / "camp.sqlite3", [], free_port(), temp)
        source = subprocess.run(
            [str(RUBY), str(RUBY.parent / "bundle"), "exec", "rails", "runner", SOURCE],
            cwd=REPOSITORY, env=environment, capture_output=True, text=True, timeout=30, check=True,
        )
        payload = json.loads(source.stdout.strip().splitlines()[-1])
        assert payload["options"]["data"]["path"] == "http://rustfire.test:4242/users/me/push_subscriptions", payload
        test_env = os.environ.copy()
        test_env.pop("RUSTFIRE_PUBLIC_URL", None)
        test_env["RUSTFIRE_EXPECTED_PUSH_TEST_PAYLOAD"] = json.dumps(payload)
        rust = subprocess.run(
            ["cargo", "test", "push_test_payload_matches_source"],
            cwd=ROOT, env=test_env, capture_output=True, text=True, timeout=180,
        )
        assert rust.returncode == 0, rust.stdout + rust.stderr
    print("PASS test-push title, body, icon, badge, and absolute click URL match pinned Campfire")


if __name__ == "__main__":
    main()
