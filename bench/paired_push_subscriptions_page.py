"""Compare the push-subscription page and delete form with pinned Campfire.

Run after ``cargo build --release``. Disposable fixtures cover the empty page,
several stored browser profiles, and the source-shaped method-override delete.
"""

import http.client
import pathlib
import sqlite3
import subprocess
import tempfile
import urllib.parse

from direct_lookup import free_port, start_server, stop_server
from paired_direct_lookup import login_campfire, seed_campfire, seed_rustfire, wait_for_server
from paired_room_shell import get_room, section


REPOSITORY = pathlib.Path("/tmp/once-campfire-reference")
RUBY = pathlib.Path("/tmp/rustfire-baseline/local/bin/ruby")
BUNDLE = pathlib.Path("/tmp/rustfire-baseline/bundle")
REVISION = "91d294f4a09f9bbe37f9548959bfcb43645678fb"
PATH = "/users/me/push_subscriptions"
AGENTS = [
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_6) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Safari/605.1.15",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_2 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 Edg/120.0.0.0",
    "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Mobile Safari/537.36",
    "",
]


def compare(camp_port, camp_cookie, rust_port, label):
    source = get_room(camp_port, camp_cookie, PATH)
    target = get_room(rust_port, "session_token=benchmark-session", PATH)
    for part in ("nav", "push_subscriptions", "footer", "sidebar"):
        expected = section(source, part)
        actual = section(target, part)
        for index, (left, right) in enumerate(zip(expected, actual)):
            assert left == right, (label, part, index, left, right)
        assert len(expected) == len(actual), (label, part, len(expected), len(actual))
        print(f"{label} {part}: {len(actual)} matching parsed tokens")
    assert 'class="admin"' in target.decode()


def delete(port, cookie, token):
    body = urllib.parse.urlencode({"_method": "delete", "authenticity_token": token})
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    try:
        connection.request("POST", f"{PATH}/1", body, {
            "Cookie": cookie,
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "text/html",
        })
        response = connection.getresponse()
        payload = response.read()
        assert response.status == 302 and urllib.parse.urlsplit(response.getheader("Location")).path == PATH, (response.status, response.getheader("Location"), payload[:200])
    finally:
        connection.close()


def main():
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPOSITORY, text=True).strip() == REVISION
    with tempfile.TemporaryDirectory(prefix="paired-push-page-") as directory:
        temp = pathlib.Path(directory)
        rust_db, camp_db = temp / "rust.sqlite3", temp / "camp.sqlite3"
        rust_port, camp_port = free_port(), free_port()
        seed_rustfire(rust_db, rust_port, [])
        env = seed_campfire(REPOSITORY, RUBY, BUNDLE, REPOSITORY / "storage/db/production.sqlite3", camp_db, [], camp_port, temp)
        rust = start_server(rust_db, rust_port, {"RUSTFIRE_CAMPFIRE_SECRET_KEY_BASE": env["SECRET_KEY_BASE"], "RUSTFIRE_DISABLE_PUSH": "1"})
        env["WEB_CONCURRENCY"] = "1"
        with open(temp / "puma.log", "w+") as log:
            camp = subprocess.Popen([str(RUBY), str(RUBY.parent / "bundle"), "exec", "puma", "-C", "config/puma.rb"], cwd=REPOSITORY, env=env, stdout=log, stderr=log)
            try:
                wait_for_server(camp_port, camp)
                camp_cookie, camp_token = login_campfire(camp_port)
                compare(camp_port, camp_cookie, rust_port, "empty")
                for database in (rust_db, camp_db):
                    with sqlite3.connect(database) as db:
                        assert db.execute("SELECT count(*) FROM push_subscriptions").fetchone() == (0,)
                        db.executemany("INSERT INTO push_subscriptions(id,user_id,endpoint,p256dh_key,auth_key,user_agent,created_at,updated_at) VALUES(?1,1,?2,'key','auth',?3,'2026-01-01 00:00:00','2026-01-01 00:00:00')", ((index, f"https://push.example.test/sub/{index}", agent or None) for index, agent in enumerate(AGENTS, 1)))
                compare(camp_port, camp_cookie, rust_port, "seven subscriptions")
                delete(camp_port, camp_cookie, camp_token)
                delete(rust_port, "session_token=benchmark-session", "benchmark-csrf")
                for database in (rust_db, camp_db):
                    with sqlite3.connect(database) as db:
                        assert db.execute("SELECT count(*) FROM push_subscriptions WHERE id=1").fetchone() == (0,)
                compare(camp_port, camp_cookie, rust_port, "after delete")
            except Exception:
                log.flush()
                log.seek(0)
                print(log.read()[-2000:])
                raise
            finally:
                stop_server(camp)
                stop_server(rust)
    print("PASS paired push-subscription page and delete form")


if __name__ == "__main__":
    main()
