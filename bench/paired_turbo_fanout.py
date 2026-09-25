"""Probe signed RoomMessagesChannel fanout on disposable Rustfire and Campfire fixtures.

Requires the pinned Campfire checkout, its installed bundle, and Redis on localhost:6379.
Both runs use the same number of sockets and messages. Message HTML still differs.
"""

import argparse
from datetime import datetime, timezone
import html
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
        print(f"{args.operation}_identity_match=true")


if __name__ == "__main__":
    main()
