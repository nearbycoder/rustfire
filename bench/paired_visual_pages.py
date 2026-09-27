"""Capture matched Campfire and Rustfire page states in Chromium for visual review."""

import argparse
import pathlib
import re
import shutil
import sqlite3
import subprocess
import tempfile
import uuid

from direct_lookup import free_port, start_server, stop_server
from paired_banned_content import BUNDLE, REPOSITORY, REVISION, RUBY, start_redis
from paired_boost_draft_browser import seed_logins
from paired_direct_lookup import seed_campfire, seed_rustfire, wait_for_server
from paired_reply_browser import browser
from paired_turbo_fanout import seed_boost_message


PAGES = (
    ("room", "/rooms/1", "#message_boost-fixture"),
    ("account", "/account/edit", "body"),
    ("profile", "/users/me/profile", "body"),
    ("search", "/searches?q=fixture", "body"),
    ("member", "/users/2", "body"),
    ("bots", "/account/bots", "body"),
    ("room-edit", "/rooms/opens/1/edit", "body"),
    ("push-subscriptions", "/users/me/push_subscriptions", "body"),
)


def capture(session, port, directory, name):
    browser(session, "set", "viewport", "1280", "800")
    browser(session, "open", f"http://127.0.0.1:{port}/session/new")
    browser(session, "fill", 'input[name="email_address"]', "benchmark@example.invalid")
    browser(session, "fill", 'input[name="password"]', "benchmark-password")
    browser(session, "click", 'button[name="log_in"]')
    browser(session, "wait", "--url", "**/rooms/1")
    for label, path, selector in PAGES:
        browser(session, "open", f"http://127.0.0.1:{port}{path}")
        browser(session, "wait", selector)
        browser(session, "wait", "--load", "networkidle")
        if label == "search":
            assert browser(session, "eval", "document.querySelectorAll('.message').length").strip() == "1"
        browser(session, "screenshot", str(directory / f"{name}-{label}.png"))
        assert not browser(session, "errors").strip(), (name, label, browser(session, "errors"))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=pathlib.Path, required=True)
    args = parser.parse_args()
    assert shutil.which("agent-browser"), "agent-browser CLI is required"
    assert shutil.which("magick"), "ImageMagick is required"
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPOSITORY, text=True).strip() == REVISION
    args.output_dir.mkdir(parents=True, exist_ok=True)
    sessions = [f"visual-rust-{uuid.uuid4().hex[:8]}", f"visual-camp-{uuid.uuid4().hex[:8]}"]
    with tempfile.TemporaryDirectory(prefix="paired-visual-pages-") as scratch:
        temp = pathlib.Path(scratch)
        rust_db, camp_db = temp / "rust.sqlite3", temp / "camp.sqlite3"
        rust_port, camp_port, redis_port = free_port(), free_port(), free_port()
        seed_rustfire(rust_db, rust_port, [])
        environment = seed_campfire(REPOSITORY, RUBY, BUNDLE, REPOSITORY / "storage/db/production.sqlite3", camp_db, [], camp_port, temp)
        environment["REDIS_URL"] = f"redis://127.0.0.1:{redis_port}"
        seed_boost_message(rust_db, camp_db)
        seed_logins(rust_db, camp_db)
        with sqlite3.connect(camp_db) as camp, sqlite3.connect(rust_db) as rust:
            updated_at = camp.execute("SELECT updated_at FROM messages WHERE id=1").fetchone()[0]
            member_created_at = camp.execute("SELECT created_at FROM users WHERE id=2").fetchone()[0]
            camp.execute("INSERT INTO message_search_index(rowid,body) VALUES(1,'Boost fixture')")
            camp.execute("UPDATE accounts SET join_code='benchmark' WHERE id=1")
            rust.execute("UPDATE messages SET updated_at=? WHERE id=1", (updated_at,))
            rust.execute("UPDATE users SET created_at=? WHERE id=2", (member_created_at,))
        redis, redis_log = start_redis(temp, redis_port)
        try:
            rust = start_server(rust_db, rust_port, {"RUSTFIRE_CAMPFIRE_SECRET_KEY_BASE": environment["SECRET_KEY_BASE"]})
            try:
                with open(temp / "puma.log", "w+") as log:
                    camp = subprocess.Popen([str(RUBY), str(RUBY.parent / "bundle"), "exec", "puma", "-C", "config/puma.rb"],
                                            cwd=REPOSITORY, env=environment, stdout=log, stderr=log)
                    try:
                        wait_for_server(camp_port, camp)
                        capture(sessions[0], rust_port, args.output_dir, "rustfire")
                        capture(sessions[1], camp_port, args.output_dir, "campfire")
                    finally:
                        stop_server(camp)
            finally:
                stop_server(rust)
        finally:
            for session in sessions:
                subprocess.run(["agent-browser", "--session", session, "close"], capture_output=True, timeout=15)
            redis.terminate()
            redis.wait(timeout=10)
            redis_log.close()
    differences = {}
    for label, _, _ in PAGES:
        compared = subprocess.run(
            ["magick", "compare", "-metric", "AE", str(args.output_dir / f"rustfire-{label}.png"),
             str(args.output_dir / f"campfire-{label}.png"), str(args.output_dir / f"diff-{label}.png")],
            capture_output=True, text=True, timeout=15,
        )
        assert compared.returncode in (0, 1), compared.stderr
        matched = re.search(r"\(([\d.]+)\)", compared.stderr)
        assert matched, compared.stderr
        differences[label] = float(matched.group(1))
        assert differences[label] < 0.002, (label, differences[label])
    print(f"PASS {len(PAGES)} paired Chromium pages at 1280x800: {differences}")


if __name__ == "__main__":
    main()
