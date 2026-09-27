"""Compare representative message form shapes against pinned Campfire.

Each form gets fresh disposable databases, a valid signed-in CSRF context, and
its own source and Rustfire server. Run after ``cargo build --release``.
"""

import argparse
import html
import pathlib
import re
import sqlite3
import subprocess
import tempfile

from direct_lookup import free_port
from paired_banned_content import BUNDLE, REPOSITORY, REVISION, RUBY, isolated_campfire, start_redis
from paired_direct_lookup import seed_campfire, seed_rustfire
from paired_turbo_fanout import seed_boost_message
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
QUERY_FORMS = (
    ("query body replaces form body", b"message%5Bbody%5D=Body", "text/vnd.turbo-stream.html", "message%5Bbody%5D=Query"),
    ("query group replaces form body", b"message%5Bbody%5D=Body", "text/vnd.turbo-stream.html", "message%5Bbogus%5D=Query"),
    ("query group replaces scalar", b"message=scalar", "text/vnd.turbo-stream.html", "message%5Bbody%5D=Query"),
    ("query scalar replaces group", b"message%5Bbody%5D=Body", "text/vnd.turbo-stream.html", "message=scalar"),
    ("query only", b"", "text/vnd.turbo-stream.html", "message%5Bbody%5D=Query"),
    ("query client ID replaces form body", b"message%5Bbody%5D=Body", "text/vnd.turbo-stream.html", "message%5Bclient_message_id%5D=edge-1"),
)
MULTIPART_QUERY_FORMS = (
    ("multipart query body", (("message[body]", "Body"),), "message%5Bbody%5D=Query"),
    ("multipart query group", (("message[body]", "Body"),), "message%5Bbogus%5D=Query"),
    ("multipart query replaces scalar", (("message", "scalar"),), "message%5Bbody%5D=Query"),
    ("multipart query scalar", (("message[body]", "Body"),), "message=scalar"),
    ("multipart query suppresses file", (("message[body]", "Body"), ("message[attachment]", "sample file", "sample.txt")), "message%5Bbody%5D=Query"),
)
EDIT_QUERY_FORMS = (
    ("edit query body", b"message%5Bbody%5D=Body", "message%5Bbody%5D=Query"),
    ("edit query group", b"message%5Bbody%5D=Body", "message%5Bbogus%5D=Query"),
    ("edit query replaces scalar", b"message=scalar", "message%5Bbody%5D=Query"),
    ("edit query scalar", b"message%5Bbody%5D=Body", "message=scalar"),
    ("edit query only", b"", "message%5Bbody%5D=Query"),
    ("edit query client ID", b"message%5Bbody%5D=Body", "message%5Bclient_message_id%5D=edge-1"),
)


def multipart_body(parts):
    boundary = "----rustfire-message-query"
    body = bytearray()
    for part in parts:
        name, value, *filename = part
        body.extend(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"".encode())
        if filename:
            body.extend(f"; filename=\"{filename[0]}\"\r\nContent-Type: text/plain".encode())
        body.extend(b"\r\n\r\n")
        body.extend(value.encode())
        body.extend(b"\r\n")
    body.extend(f"--{boundary}--\r\n".encode())
    return bytes(body), f"multipart/form-data; boundary={boundary}"


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
            if rails:
                attached = db.execute(
                    "SELECT EXISTS(SELECT 1 FROM active_storage_attachments WHERE record_type='Message' AND record_id=?)",
                    [message_id],
                ).fetchone()[0]
            else:
                attached = db.execute("SELECT EXISTS(SELECT 1 FROM attachments WHERE message_id=?)", [message_id]).fetchone()[0]
            saved.append((plain, client_id == "edge-1", bool(attached)))
    return tuple(saved)


def message_updated_at(database):
    with sqlite3.connect(database) as db:
        return db.execute("SELECT updated_at FROM messages WHERE id=1").fetchone()[0]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    selected = parser.add_mutually_exclusive_group()
    selected.add_argument("--query-only", action="store_true", help="check only URL-encoded query/body cases")
    selected.add_argument("--multipart-query-only", action="store_true", help="check only multipart query/body cases")
    selected.add_argument("--edit-query-only", action="store_true", help="check only message edit query/body cases")
    args = parser.parse_args()
    ordinary = tuple((label, body, accept, "", "application/x-www-form-urlencoded") for label, body, accept in FORMS)
    query = tuple((label, body, accept, query, "application/x-www-form-urlencoded") for label, body, accept, query in QUERY_FORMS)
    multipart = tuple((label, *multipart_body(parts), query) for label, parts, query in MULTIPART_QUERY_FORMS)
    multipart = tuple((label, body, "text/vnd.turbo-stream.html", query, content_type) for label, body, content_type, query in multipart)
    edit = tuple((label, body, "text/html", query, "application/x-www-form-urlencoded") for label, body, query in EDIT_QUERY_FORMS)
    forms = edit if args.edit_query_only else multipart if args.multipart_query_only else query if args.query_only else ordinary + query + multipart
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPOSITORY, text=True).strip() == REVISION
    with tempfile.TemporaryDirectory(prefix="paired-message-parameter-edges-") as scratch:
        temp = pathlib.Path(scratch)
        rust_base, camp_base = temp / "rust-base.sqlite3", temp / "camp-base.sqlite3"
        seed_rustfire(rust_base, free_port(), [])
        base_env = seed_campfire(REPOSITORY, RUBY, BUNDLE, REPOSITORY / "storage/db/production.sqlite3", camp_base, [], free_port(), temp)
        if args.edit_query_only:
            seed_boost_message(rust_base, camp_base)
        with sqlite3.connect(camp_base) as db:
            db.execute("DELETE FROM sessions")
        redis_port = free_port()
        checkout = isolated_campfire(temp, redis_port)
        base_env["REDIS_URL"] = f"redis://127.0.0.1:{redis_port}"
        redis, redis_log = start_redis(temp, redis_port)
        try:
            mismatches = []
            for index, (label, body, accept, query, content_type) in enumerate(forms):
                method, path = ("PATCH", "/rooms/1/messages/1") if args.edit_query_only else ("POST", "/rooms/1/messages")
                rust_result, camp_result = run_case(
                    (method, path, accept), index, temp,
                    rust_base, camp_base, checkout, base_env, redis_port, body=body, query=query,
                    content_type=content_type,
                )
                rust_saved = saved_message(temp / f"rust-{index}.sqlite3", False)
                camp_saved = saved_message(temp / f"camp-{index}.sqlite3", True)
                if args.edit_query_only:
                    rust_saved = (rust_saved, message_updated_at(temp / f"rust-{index}.sqlite3") != message_updated_at(rust_base))
                    camp_saved = (camp_saved, message_updated_at(temp / f"camp-{index}.sqlite3") != message_updated_at(camp_base))
                if (rust_result, rust_saved) != (camp_result, camp_saved):
                    mismatches.append(label)
                    print(f"{label}: Rustfire {(rust_result, rust_saved)}, Campfire {(camp_result, camp_saved)}", flush=True)
        finally:
            redis.terminate()
            redis.wait(timeout=10)
            redis_log.close()
    print(f"Matched {len(forms) - len(mismatches)}/{len(forms)} message form shapes")
    if mismatches:
        raise AssertionError(f"{len(mismatches)} message form shapes differ")


if __name__ == "__main__":
    main()
