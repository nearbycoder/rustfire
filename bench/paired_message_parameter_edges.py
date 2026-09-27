"""Compare representative message form shapes against pinned Campfire.

Each form gets fresh disposable databases, a valid signed-in CSRF context, and
its own source and Rustfire server. Run after ``cargo build --release``.
"""

import argparse
import hashlib
import html
import json
import pathlib
import re
import select
import sqlite3
import subprocess
import tempfile
import urllib.error
import urllib.request

from paired_attachment_mime import image_preview, video_poster
from paired_link_preview import Presentation
from paired_room_shell import Markup
from direct_lookup import ROOT, free_port
from paired_banned_content import BUNDLE, REPOSITORY, REVISION, RUBY, isolated_campfire, start_redis
from paired_direct_lookup import seed_campfire, seed_rustfire
from paired_turbo_fanout import seed_boost_message
from paired_valid_write_route_inventory import run_case

IMAGE = REPOSITORY / "test/fixtures/files/moon.jpg"
VIDEO = REPOSITORY / "test/fixtures/files/alpha-centuri.mov"

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
EDIT_MULTIPART_FORMS = (
    ("edit multipart body", (("message[body]", "Edited"),), ""),
    ("edit multipart file", (("message[body]", "Edited"), ("message[attachment]", "new file", "new.txt")), ""),
    ("edit multipart file only", (("message[attachment]", "new file", "new.txt"),), ""),
    ("edit multipart query suppresses file", (("message[body]", "Body"), ("message[attachment]", "new file", "new.txt")), "message%5Bbody%5D=Query"),
    ("edit multipart image", (("message[attachment]", IMAGE.read_bytes(), "moon.jpg", "image/jpeg"),), ""),
    ("edit multipart video", (("message[attachment]", VIDEO.read_bytes(), "alpha-centuri.mov", "video/quicktime"),), ""),
    ("edit multipart malformed JPEG", (("message[attachment]", b"A plain text attachment.\n", "notes.txt", "image/jpeg"),), ""),
)
EDIT_POST_FORMS = (
    ("POST patch override", b"_method=patch&message%5Bbody%5D=Edited", ""),
    ("POST put override", b"_method=put&message%5Bbody%5D=Edited", ""),
    ("POST override query body", b"_method=patch&message%5Bbody%5D=Body", "message%5Bbody%5D=Query"),
)
EDIT_MULTIPART_POST_FORMS = (
    ("POST multipart patch file", (("_method", "patch"), ("message[body]", "Edited"), ("message[attachment]", "new file", "new.txt")), ""),
    ("POST multipart put file only", (("_method", "put"), ("message[attachment]", "new file", "new.txt")), ""),
    ("POST multipart query suppresses file", (("_method", "patch"), ("message[body]", "Body"), ("message[attachment]", "new file", "new.txt")), "message%5Bbody%5D=Query"),
    ("POST multipart image", (("_method", "patch"), ("message[attachment]", IMAGE.read_bytes(), "moon.jpg", "image/jpeg")), ""),
    ("POST multipart malformed JPEG", (("_method", "put"), ("message[attachment]", b"A plain text attachment.\n", "notes.txt", "image/jpeg")), ""),
)
OLD_FILE = b"old attachment bytes"
OLD_RUST_STORED = "11111111-1111-4111-8111-111111111111"
OLD_CAMP_KEY = "oldattachmentfixturekey000001"


def multipart_body(parts):
    boundary = "----rustfire-message-query"
    body = bytearray()
    for part in parts:
        name, value, *file_metadata = part
        body.extend(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"".encode())
        if file_metadata:
            content_type = file_metadata[1] if len(file_metadata) > 1 else "text/plain"
            body.extend(f"; filename=\"{file_metadata[0]}\"\r\nContent-Type: {content_type}".encode())
        body.extend(b"\r\n\r\n")
        body.extend(value if isinstance(value, bytes) else value.encode())
        body.extend(b"\r\n")
    body.extend(f"--{boundary}--\r\n".encode())
    return bytes(body), f"multipart/form-data; boundary={boundary}"


def saved_message(database, rails, files_root=None):
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
            if files_root is not None and attached:
                if rails:
                    filename, content_type, key = db.execute(
                        "SELECT b.filename,b.content_type,b.key FROM active_storage_attachments a JOIN active_storage_blobs b ON b.id=a.blob_id WHERE a.record_type='Message' AND a.record_id=?",
                        [message_id],
                    ).fetchone()
                    original = files_root / key[:2] / key[2:4] / key
                else:
                    filename, content_type, stored = db.execute(
                        "SELECT filename,content_type,stored_name FROM attachments WHERE message_id=?",
                        [message_id],
                    ).fetchone()
                    original = files_root / stored
                attachment = (filename, content_type, hashlib.sha256(original.read_bytes()).hexdigest())
            else:
                attachment = None
            saved.append((plain, client_id == "edge-1", bool(attached), attachment))
    return tuple(saved)


def message_updated_at(database):
    with sqlite3.connect(database) as db:
        return db.execute("SELECT updated_at FROM messages WHERE id=1").fetchone()[0]


def seed_existing_attachment(rust_database, camp_database):
    with sqlite3.connect(rust_database) as db:
        db.execute(
            "INSERT INTO attachments(message_id,filename,content_type,stored_name,created_at) VALUES(1,'old.txt','text/plain',?,'2026-01-01T00:00:00Z')",
            [OLD_RUST_STORED],
        )
    with sqlite3.connect(camp_database) as db:
        blob_id = db.execute("SELECT COALESCE(MAX(id),0)+1 FROM active_storage_blobs").fetchone()[0]
        db.execute(
            "INSERT INTO active_storage_blobs(id,key,filename,content_type,metadata,service_name,byte_size,created_at) VALUES(?,?,'old.txt','text/plain','{}','local',?,'2026-01-01 00:00:00.000000')",
            [blob_id, OLD_CAMP_KEY, len(OLD_FILE)],
        )
        db.execute(
            "INSERT INTO active_storage_attachments(name,record_type,record_id,blob_id,created_at) VALUES('attachment','Message',1,?,'2026-01-01 00:00:00.000000')",
            [blob_id],
        )


def replaced_preview(rust_port, rust_cookie, camp_port, camp_cookie, kind):
    def preview(port, cookie):
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/rooms/1/messages/1", headers={"Cookie": cookie},
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            assert response.status == 200, (kind, response.status)
            page = response.read()
        if kind == "malformed":
            parsed = Presentation("boost-fixture")
            parsed.feed(page.decode())
            images = [dict(item[1])["src"] for item in parsed.structure
                      if isinstance(item, tuple) and item[0] == "img" and
                      "message__attachment" in dict(item[1]).get("class", "").split()]
            assert len(images) == 1, images
            variant_request = urllib.request.Request(
                f"http://127.0.0.1:{port}{images[0]}", headers={"Cookie": cookie},
            )
            try:
                response = urllib.request.urlopen(variant_request, timeout=30)
            except urllib.error.HTTPError as error:
                response = error
            with response:
                body = response.read()
                return response.status, response.headers.get_content_type(), hashlib.sha256(body).hexdigest()
        check = image_preview if kind == "image" else video_poster
        return check(port, cookie, page, "boost-fixture")

    return preview(rust_port, rust_cookie), preview(camp_port, camp_cookie)


def normalized_replace_stream(payload, target):
    parsed = Markup()
    parsed.feed(payload)
    tokens = []
    for token in parsed.tokens:
        if token[0] != "start":
            tokens.append(token)
            continue
        _, tag, attrs = token
        normalized = []
        for key, value in attrs:
            if value is not None:
                value = re.sub(
                    r"(/rails/active_storage/(?:blobs|representations)/(?:redirect|proxy)/)(?:[^/]+/)+(?=[^/?]+(?:\?|$))",
                    r"\1<signed>/", value,
                )
            normalized.append((key, value))
        tokens.append(("start", tag, tuple(normalized)))
    assert tokens[0] == ("start", "turbo-stream", (("action", "replace"), ("maintain_scroll", "true"), ("target", target))), tokens[:2]
    return tuple(tokens)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    selected = parser.add_mutually_exclusive_group()
    selected.add_argument("--query-only", action="store_true", help="check only URL-encoded query/body cases")
    selected.add_argument("--multipart-query-only", action="store_true", help="check only multipart query/body cases")
    selected.add_argument("--edit-query-only", action="store_true", help="check only message edit query/body cases")
    selected.add_argument("--edit-multipart-only", action="store_true", help="check only message edit multipart cases")
    selected.add_argument("--edit-post-only", action="store_true", help="check only URL-encoded POST method-override edits")
    selected.add_argument("--edit-multipart-post-only", action="store_true", help="check only multipart POST method-override edits")
    parser.add_argument("--label", help="run only a named case from the selected group")
    parser.add_argument("--edit-stream", action="store_true", help="also compare one signed room replacement event per edit")
    args = parser.parse_args()
    if args.edit_stream and not (args.edit_query_only or args.edit_multipart_only or args.edit_post_only or args.edit_multipart_post_only):
        parser.error("--edit-stream requires an edit case group")
    ordinary = tuple((label, body, accept, "", "application/x-www-form-urlencoded") for label, body, accept in FORMS)
    query = tuple((label, body, accept, query, "application/x-www-form-urlencoded") for label, body, accept, query in QUERY_FORMS)
    multipart = tuple((label, *multipart_body(parts), query) for label, parts, query in MULTIPART_QUERY_FORMS)
    multipart = tuple((label, body, "text/vnd.turbo-stream.html", query, content_type) for label, body, content_type, query in multipart)
    edit = tuple((label, body, "text/html", query, "application/x-www-form-urlencoded") for label, body, query in EDIT_QUERY_FORMS)
    edit_multipart = tuple((label, *multipart_body(parts), query) for label, parts, query in EDIT_MULTIPART_FORMS)
    edit_multipart = tuple((label, body, "text/html", query, content_type) for label, body, content_type, query in edit_multipart)
    edit_post = tuple((label, body, "text/html", query, "application/x-www-form-urlencoded") for label, body, query in EDIT_POST_FORMS)
    edit_multipart_post = tuple((label, *multipart_body(parts), query) for label, parts, query in EDIT_MULTIPART_POST_FORMS)
    edit_multipart_post = tuple((label, body, "text/html", query, content_type) for label, body, content_type, query in edit_multipart_post)
    forms = edit_multipart_post if args.edit_multipart_post_only else edit_post if args.edit_post_only else edit_multipart if args.edit_multipart_only else edit if args.edit_query_only else multipart if args.multipart_query_only else query if args.query_only else ordinary + query + multipart
    if args.label:
        forms = tuple(form for form in forms if form[0] == args.label)
        assert forms, args.label
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPOSITORY, text=True).strip() == REVISION
    with tempfile.TemporaryDirectory(prefix="paired-message-parameter-edges-") as scratch:
        temp = pathlib.Path(scratch)
        rust_base, camp_base = temp / "rust-base.sqlite3", temp / "camp-base.sqlite3"
        seed_rustfire(rust_base, free_port(), [])
        base_env = seed_campfire(REPOSITORY, RUBY, BUNDLE, REPOSITORY / "storage/db/production.sqlite3", camp_base, [], free_port(), temp)
        if args.edit_query_only or args.edit_multipart_only or args.edit_post_only or args.edit_multipart_post_only:
            seed_boost_message(rust_base, camp_base)
        if args.edit_multipart_only or args.edit_multipart_post_only:
            seed_existing_attachment(rust_base, camp_base)
        with sqlite3.connect(camp_base) as db:
            db.execute("DELETE FROM sessions")
        redis_port = free_port()
        checkout = isolated_campfire(temp, redis_port)
        base_env["REDIS_URL"] = f"redis://127.0.0.1:{redis_port}"
        redis, redis_log = start_redis(temp, redis_port)
        try:
            mismatches = []
            for index, (label, body, accept, query, content_type) in enumerate(forms):
                if args.edit_multipart_only or args.edit_multipart_post_only:
                    rust_old = temp / f"rust-uploads-{index}" / OLD_RUST_STORED
                    rust_old.parent.mkdir(parents=True, exist_ok=True)
                    rust_old.write_bytes(OLD_FILE)
                    camp_old = checkout / "storage/files" / OLD_CAMP_KEY[:2] / OLD_CAMP_KEY[2:4] / OLD_CAMP_KEY
                    camp_old.parent.mkdir(parents=True, exist_ok=True)
                    camp_old.write_bytes(OLD_FILE)
                method, path = ("POST" if args.edit_post_only or args.edit_multipart_post_only else "PATCH", "/rooms/1/messages/1") if args.edit_query_only or args.edit_multipart_only or args.edit_post_only or args.edit_multipart_post_only else ("POST", "/rooms/1/messages")
                preview_kind = "image" if label in ("edit multipart image", "POST multipart image") else "video" if label == "edit multipart video" else "malformed" if label in ("edit multipart malformed JPEG", "POST multipart malformed JPEG") else None
                stream_expected = args.edit_stream and label != "edit query scalar"
                stream_target = "presentation_message_edge-1" if label == "edit query client ID" else "presentation_message_boost-fixture"
                captures = []
                def start_captures(rust_port, rust_cookie, camp_port, camp_cookie):
                    for port, cookie in ((rust_port, rust_cookie), (camp_port, camp_cookie)):
                        capture = subprocess.Popen(
                            ["node", "bench/capture_message_replace.mjs", "--base", f"http://127.0.0.1:{port}",
                             "--cookie", cookie, "--target", stream_target],
                            cwd=ROOT, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                        )
                        captures.append(capture)
                        ready, _, _ = select.select([capture.stdout], [], [], 20)
                        marker = capture.stdout.readline().strip() if ready else ""
                        if marker != "READY":
                            output, error = capture.communicate(timeout=5)
                            raise AssertionError((label, "socket capture not ready", marker, output, error))

                def inspect_after(rust_port, rust_cookie, camp_port, camp_cookie):
                    preview = replaced_preview(rust_port, rust_cookie, camp_port, camp_cookie, preview_kind) if preview_kind else (None, None)
                    if not stream_expected:
                        return preview
                    streams = []
                    for capture in captures:
                        output, error = capture.communicate(timeout=35)
                        assert capture.returncode == 0, (label, output, error)
                        streams.append(normalized_replace_stream(json.loads(output.strip().splitlines()[-1]), stream_target))
                    return (preview[0], streams[0]), (preview[1], streams[1])

                try:
                    rust_result, camp_result = run_case(
                        (method, path, accept), index, temp,
                        rust_base, camp_base, checkout, base_env, redis_port, body=body, query=query,
                        content_type=content_type,
                        before_request=start_captures if stream_expected else None,
                        after_request=inspect_after if preview_kind or stream_expected else None,
                    )
                finally:
                    for capture in captures:
                        if capture.poll() is None:
                            capture.kill()
                            capture.communicate()
                rust_files = temp / f"rust-uploads-{index}" if args.edit_multipart_only or args.edit_multipart_post_only else None
                camp_files = checkout / "storage/files" if args.edit_multipart_only or args.edit_multipart_post_only else None
                rust_saved = saved_message(temp / f"rust-{index}.sqlite3", False, rust_files)
                camp_saved = saved_message(temp / f"camp-{index}.sqlite3", True, camp_files)
                if args.edit_multipart_only or args.edit_multipart_post_only:
                    replaced = label in ("edit multipart file", "edit multipart file only", "edit multipart image", "edit multipart video", "edit multipart malformed JPEG", "POST multipart patch file", "POST multipart put file only", "POST multipart image", "POST multipart malformed JPEG")
                    assert rust_old.exists() != replaced, (label, "old Rustfire attachment cleanup")
                if args.edit_query_only or args.edit_multipart_only or args.edit_post_only or args.edit_multipart_post_only:
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
