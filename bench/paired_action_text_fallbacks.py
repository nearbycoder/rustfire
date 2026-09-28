"""Compare unsigned ActionText attachment fallbacks on disposable apps.

Run after ``cargo build --release`` with the pinned Campfire checkout and bundle.
"""

import argparse
import html
import pathlib
import sqlite3
import subprocess
import tempfile

from direct_lookup import free_port, start_server, stop_server
from paired_banned_content import BUNDLE, REPOSITORY, REVISION, RUBY, start_redis
from paired_direct_lookup import login_campfire, seed_campfire, seed_rustfire, wait_for_server
from paired_rich_filters import indexed_texts, post_blank


CASES = [
    ("missing-empty", "<div>A<action-text-attachment></action-text-attachment>Z</div>"),
    ("missing-filename", "<div>A<action-text-attachment filename='notes.txt'></action-text-attachment>Z</div>"),
    ("missing-caption", "<div>A<action-text-attachment caption='A caption'></action-text-attachment>Z</div>"),
    ("missing-both", "<div>A<action-text-attachment filename='notes.txt' caption='A caption'></action-text-attachment>Z</div>"),
    ("missing-content", "<div>A<action-text-attachment content-type='application/x-unknown' filename='notes.txt' content='hi'></action-text-attachment>Z</div>"),
    ("remote-image", "<div>A<action-text-attachment content-type='image/png' url='https://example.com/picture.png' filename='picture.png'></action-text-attachment>Z</div>"),
    ("remote-image-caption", "<div>A<action-text-attachment content-type='image/png' url='https://example.com/picture.png' caption='Picture'></action-text-attachment>Z</div>"),
    ("content-html", "<div>A<action-text-attachment content-type='text/html' content='&lt;strong&gt;Hello&lt;/strong&gt;'></action-text-attachment>Z</div>"),
    ("trix-missing", "<div>A<figure data-trix-attachment='{" + html.escape('"filename":"notes.txt"', quote=True) + "}'>notes.txt</figure>Z</div>"),
]

MALFORMED_CASES = [
    ("invalid-sgid", "<div>A<action-text-attachment sgid='invalid' filename='notes.txt'></action-text-attachment>Z</div>"),
    ("invalid-trix-sgid", "<div>A<figure data-trix-attachment='{" + html.escape('"sgid":"invalid","filename":"notes.txt","contentType":"text/plain"', quote=True) + "}'>notes.txt</figure>Z</div>"),
]


def run(port, cookie, csrf, database, cases):
    presentations = {name: post_blank(port, cookie, csrf, name, body) for name, body in cases}
    search = indexed_texts(database, cases)
    with sqlite3.connect(database) as db:
        saved = {name: db.execute("SELECT COUNT(*) FROM messages WHERE client_message_id=?", [f"paired-rich-filter-{name}"]).fetchone()[0] for name, _ in cases}
    return presentations, search, saved


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--include-malformed", action="store_true", help="include two known failing malformed-SGID cases")
    args = parser.parse_args()
    cases = CASES + (MALFORMED_CASES if args.include_malformed else [])
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPOSITORY, text=True).strip() == REVISION
    with tempfile.TemporaryDirectory(prefix="paired-action-text-fallbacks-") as scratch:
        temp = pathlib.Path(scratch)
        rust_db, camp_db = temp / "rust.sqlite3", temp / "camp.sqlite3"
        rust_port, camp_port, redis_port = free_port(), free_port(), free_port()
        seed_rustfire(rust_db, rust_port, [])
        env = seed_campfire(REPOSITORY, RUBY, BUNDLE, REPOSITORY / "storage/db/production.sqlite3", camp_db, [], camp_port, temp)
        env["REDIS_URL"] = f"redis://127.0.0.1:{redis_port}"
        env["WEB_CONCURRENCY"] = "1"
        with sqlite3.connect(camp_db) as camp, sqlite3.connect(rust_db) as rust:
            camp.execute("DELETE FROM sqlite_sequence WHERE name='messages'")
            rust.executemany("UPDATE users SET name=?2,updated_at=?3 WHERE id=?1", camp.execute("SELECT id,name,updated_at FROM users WHERE id IN(1,2)").fetchall())
            rust.execute("UPDATE rooms SET name=? WHERE id=1", [camp.execute("SELECT name FROM rooms WHERE id=1").fetchone()[0]])
        redis, redis_log = start_redis(temp, redis_port)
        try:
            rust_process = start_server(rust_db, rust_port, {"RUSTFIRE_CAMPFIRE_SECRET_KEY_BASE": env["SECRET_KEY_BASE"]})
            try:
                rust = run(rust_port, "session_token=benchmark-session", "benchmark-csrf", rust_db, cases)
            finally:
                stop_server(rust_process)
            with (temp / "puma.log").open("w+") as log:
                camp_process = subprocess.Popen([str(RUBY), str(RUBY.parent / "bundle"), "exec", "puma", "-C", "config/puma.rb"], cwd=REPOSITORY, env=env, stdout=log, stderr=log)
                try:
                    wait_for_server(camp_port, camp_process)
                    cookie, csrf = login_campfire(camp_port)
                    camp = run(camp_port, cookie, csrf, camp_db, cases)
                except Exception:
                    log.flush()
                    log.seek(0)
                    print(log.read()[-2500:])
                    raise
                finally:
                    stop_server(camp_process)
        finally:
            redis.terminate()
            redis.wait(timeout=10)
            redis_log.close()
    mismatches = []
    for name, _ in cases:
        rust_response, camp_response = rust[0][name], camp[0][name]
        rust_search, camp_search = rust[1][name], camp[1][name]
        rust_saved, camp_saved = rust[2][name], camp[2][name]
        if rust_response != camp_response or rust_search != camp_search or rust_saved != camp_saved:
            mismatches.append((name, rust_response, camp_response, rust_search, camp_search, rust_saved, camp_saved))
            print(f"{name}: response {rust_response!r} / {camp_response!r}; search {rust_search!r} / {camp_search!r}; saved {rust_saved} / {camp_saved}")
        else:
            assert rust_response[0] == (500 if name.startswith("invalid-") else 200), (name, rust_response[0])
            print(f"{name}: matched response and search text")
    assert not mismatches, f"{len(mismatches)} ActionText fallback cases differ"
    print(f"PASS {len(cases)} ActionText fallback cases")


if __name__ == "__main__":
    main()
