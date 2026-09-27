"""Compare multipart message attachment storage and MIME detection with Campfire."""

import argparse
import hashlib
import http.client
import pathlib
import re
import sqlite3
import subprocess
import tempfile
import urllib.request

from direct_lookup import free_port, start_server, stop_server
from paired_banned_content import BUNDLE, REPOSITORY, REVISION, RUBY, start_redis
from paired_bot_admin import PNG, cleanup_campfire_uploads
from paired_direct_lookup import login_campfire, seed_campfire, seed_rustfire, wait_for_server
from paired_link_preview import Presentation


def cases(large_mib=None, extended_dir=None):
    files = REPOSITORY / "test/fixtures/files"
    fixtures = [
        ("jpeg-as-text", "moon.jpg", "text/plain", (files / "moon.jpg").read_bytes()),
        ("png-as-text", "pixel.png", "text/plain", PNG),
        ("bmp-as-jpeg", "pixel.bmp", "image/jpeg", (files / "pixel.bmp").read_bytes()),
        ("mov-as-text", "alpha-centuri.mov", "text/plain", (files / "alpha-centuri.mov").read_bytes()),
    ]
    if extended_dir is not None:
        for format in ("gif", "webp", "tiff"):
            converted = extended_dir / f"moon.{format}"
            subprocess.run(["vips", "copy", str(files / "moon.jpg"), str(converted)],
                           check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            fixtures.append((f"{format}-as-text", converted.name, "text/plain", converted.read_bytes()))
    if large_mib is not None:
        fixtures.append(("large-file", "large.bin", "application/octet-stream",
                         b"R" * (large_mib * 1024 * 1024 + 1)))
    return fixtures


def post(port, cookie, csrf, case, expected_status=200):
    name, filename, mime, data = case
    boundary = f"paired-attachment-{name}".encode()
    body = (b"--" + boundary + b"\r\nContent-Disposition: form-data; name=\"authenticity_token\"\r\n\r\n" + csrf.encode() + b"\r\n"
            + b"--" + boundary + b"\r\nContent-Disposition: form-data; name=\"message[client_message_id]\"\r\n\r\n" + name.encode() + b"\r\n"
            + b"--" + boundary + b"\r\nContent-Disposition: form-data; name=\"message[attachment]\"; filename=\"" + filename.encode() + b"\"\r\nContent-Type: " + mime.encode() + b"\r\n\r\n"
            + data + b"\r\n--" + boundary + b"--\r\n")
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=40)
    try:
        connection.request("POST", "/rooms/1/messages", body, {
            "Cookie": cookie, "X-CSRF-Token": csrf, "Accept": "text/vnd.turbo-stream.html, text/html",
            "Content-Type": "multipart/form-data; boundary=" + boundary.decode(),
        })
        response = connection.getresponse()
        payload = response.read()
        if expected_status is not None:
            assert response.status == expected_status, (name, response.status, payload[:300])
        if response.status == 200:
            assert response.getheader("Content-Type", "").startswith("text/vnd.turbo-stream.html"), (name, response.status, payload[:300])
            assert f"message_{name}".encode() in payload, (name, payload[:300])
        return response.status, payload
    finally:
        connection.close()


def saved(database, campfire, upload_dir, fixtures):
    with sqlite3.connect(database) as db:
        if campfire:
            rows = db.execute("""SELECT m.client_message_id,b.filename,b.content_type,b.key,b.byte_size
                FROM messages m JOIN active_storage_attachments a ON a.record_type='Message' AND a.record_id=m.id AND a.name='attachment'
                JOIN active_storage_blobs b ON b.id=a.blob_id ORDER BY m.id""").fetchall()
        else:
            rows = db.execute("""SELECT m.client_message_id,a.filename,a.content_type,a.stored_name,NULL
                FROM messages m JOIN attachments a ON a.message_id=m.id ORDER BY m.id""").fetchall()
    assert len(rows) == len(fixtures), rows
    types = []
    for row, (name, filename, _, data) in zip(rows, fixtures):
        client_id, stored_filename, content_type, key, byte_size = row
        assert (client_id, stored_filename) == (name, filename), row
        if byte_size is not None:
            assert byte_size == len(data), row
        path = upload_dir / key[:2] / key[2:4] / key if campfire else upload_dir / key
        with path.open("rb") as stored_file:
            assert hashlib.file_digest(stored_file, "sha256").digest() == hashlib.sha256(data).digest(), path
        types.append(content_type)
    return types


def presentation(payload, name):
    parsed = Presentation(name)
    parsed.feed(payload.decode())
    assert parsed.structure, name
    tokens = []
    for item in parsed.structure:
        if isinstance(item, tuple):
            tag, attrs = item
            attrs = tuple((key, re.sub(r"(/rails/active_storage/(?:blobs|representations)/(?:redirect|proxy)/)(?:[^/]+/)+(?=[^/?]+(?:\?|$))", r"\1<signed>/", value) if value else value)
                          for key, value in attrs)
            tokens.append((tag, attrs))
        else:
            tokens.append(item)
    return tokens


def image_preview(port, cookie, payload, name):
    parsed = Presentation(name)
    parsed.feed(payload.decode())
    images = [dict(item[1])["src"] for item in parsed.structure
              if isinstance(item, tuple) and item[0] == "img" and
              "message__attachment" in dict(item[1]).get("class", "").split()]
    assert len(images) == 1, (name, images)
    assert images[0].startswith("/rails/active_storage/representations/redirect/"), (name, images[0])
    request = urllib.request.Request(f"http://127.0.0.1:{port}{images[0]}", headers={"Cookie": cookie})
    with urllib.request.urlopen(request, timeout=30) as response:
        assert response.status == 200, (name, response.status)
        return response.headers.get_content_type(), hashlib.sha256(response.read()).hexdigest()


def malformed_state(database, campfire, upload_dir):
    with sqlite3.connect(database) as db:
        if campfire:
            row = db.execute("""SELECT m.id,b.filename,b.content_type,b.key,b.byte_size
                FROM messages m JOIN active_storage_attachments a ON a.record_type='Message' AND a.record_id=m.id AND a.name='attachment'
                JOIN active_storage_blobs b ON b.id=a.blob_id WHERE m.client_message_id='text-as-jpeg'""").fetchone()
        else:
            row = db.execute("""SELECT m.id,a.filename,a.content_type,a.stored_name,NULL
                FROM messages m JOIN attachments a ON a.message_id=m.id WHERE m.client_message_id='text-as-jpeg'""").fetchone()
    assert row is not None
    message_id, filename, content_type, key, byte_size = row
    path = upload_dir / key[:2] / key[2:4] / key if campfire else upload_dir / key
    data = path.read_bytes()
    assert byte_size is None or byte_size == len(data)
    return message_id, filename, content_type, hashlib.sha256(data).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--large-mib", type=int,
                        help="also upload a binary message attachment of this many MiB plus one byte")
    parser.add_argument("--extended-images", action="store_true",
                        help="also upload generated GIF, WebP, and TIFF images labeled text/plain")
    args = parser.parse_args()
    if args.large_mib is not None and args.large_mib < 1:
        parser.error("--large-mib must be positive")
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPOSITORY, text=True).strip()
    assert revision == REVISION, revision
    with tempfile.TemporaryDirectory(prefix="paired-attachment-mime-") as scratch:
        temp = pathlib.Path(scratch)
        fixtures = cases(args.large_mib, temp if args.extended_images else None)
        rust_db, camp_db = temp / "rust.sqlite3", temp / "camp.sqlite3"
        rust_port, camp_port, redis_port = free_port(), free_port(), free_port()
        seed_rustfire(rust_db, rust_port, [])
        source_database = REPOSITORY / "storage/db/production.sqlite3"
        env = seed_campfire(REPOSITORY, RUBY, BUNDLE, source_database, camp_db, [], camp_port, temp)
        env["REDIS_URL"] = f"redis://127.0.0.1:{redis_port}"
        with sqlite3.connect(camp_db) as camp, sqlite3.connect(rust_db) as rust:
            camp.execute("DELETE FROM sqlite_sequence WHERE name='messages'")
            rust.executemany("UPDATE users SET name=?2,updated_at=?3 WHERE id=?1", camp.execute("SELECT id,name,updated_at FROM users WHERE id IN(1,2)").fetchall())
            rust.execute("UPDATE rooms SET name=? WHERE id=1", [camp.execute("SELECT name FROM rooms WHERE id=1").fetchone()[0]])
        redis, redis_log = start_redis(temp, redis_port)
        try:
            rust = start_server(rust_db, rust_port, {"RUSTFIRE_UPLOAD_DIR": str(temp / "uploads")})
            try:
                rust_payloads = [post(rust_port, "session_token=benchmark-session", "benchmark-csrf", case)[1] for case in fixtures]
                rust_types = saved(rust_db, False, temp / "uploads", fixtures)
                rust_previews = {name: image_preview(rust_port, "session_token=benchmark-session", payload, name)
                                 for (name, _, _, _), payload in zip(fixtures, rust_payloads)
                                 if name in {"gif-as-text", "webp-as-text", "tiff-as-text"}}
                rust_bad = post(rust_port, "session_token=benchmark-session", "benchmark-csrf", ("text-as-jpeg", "notes.txt", "image/jpeg", b"A plain text attachment.\n"), None)[0]
                rust_bad_row = malformed_state(rust_db, False, temp / "uploads")
                assert not list((temp / "uploads").glob("message-upload-*")), "Rustfire left staged composer files"
            finally:
                stop_server(rust)
            with open(temp / "puma.log", "w+") as log:
                camp = subprocess.Popen([str(RUBY), str(RUBY.parent / "bundle"), "exec", "puma", "-C", "config/puma.rb"], cwd=REPOSITORY, env=env, stdout=log, stderr=log)
                try:
                    wait_for_server(camp_port, camp)
                    cookie, csrf = login_campfire(camp_port)
                    camp_payloads = [post(camp_port, cookie, csrf, case)[1] for case in fixtures]
                    camp_types = saved(camp_db, True, REPOSITORY / "storage/files", fixtures)
                    camp_previews = {name: image_preview(camp_port, cookie, payload, name)
                                     for (name, _, _, _), payload in zip(fixtures, camp_payloads)
                                     if name in {"gif-as-text", "webp-as-text", "tiff-as-text"}}
                    camp_bad = post(camp_port, cookie, csrf, ("text-as-jpeg", "notes.txt", "image/jpeg", b"A plain text attachment.\n"), None)[0]
                    camp_bad_row = malformed_state(camp_db, True, REPOSITORY / "storage/files")
                except Exception:
                    log.flush()
                    log.seek(0)
                    print(log.read()[-3000:])
                    raise
                finally:
                    stop_server(camp)
            assert rust_types == camp_types, (rust_types, camp_types)
            assert rust_previews == camp_previews, (rust_previews, camp_previews)
            assert rust_bad == camp_bad == 500, (rust_bad, camp_bad)
            assert rust_bad_row == camp_bad_row, (rust_bad_row, camp_bad_row)
            for case, rust_payload, camp_payload in zip(fixtures, rust_payloads, camp_payloads):
                rust = presentation(rust_payload, case[0])
                camp = presentation(camp_payload, case[0])
                assert rust == camp, (case[0], next(((i,left,right) for i,(left,right) in enumerate(zip(rust,camp)) if left!=right), (len(rust),len(camp))))
            large_note = f", {args.large_mib} MiB plus one byte file" if args.large_mib else ""
            image_note = ", generated GIF/WebP/TIFF" if args.extended_images else ""
            print(f"PASS paired message attachment types, saved original bytes, Turbo responses, malformed image failure{image_note}{large_note}")
        finally:
            redis.terminate()
            redis.wait(timeout=10)
            redis_log.close()
            cleanup_campfire_uploads(source_database, camp_db, REPOSITORY)


if __name__ == "__main__":
    main()
