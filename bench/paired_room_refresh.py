"""Check room-refresh selection and parsed markup against pinned Campfire.

Requires a release build, pinned Campfire checkout, and its Ruby bundle.
The script starts isolated Redis for Campfire and uses disposable databases.
"""

import argparse
from datetime import datetime, timezone
import http.client
import pathlib
import re
import sqlite3
import statistics
import subprocess
import tempfile
import time

from direct_lookup import ROOT, free_port, p95, start_server, stop_server
from message_markup import check_message_markup
from paired_banned_content import start_redis
from paired_direct_lookup import login_campfire, seed_campfire, seed_rustfire, wait_for_server


BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)
CUTOFF = int(BASE.timestamp() * 1000) + 60_000
STAMP = datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
ROWS = [
    (1, "refresh old unchanged", 0, 0),
    (2, "refresh old edited", 0, None),
    (3, "refresh new", 120, None),
]


def times(seconds, updated):
    created = datetime.fromtimestamp(BASE.timestamp() + seconds, timezone.utc)
    changed = datetime.fromisoformat(STAMP.replace("Z", "+00:00")) if updated is None else datetime.fromtimestamp(BASE.timestamp() + updated, timezone.utc)
    return created, changed


def seed_messages(rust_db, camp_db):
    with sqlite3.connect(rust_db) as db:
        for mid, body, seconds, updated in ROWS:
            created, changed = times(seconds, updated)
            db.execute(
                "INSERT INTO messages(id,room_id,creator_id,body,client_message_id,created_at,updated_at) VALUES(?1,1,1,?2,?3,?4,?5)",
                (mid, body, f"refresh-{mid}", created.isoformat(), changed.isoformat()),
            )
    with sqlite3.connect(camp_db) as db:
        for mid, body, seconds, updated in ROWS:
            created, changed = times(seconds, updated)
            created = created.strftime("%Y-%m-%d %H:%M:%S.%f")
            changed = changed.strftime("%Y-%m-%d %H:%M:%S.%f")
            db.execute(
                "INSERT INTO messages(id,room_id,creator_id,client_message_id,created_at,updated_at) VALUES(?1,1,1,?2,?3,?4)",
                (mid, f"refresh-{mid}", created, changed),
            )
            db.execute(
                "INSERT INTO action_text_rich_texts(name,body,record_type,record_id,created_at,updated_at) VALUES('body',?1,'Message',?2,?3,?3)",
                (body, mid, created),
            )


def measure(port, cookie, iterations):
    path = f"/rooms/1/refresh?since={CUTOFF}"
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    samples = []
    body = None
    try:
        connection.request("GET", "/rooms/1", headers={"Cookie": cookie})
        room_response = connection.getresponse()
        room_html = room_response.read().decode()
        if room_response.status != 200:
            raise AssertionError((room_response.status, room_html[:300]))
        for iteration in range(iterations + 2):
            started = time.perf_counter()
            connection.request("GET", path, headers={"Cookie": cookie, "Accept": "text/vnd.turbo-stream.html"})
            response = connection.getresponse()
            content = response.read().decode()
            elapsed = (time.perf_counter() - started) * 1000
            if response.status != 200 or response.getheader("Content-Type", "").split(";")[0] != "text/vnd.turbo-stream.html":
                raise AssertionError((response.status, response.getheader("Content-Type"), content[:300]))
            if body is None:
                body = content
                streams = re.findall(r"<turbo-stream\b([^>]*)>(.*?)</turbo-stream>", content, re.S)
                actions = []
                for attributes, fragment in streams:
                    action = re.search(r"\baction=['\"]([^'\"]+)['\"]", attributes)
                    target = re.search(r"\btarget=['\"]([^'\"]+)['\"]", attributes)
                    if not action or not target or not re.search(rf"\bid=['\"]{re.escape(target.group(1))}['\"]", room_html):
                        raise AssertionError(f"Turbo stream target is missing from the room: {attributes!r}")
                    actions.append((action.group(1), fragment))
                if len(actions) != 2 or [action for action, _ in actions] != ["append", "replace"]:
                    raise AssertionError(f"Unexpected Turbo actions: {actions!r}")
                if "refresh new" not in actions[0][1] or "refresh old edited" not in actions[1][1] or "refresh old unchanged" in content:
                    raise AssertionError("Room refresh selected the wrong messages")
            elif content != body:
                raise AssertionError("Room refresh changed during the probe")
            if iteration >= 2:
                samples.append(elapsed)
    finally:
        connection.close()
    return statistics.median(samples), p95(samples), len(body.encode()), body


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iterations", type=int, default=30)
    parser.add_argument("--sample-dir", type=pathlib.Path, help="save each app's first Turbo refresh response")
    parser.add_argument("--campfire-repo", type=pathlib.Path, default=pathlib.Path("/tmp/once-campfire-reference"))
    parser.add_argument("--ruby", type=pathlib.Path, default=pathlib.Path("/tmp/rustfire-baseline/local/bin/ruby"))
    parser.add_argument("--bundle-path", type=pathlib.Path, default=pathlib.Path("/tmp/rustfire-baseline/bundle"))
    args = parser.parse_args()
    if args.iterations < 1:
        parser.error("iterations must be positive")
    repo = args.campfire_repo.resolve()
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    if revision != "91d294f4a09f9bbe37f9548959bfcb43645678fb":
        parser.error(f"Campfire source is at {revision}, not the pinned compatibility target")
    ruby = args.ruby.resolve()
    with tempfile.TemporaryDirectory(prefix="paired-room-refresh-") as scratch:
        temp = pathlib.Path(scratch)
        rust_db, camp_db = temp / "rust.sqlite3", temp / "camp.sqlite3"
        rust_port, camp_port, redis_port = free_port(), free_port(), free_port()
        seed_rustfire(rust_db, rust_port, [])
        camp_env = seed_campfire(repo, ruby, args.bundle_path.resolve(), repo / "storage/db/production.sqlite3", camp_db, [], camp_port, temp)
        camp_env["REDIS_URL"] = f"redis://127.0.0.1:{redis_port}"
        seed_messages(rust_db, camp_db)
        with sqlite3.connect(camp_db) as camp, sqlite3.connect(rust_db) as rust_db_conn:
            people = camp.execute("SELECT id,name,updated_at FROM users WHERE id IN(1,2)").fetchall()
            rust_db_conn.executemany("UPDATE users SET name=?2,updated_at=?3 WHERE id=?1", people)
            room_name = camp.execute("SELECT name FROM rooms WHERE id=1").fetchone()[0]
            rust_db_conn.execute("UPDATE rooms SET name=? WHERE id=1", [room_name])
        redis, redis_log = start_redis(temp, redis_port)
        rust = start_server(rust_db, rust_port, {"RUSTFIRE_CAMPFIRE_SECRET_KEY_BASE": camp_env["SECRET_KEY_BASE"]})
        try:
            rust_result = measure(rust_port, "session_token=benchmark-session", args.iterations)
        finally:
            stop_server(rust)
        try:
            with open(temp / "puma.log", "w+") as log:
                camp = subprocess.Popen([str(ruby), str(ruby.parent / "bundle"), "exec", "puma", "-C", "config/puma.rb"], cwd=repo, env=camp_env, stdout=log, stderr=log)
                try:
                    wait_for_server(camp_port, camp)
                    cookie, _ = login_campfire(camp_port)
                    camp_result = measure(camp_port, cookie, args.iterations)
                except Exception:
                    log.flush()
                    log.seek(0)
                    print(log.read()[-2000:])
                    raise
                finally:
                    stop_server(camp)
        finally:
            redis.terminate()
            redis.wait(timeout=10)
            redis_log.close()
        if args.sample_dir:
            args.sample_dir.mkdir(parents=True, exist_ok=True)
            (args.sample_dir / "rustfire-refresh.html").write_text(rust_result[3])
            (args.sample_dir / "campfire-refresh.html").write_text(camp_result[3])
        check_message_markup(camp_result[3].encode(), rust_result[3].encode(), 2)
        print("PASS paired reconnect selection and parsed Turbo response markup")
        print(f"rustfire median/p95_ms={rust_result[0]:.3f}/{rust_result[1]:.3f} bytes={rust_result[2]}")
        print(f"campfire median/p95_ms={camp_result[0]:.3f}/{camp_result[1]:.3f} bytes={camp_result[2]}")


if __name__ == "__main__":
    main()
