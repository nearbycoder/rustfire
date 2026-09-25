"""Probe signed RoomMessagesChannel fanout on disposable Rustfire and Campfire fixtures.

Requires the pinned Campfire checkout, its installed bundle, and Redis on localhost:6379.
Both runs use the same number of sockets and messages. Message values still differ.
"""

import argparse
from datetime import datetime, timezone
import html
from html.parser import HTMLParser
import pathlib
import re
import sqlite3
import subprocess
import tempfile

from direct_lookup import ROOT, free_port, start_server, stop_server
from paired_direct_lookup import login_campfire, seed_campfire, seed_rustfire, wait_for_server


def seed_boost_message(rust_db, camp_db):
    instant = datetime.now(timezone.utc)
    rust_time = instant.isoformat().replace("+00:00", "Z")
    camp_time = instant.strftime("%Y-%m-%d %H:%M:%S.%f")
    with sqlite3.connect(rust_db) as db:
        db.execute("INSERT INTO messages(id,room_id,creator_id,body,client_message_id,created_at,updated_at) VALUES(1,1,1,'Boost fixture','boost-fixture',?1,?1)", [rust_time])
    with sqlite3.connect(camp_db) as db:
        db.execute("INSERT INTO messages(id,room_id,creator_id,client_message_id,created_at,updated_at) VALUES(1,1,1,'boost-fixture',?1,?1)", [camp_time])
        db.execute("INSERT INTO action_text_rich_texts(name,body,record_type,record_id,created_at,updated_at) VALUES('body','Boost fixture','Message',1,?1,?1)", [camp_time])


def fanout(app, port, cookie, csrf, sockets, messages, operation, sample_file=None):
    command = [
        "node", "bench/fanout.mjs", "--app", app,
        "--base", f"http://127.0.0.1:{port}", "--cookie", cookie,
        "--csrf", csrf, "--room", "1", "--sockets", str(sockets),
        "--messages", str(messages), "--operation", operation,
    ]
    if sample_file is not None:
        command.extend(["--sample-file", str(sample_file)])
    result = subprocess.run(
        command,
        cwd=ROOT, text=True, capture_output=True,
    )
    if result.returncode:
        raise RuntimeError(f"{app} fanout failed: {result.stdout}\n{result.stderr}")
    return result.stdout.strip()


def stream_avatar_identity(sample_file):
    sample = html.unescape(sample_file.read_text())
    name = re.search(r"<a\b[^>]*\btitle=['\"]([^'\"]+)['\"]", sample)
    avatar = re.search(r"<img\b[^>]*\bsrc=['\"]([^'\"]+/avatar\?v=\d+)['\"]", sample)
    if not name or not avatar:
        raise RuntimeError(f"Stream sample lacks avatar identity: {sample_file}")
    return name.group(1), avatar.group(1)


def stream_message_id(sample_file):
    sample = sample_file.read_text()
    matched = re.search(r"\bdata-message-id=['\"](\d+)['\"]", sample)
    if not matched:
        raise RuntimeError(f"Message sample lacks its numeric ID: {sample_file}")
    return int(matched.group(1))


def check_message_targets(sample_file):
    sample = sample_file.read_text()
    ids = set(re.findall(r"\bid=['\"]([^'\"]+)['\"]", sample))
    required = (
        "message_", "edit_message_", "presentation_message_",
        "boosting_message_", "boosts_message_", "new_boost_message_",
    )
    missing = [prefix for prefix in required if not any(value.startswith(prefix) for value in ids)]
    if missing:
        raise RuntimeError(f"Message stream lacks frame or DOM targets {missing}: {sample_file}")
    if not re.search(r"<h2\s+class=['\"]message__day-separator['\"]>\s*<time\b", sample):
        raise RuntimeError(f"Message stream lacks its day heading: {sample_file}")
    if "custom-boost-form" in sample:
        raise RuntimeError(f"Message stream eagerly renders a new-boost form: {sample_file}")


def message_room_label(sample_file):
    sample = sample_file.read_text()
    matched = re.search(r"<span\b[^>]*class=['\"]message__room['\"][^>]*>\s*<a\b[^>]*>([^<]*)</a>", sample)
    if not matched:
        raise RuntimeError(f"Message stream lacks its room link: {sample_file}")
    return html.unescape(matched.group(1))


class MessageTagSequence(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags = []
        self.attribute_keys = []

    def handle_starttag(self, tag, attrs):
        self.tags.append(("start", tag))
        self.attribute_keys.append((tag, tuple(sorted(key for key, _ in attrs))))

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        if tag not in {"img", "input", "br", "hr", "source", "meta", "link"}:
            self.tags.append(("end", tag))


def message_structure(sample_file):
    parser = MessageTagSequence()
    parser.feed(sample_file.read_text())
    return parser.tags, parser.attribute_keys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sockets", type=int, default=50)
    parser.add_argument("--messages", type=int, default=5)
    parser.add_argument("--operation", choices=["messages", "boosts"], default="messages")
    parser.add_argument("--campfire-workers", type=int, default=1)
    parser.add_argument("--sample-dir", type=pathlib.Path, help="Write one received Turbo event from each app to this directory")
    parser.add_argument("--campfire-repo", type=pathlib.Path, default=pathlib.Path("/tmp/once-campfire-reference"))
    parser.add_argument("--ruby", type=pathlib.Path, default=pathlib.Path("/tmp/rustfire-baseline/local/bin/ruby"))
    parser.add_argument("--bundle-path", type=pathlib.Path, default=pathlib.Path("/tmp/rustfire-baseline/bundle"))
    args = parser.parse_args()
    if args.sockets < 1 or args.messages < 1 or args.campfire_workers < 1:
        parser.error("sockets, messages, and workers must be positive")
    repo = args.campfire_repo.resolve()
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    if revision != "91d294f4a09f9bbe37f9548959bfcb43645678fb":
        parser.error(f"Campfire source is at {revision}, not the pinned compatibility target")
    ruby = args.ruby.resolve()
    sample_dir = args.sample_dir.resolve() if args.sample_dir else None
    if sample_dir:
        sample_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="paired-turbo-fanout-") as scratch:
        temp = pathlib.Path(scratch)
        output_dir = sample_dir or temp
        rust_db, camp_db = temp / "rust.sqlite3", temp / "camp.sqlite3"
        rust_port, camp_port = free_port(), free_port()
        seed_rustfire(rust_db, rust_port, [])
        camp_env = seed_campfire(repo, ruby, args.bundle_path.resolve(), repo / "storage/db/production.sqlite3", camp_db, [], camp_port, temp)
        with sqlite3.connect(camp_db) as camp, sqlite3.connect(rust_db) as rust:
            camp.execute("DELETE FROM sqlite_sequence WHERE name='messages'")
            people = camp.execute("SELECT id,name,updated_at FROM users WHERE id IN(1,2)").fetchall()
            rust.executemany("UPDATE users SET name=?2,updated_at=?3 WHERE id=?1", people)
            room_name = camp.execute("SELECT name FROM rooms WHERE id=1").fetchone()[0]
            rust.execute("UPDATE rooms SET name=? WHERE id=1", [room_name])
        camp_env["WEB_CONCURRENCY"] = str(args.campfire_workers)
        if args.operation == "boosts":
            seed_boost_message(rust_db, camp_db)

        rust = start_server(rust_db, rust_port, {
            "RUSTFIRE_DISABLE_PUSH": "0", "RUSTFIRE_DISABLE_WEBHOOKS": "0",
            "RUSTFIRE_CAMPFIRE_SECRET_KEY_BASE": camp_env["SECRET_KEY_BASE"],
        })
        try:
            rust_result = fanout("rustfire-turbo", rust_port, "session_token=benchmark-session", "benchmark-csrf", args.sockets, args.messages, args.operation, output_dir / "rustfire.html")
        finally:
            stop_server(rust)

        with open(temp / "puma.log", "w+") as log:
            camp = subprocess.Popen(
                [str(ruby), str(ruby.parent / "bundle"), "exec", "puma", "-C", "config/puma.rb"],
                cwd=repo, env=camp_env, stdout=log, stderr=log,
            )
            try:
                wait_for_server(camp_port, camp)
                cookie, csrf = login_campfire(camp_port)
                camp_result = fanout("campfire", camp_port, cookie, csrf, args.sockets, args.messages, args.operation, output_dir / "campfire.html")
            except Exception:
                log.flush()
                log.seek(0)
                print(log.read()[-2000:])
                raise
            finally:
                stop_server(camp)
        print(f"rustfire-turbo {rust_result}")
        print(f"campfire       {camp_result}")
        rust_identity = stream_avatar_identity(output_dir / "rustfire.html")
        camp_identity = stream_avatar_identity(output_dir / "campfire.html")
        if rust_identity != camp_identity:
            raise RuntimeError(f"Avatar identity differs: Rustfire {rust_identity}, Campfire {camp_identity}")
        if args.operation == "messages" and stream_message_id(output_dir / "rustfire.html") != stream_message_id(output_dir / "campfire.html"):
            raise RuntimeError("Message IDs differ in paired stream samples")
        if args.operation == "messages":
            check_message_targets(output_dir / "rustfire.html")
            check_message_targets(output_dir / "campfire.html")
            rust_room_label = message_room_label(output_dir / "rustfire.html")
            camp_room_label = message_room_label(output_dir / "campfire.html")
            if rust_room_label != camp_room_label:
                raise RuntimeError(f"Message room labels differ: Rustfire {rust_room_label}, Campfire {camp_room_label}")
            rust_tags, rust_attributes = message_structure(output_dir / "rustfire.html")
            camp_tags, camp_attributes = message_structure(output_dir / "campfire.html")
            if rust_tags != camp_tags:
                raise RuntimeError("Message stream tag structure differs from Campfire")
            if rust_attributes != camp_attributes:
                raise RuntimeError("Message stream attribute keys differ from Campfire")
        print(f"{args.operation}_identity_match=true")


if __name__ == "__main__":
    main()
