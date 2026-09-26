"""Check Campfire push recipient scopes against the matching Rust unit fixture."""

import json
import pathlib
import sqlite3
import subprocess
import tempfile

from direct_lookup import free_port
from paired_direct_lookup import seed_campfire
from paired_push_subscriptions_page import BUNDLE, REPOSITORY, REVISION, RUBY


def main():
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPOSITORY, text=True).strip() == REVISION
    with tempfile.TemporaryDirectory(prefix="paired-push-targets-") as scratch:
        temp = pathlib.Path(scratch)
        database = temp / "campfire.sqlite3"
        env = seed_campfire(REPOSITORY, RUBY, BUNDLE, REPOSITORY / "storage/db/production.sqlite3", database, [], free_port(), temp)
        stamp = "2026-01-01 00:00:00"
        with sqlite3.connect(database) as db:
            db.execute("UPDATE users SET status=2 WHERE id IN (2,4)")
            db.execute("UPDATE memberships SET involvement='everything',connected_at=NULL WHERE room_id=1 AND user_id=2")
            db.execute("UPDATE memberships SET involvement='everything',connected_at=NULL WHERE room_id=1 AND user_id=1")
            for user_id, involvement in ((3,"everything"),(4,"mentions"),(5,"mentions"),(6,"invisible"),(7,"everything"),(8,"nothing")):
                db.execute("INSERT INTO memberships(room_id,user_id,involvement,connected_at,created_at,updated_at) VALUES(1,?1,?2,?3,?4,?4)",
                           (user_id, involvement, "2030-01-01 00:00:00" if user_id == 7 else None, stamp))
            for user_id in range(1, 9):
                db.execute("INSERT INTO push_subscriptions(id,user_id,endpoint,p256dh_key,auth_key,created_at,updated_at) VALUES(?1,?1,?2,'key','auth',?3,?3)",
                           (user_id, f"https://fcm.googleapis.com/fcm/send/{user_id}", stamp))
        code = """require 'json'
pusher = Room::MessagePusher.new(room: Room.find(1), message: Message.new(creator: User.find(1)))
everything = pusher.send(:push_subscriptions_for_users_involved_in_everything).order(:id).pluck(:id)
mentions = pusher.send(:push_subscriptions_for_mentionable_users, User.where(id: 4)).order(:id).pluck(:id)
puts JSON.generate({ everything: everything, mentions: mentions })"""
        source = subprocess.run([str(RUBY), str(RUBY.parent / "bundle"), "exec", "rails", "runner", code],
                                cwd=REPOSITORY, env=env, text=True, capture_output=True, timeout=30, check=True)
        result = json.loads(source.stdout.strip().splitlines()[-1])
        assert result == {"everything": [2, 3], "mentions": [4]}, result
        rust = subprocess.run(["cargo", "test", "push_targets_follow_campfire_membership_scopes_even_after_ban"],
                              cwd=pathlib.Path(__file__).resolve().parents[1], text=True, capture_output=True, timeout=120)
        assert rust.returncode == 0, rust.stdout + rust.stderr
    print("PASS Campfire and Rustfire push targets include banned room members and exclude connected, invisible, and unmentioned members")


if __name__ == "__main__":
    main()
