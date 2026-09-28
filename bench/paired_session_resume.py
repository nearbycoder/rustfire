"""Compare authenticated session activity refresh with pinned Campfire.

Run after ``cargo build --release --locked`` with the pinned Ruby bundle.
The probe uses disposable databases and never changes a real account.
"""

from datetime import datetime, timedelta, timezone
import pathlib
import sqlite3
import subprocess
import tempfile

from direct_lookup import free_port, start_server, stop_server
from paired_bot_admin import request
from paired_direct_lookup import login_campfire, seed_campfire, seed_rustfire, wait_for_server


REPOSITORY = pathlib.Path("/tmp/once-campfire-reference")
RUBY = pathlib.Path("/tmp/rustfire-baseline/local/bin/ruby")
BUNDLE = pathlib.Path("/tmp/rustfire-baseline/bundle")
REVISION = "91d294f4a09f9bbe37f9548959bfcb43645678fb"
OLD_IP = "203.0.113.7"
NEW_IP = "8.8.8.8"
OLD_AGENT = "Before refresh"
NEW_AGENT = "Paired session resume"


def metadata(database, session_id):
    with sqlite3.connect(database) as db:
        return db.execute(
            "SELECT user_agent,ip_address,last_active_at,updated_at FROM sessions WHERE id=?",
            (session_id,),
        ).fetchone()


def timestamp(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def set_metadata(database, session_id, age):
    activity = (datetime.now(timezone.utc) - age).isoformat()
    updated = "2025-01-01T00:00:00Z"
    with sqlite3.connect(database) as db:
        db.execute(
            "UPDATE sessions SET user_agent=?,ip_address=?,last_active_at=?,updated_at=? WHERE id=?",
            (OLD_AGENT, OLD_IP, activity, updated, session_id),
        )
    return OLD_AGENT, OLD_IP, activity, updated


def page(port, cookie):
    status, _, body = request(
        port, "GET", "/account/edit", cookie, "", extra_headers={
            "User-Agent": NEW_AGENT,
            "X-Forwarded-For": NEW_IP,
            "X-Rustfire-Peer-Ip": "1.1.1.1",  # Untrusted client input must not win.
        },
    )
    assert status == 200, (status, body[:300])


def exercise(port, database, session_id, cookie):
    original = set_metadata(database, session_id, timedelta(minutes=5))
    page(port, cookie)
    assert metadata(database, session_id) == original, ("fresh", metadata(database, session_id))

    original = set_metadata(database, session_id, timedelta(hours=2))
    before = datetime.now(timezone.utc)
    page(port, cookie)
    after = datetime.now(timezone.utc)
    refreshed = metadata(database, session_id)
    assert refreshed[:2] == (NEW_AGENT, NEW_IP), refreshed
    assert before - timedelta(seconds=2) <= timestamp(refreshed[2]) <= after + timedelta(seconds=2), refreshed
    assert before - timedelta(seconds=2) <= timestamp(refreshed[3]) <= after + timedelta(seconds=2), refreshed
    assert refreshed[2] != original[2] and refreshed[3] != original[3]

    page(port, cookie)
    assert metadata(database, session_id) == refreshed, ("second request", metadata(database, session_id))
    return "fresh unchanged; stale agent, IP, and times refreshed; immediate repeat unchanged"


def main():
    revision = subprocess.check_output(("git", "rev-parse", "HEAD"), cwd=REPOSITORY, text=True).strip()
    assert revision == REVISION, revision
    with tempfile.TemporaryDirectory(prefix="paired-session-resume-") as directory:
        temp = pathlib.Path(directory)
        rust_db, camp_db = temp / "rustfire.sqlite3", temp / "campfire.sqlite3"
        rust_port, camp_port = free_port(), free_port()
        seed_rustfire(rust_db, rust_port, [])
        env = seed_campfire(REPOSITORY, RUBY, BUNDLE, REPOSITORY / "storage/db/production.sqlite3",
                            camp_db, [], camp_port, temp)
        rust_process = start_server(rust_db, rust_port, {"RUSTFIRE_TRUSTED_PROXY_IPS": "127.0.0.1"})
        try:
            with sqlite3.connect(rust_db) as db:
                rust_session = db.execute("SELECT id FROM sessions WHERE token='benchmark-session'").fetchone()[0]
            rust_result = exercise(rust_port, rust_db, rust_session, "session_token=benchmark-session")
        finally:
            stop_server(rust_process)
        with (temp / "puma.log").open("w+") as log:
            camp_process = subprocess.Popen(
                (str(RUBY), str(RUBY.parent / "bundle"), "exec", "puma", "-C", "config/puma.rb"),
                cwd=REPOSITORY, env=env, stdout=log, stderr=log,
            )
            try:
                wait_for_server(camp_port, camp_process)
                cookie, _ = login_campfire(camp_port)
                with sqlite3.connect(camp_db) as db:
                    camp_session = db.execute("SELECT id FROM sessions WHERE user_id=1 ORDER BY id DESC LIMIT 1").fetchone()[0]
                camp_result = exercise(camp_port, camp_db, camp_session, cookie)
            except Exception:
                log.flush()
                log.seek(0)
                print(log.read()[-3000:])
                raise
            finally:
                stop_server(camp_process)
    assert rust_result == camp_result
    print("PASS paired session resume:", rust_result)


if __name__ == "__main__":
    main()
