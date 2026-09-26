"""Compare empty and populated search page sections with pinned Campfire.

Run after ``cargo build --release``. The fixture uses the same search messages
and recent query in both apps; generated CSRF values and origins are normalized.
"""

import pathlib
import re
import sqlite3
import subprocess
import tempfile

from direct_lookup import free_port, start_server, stop_server
from paired_direct_lookup import login_campfire, seed_campfire, seed_rustfire, wait_for_server
from paired_room_shell import Section, get_room
from paired_search import seed_messages


REPOSITORY = pathlib.Path("/tmp/once-campfire-reference")
RUBY = pathlib.Path("/tmp/rustfire-baseline/local/bin/ruby")
BUNDLE = pathlib.Path("/tmp/rustfire-baseline/bundle")
REVISION = "91d294f4a09f9bbe37f9548959bfcb43645678fb"


def section(page, target):
    parser = Section(target, normalize_times=True)
    parser.feed(page.decode())
    tokens = []
    for token in parser.tokens:
        if token[0] == "start":
            attrs = []
            for key, value in token[2]:
                if key == "action":
                    value = value.removeprefix("<origin>")
                if key == "src":
                    value = re.sub(r"/users/[^/]+/avatar", "/users/<signed-avatar>/avatar", value)
                attrs.append((key, value))
            attrs = tuple(attrs)
            tokens.append((token[0], token[1], attrs))
        else:
            tokens.append(token)
    assert tokens, target
    return tokens


def compare(source, target, label):
    for part in ("nav", "sidebar", "message-area", "footer"):
        expected, actual = section(source, part), section(target, part)
        for index, (left, right) in enumerate(zip(expected, actual)):
            assert left == right, (label, part, index, left, right)
        assert len(expected) == len(actual), (label, part, len(expected), len(actual))
        print(f"{label} {part}: {len(actual)} matching parsed tokens")
    assert 'class="sidebar searches admin"' in target.decode()


def main():
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPOSITORY, text=True).strip() == REVISION
    with tempfile.TemporaryDirectory(prefix="paired-search-shell-") as directory:
        temp = pathlib.Path(directory)
        rust_db, camp_db = temp / "rust.sqlite3", temp / "camp.sqlite3"
        rust_port, camp_port = free_port(), free_port()
        seed_rustfire(rust_db, rust_port, [])
        env = seed_campfire(REPOSITORY, RUBY, BUNDLE, REPOSITORY / "storage/db/production.sqlite3", camp_db, [], camp_port, temp)
        seed_messages(rust_db, camp_db, 3)
        with sqlite3.connect(rust_db) as db:
            db.execute("UPDATE messages SET body_html='<div>' || body || '</div>'")
        for database in (rust_db, camp_db):
            with sqlite3.connect(database) as db:
                db.execute("INSERT INTO searches(user_id,query,created_at,updated_at) VALUES(1,'benchmark','2026-01-02 00:00:00','2026-01-02 00:00:00')")
        rust = start_server(rust_db, rust_port, {"RUSTFIRE_CAMPFIRE_SECRET_KEY_BASE": env["SECRET_KEY_BASE"]})
        env["WEB_CONCURRENCY"] = "1"
        with open(temp / "puma.log", "w+") as log:
            camp = subprocess.Popen([str(RUBY), str(RUBY.parent / "bundle"), "exec", "puma", "-C", "config/puma.rb"], cwd=REPOSITORY, env=env, stdout=log, stderr=log)
            try:
                wait_for_server(camp_port, camp)
                camp_cookie, _ = login_campfire(camp_port)
                for label, path in (("empty", "/searches"), ("results", "/searches?q=benchmark")):
                    source = get_room(camp_port, camp_cookie, path)
                    target = get_room(rust_port, "session_token=benchmark-session", path)
                    compare(source, target, label)
            except Exception:
                log.flush()
                log.seek(0)
                print(log.read()[-2000:])
                raise
            finally:
                stop_server(camp)
                stop_server(rust)
    print("PASS paired search page sections")


if __name__ == "__main__":
    main()
