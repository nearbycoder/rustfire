"""Compare ordinary message push JSON with the pinned Campfire builder.

The source renders three message shapes through Room::MessagePusher and
WebPush::Notification. Rustfire's unit test exercises the functions used by
its production enqueue path with those source payloads.
"""

import json
import os
import pathlib
import subprocess
import tempfile

from direct_lookup import free_port
from paired_direct_lookup import seed_campfire
from paired_push_subscriptions_page import BUNDLE, REPOSITORY, REVISION, RUBY


ROOT = pathlib.Path(__file__).resolve().parents[1]
SOURCE = '''require "json"
user = User.find(1)
open_room = Room.find(1)
direct_room = Rooms::Direct.new(id: 2)
unnamed_room = Rooms::Open.new(id: 3, name: nil)
cases = [
  [open_room, "<div>Hello <strong>team</strong></div>", 3],
  [direct_room, "<div>Line one<br>line two</div>", 3],
  [open_room, "", 3],
  [unnamed_room, "Hello", 0]
]
result = cases.map do |room, body, badge|
  message = Message.new(creator: user, room: room, body: body)
  message.define_singleton_method(:attachment) { Struct.new(:filename).new("report.pdf") } if body.empty?
  params = Room::MessagePusher.new(room: room, message: message).send(:build_payload)
  notification = WebPush::Notification.new(**params, badge: badge,
    endpoint: "https://fcm.googleapis.com/fcm/send/test", endpoint_ip_resolver: -> { nil },
    p256dh_key: "unused", auth_key: "unused")
  JSON.parse(notification.send(:encoded_message))
end
puts JSON.generate(result)
'''


def main():
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPOSITORY, text=True).strip() == REVISION
    with tempfile.TemporaryDirectory(prefix="paired-push-message-payload-") as scratch:
        temp = pathlib.Path(scratch)
        environment = seed_campfire(
            REPOSITORY, RUBY, BUNDLE, REPOSITORY / "storage/db/production.sqlite3",
            temp / "camp.sqlite3", [], free_port(), temp,
        )
        source = subprocess.run(
            [str(RUBY), str(RUBY.parent / "bundle"), "exec", "rails", "runner", SOURCE],
            cwd=REPOSITORY, env=environment, capture_output=True, text=True, timeout=30,
        )
        assert source.returncode == 0, source.stdout + source.stderr
        payloads = json.loads(source.stdout.strip().splitlines()[-1])
        assert len(payloads) == 4, payloads
        rust_env = os.environ.copy()
        rust_env["RUSTFIRE_EXPECTED_PUSH_MESSAGE_PAYLOADS"] = json.dumps(payloads)
        rust = subprocess.run(
            ["cargo", "test", "push_message_payload_matches_pinned_campfire"],
            cwd=ROOT, env=rust_env, capture_output=True, text=True, timeout=180,
        )
        assert rust.returncode == 0, rust.stdout + rust.stderr
    print("PASS shared rich text, direct multiline text, file-only, and unnamed-room push JSON match pinned Campfire")


if __name__ == "__main__":
    main()
