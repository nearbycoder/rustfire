"""Compare representative message form shapes against pinned Campfire.

Each form gets fresh disposable databases, a valid signed-in CSRF context, and
its own source and Rustfire server. Run after ``cargo build --release``.
"""

import html
import pathlib
import re
import sqlite3
import subprocess
import tempfile

from direct_lookup import free_port
from paired_banned_content import BUNDLE, REPOSITORY, REVISION, RUBY, isolated_campfire, start_redis
from paired_direct_lookup import seed_campfire, seed_rustfire
from paired_valid_write_route_inventory import run_case


FORMS = (
    ("missing message", b"", "text/html"),
    ("blank body", b"message%5Bbody%5D=", "text/vnd.turbo-stream.html"),
    ("plain body", b"message%5Bbody%5D=Hello+edge", "text/vnd.turbo-stream.html"),
    ("client ID only", b"message%5Bclient_message_id%5D=edge-1", "text/vnd.turbo-stream.html"),
    ("format only", b"message%5Bformat%5D=html", "text/vnd.turbo-stream.html"),
    ("unknown nested field", b"message%5Bbogus%5D=1", "text/vnd.turbo-stream.html"),
    ("blank spaces", b"message%5Bbody%5D=+", "text/vnd.turbo-stream.html"),
    ("scalar message", b"message=scalar", "text/vnd.turbo-stream.html"),
    ("empty scalar message", b"message=", "text/vnd.turbo-stream.html"),
    ("space scalar message", b"message=+", "text/vnd.turbo-stream.html"),
    ("flat body", b"body=Hello+edge", "text/vnd.turbo-stream.html"),
    ("duplicate body", b"message%5Bbody%5D=first&message%5Bbody%5D=second", "text/vnd.turbo-stream.html"),
    ("plain text format", b"message%5Bbody%5D=Hello+edge&message%5Bformat%5D=text", "text/vnd.turbo-stream.html"),
    ("blank body HTML accept", b"message%5Bbody%5D=", "text/html"),
    ("plain body HTML accept", b"message%5Bbody%5D=Hello+edge", "text/html"),
    ("plain body JSON accept", b"message%5Bbody%5D=Hello+edge", "application/json"),
)


def saved_message(database, rails):
    with sqlite3.connect(database) as db:
        rows = db.execute("SELECT id,client_message_id FROM messages ORDER BY id").fetchall()
        saved = []
        for message_id, client_id in rows:
            if rails:
                row = db.execute(
                    "SELECT body FROM action_text_rich_texts WHERE record_type='Message' AND record_id=?",
                    [message_id],
                ).fetchone()
                body = row[0] if row else ""
            else:
                body = db.execute("SELECT body FROM messages WHERE id=?", [message_id]).fetchone()[0]
            plain = html.unescape(re.sub(r"<[^>]*>", "", body)).strip()
            saved.append((plain, client_id == "edge-1"))
    return tuple(saved)


def main():
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPOSITORY, text=True).strip() == REVISION
    with tempfile.TemporaryDirectory(prefix="paired-message-parameter-edges-") as scratch:
        temp = pathlib.Path(scratch)
        rust_base, camp_base = temp / "rust-base.sqlite3", temp / "camp-base.sqlite3"
        seed_rustfire(rust_base, free_port(), [])
        base_env = seed_campfire(REPOSITORY, RUBY, BUNDLE, REPOSITORY / "storage/db/production.sqlite3", camp_base, [], free_port(), temp)
        with sqlite3.connect(camp_base) as db:
            db.execute("DELETE FROM sessions")
        redis_port = free_port()
        checkout = isolated_campfire(temp, redis_port)
        base_env["REDIS_URL"] = f"redis://127.0.0.1:{redis_port}"
        redis, redis_log = start_redis(temp, redis_port)
        try:
            mismatches = []
            for index, (label, body, accept) in enumerate(FORMS):
                rust_result, camp_result = run_case(
                    ("POST", "/rooms/1/messages", accept), index, temp,
                    rust_base, camp_base, checkout, base_env, redis_port, body=body,
                )
                rust_saved = saved_message(temp / f"rust-{index}.sqlite3", False)
                camp_saved = saved_message(temp / f"camp-{index}.sqlite3", True)
                if (rust_result, rust_saved) != (camp_result, camp_saved):
                    mismatches.append(label)
                    print(f"{label}: Rustfire {(rust_result, rust_saved)}, Campfire {(camp_result, camp_saved)}", flush=True)
        finally:
            redis.terminate()
            redis.wait(timeout=10)
            redis_log.close()
    print(f"Matched {len(FORMS) - len(mismatches)}/{len(FORMS)} message form shapes")
    if mismatches:
        raise AssertionError(f"{len(mismatches)} message form shapes differ")


if __name__ == "__main__":
    main()
