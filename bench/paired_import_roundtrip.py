"""Compare a live Campfire fixture with its offline Rustfire import.

Run after cargo build --release with the pinned Ruby bundle and Redis.
"""

import argparse
import base64
from datetime import datetime
import hashlib
import html
import http.client
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import tempfile
import urllib.parse

from direct_lookup import free_port, start_server, stop_server
from paired_bot_admin import AvatarPreview, PNG, cleanup_campfire_uploads, multipart, request
from paired_attachment_mime import post as post_attachment
from paired_direct_lookup import login_campfire, seed_campfire, wait_for_server
from paired_room_shell import AGENT, assert_equal, measure_room_page, section


MESSAGE_INDEX_TEMPLATE_DIGEST = "8686c9089c0ca2724d567e0bf0e90539"


def campfire_page_etag(database, document):
    ids = [int(value) for value in re.findall(rb'data-message-id=["\'](\d+)', document)]
    assert ids
    with sqlite3.connect(database) as db:
        rows = db.execute(
            f"SELECT id,updated_at FROM messages WHERE id IN ({','.join('?' for _ in ids)})", ids
        ).fetchall()
    by_id = {id: updated for id, updated in rows}
    assert len(by_id) == len(ids)
    keys = []
    for id in ids:
        stamp = datetime.fromisoformat(by_id[id].replace("Z", "+00:00"))
        keys.append(f"messages/{id}-{stamp:%Y%m%d%H%M%S%f}")
    return 'W/"' + hashlib.sha256(("/".join(keys) + "/" + MESSAGE_INDEX_TEMPLATE_DIGEST).encode()).hexdigest()[:32] + '"'


def page(port, path, cookie, csrf):
    status, _, body = request(port, "GET", path, cookie, csrf, extra_headers={"User-Agent": AGENT})
    assert status == 200, (path, status)
    return body


def paged_response(port, path, cookie, conditional=None):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    headers = {"Cookie": cookie, "User-Agent": AGENT, "Accept": "text/html"}
    if conditional:
        headers.update(conditional)
    try:
        connection.request("GET", path, headers=headers)
        response = connection.getresponse()
        return response.status, {key.lower(): value for key, value in response.getheaders()}, response.read()
    finally:
        connection.close()


def check_paged_cache(port, path, cookie):
    status, headers, body = paged_response(port, path, cookie)
    etag = headers.get("etag")
    modified = headers.get("last-modified")
    assert status == 200 and body and etag and etag.startswith('W/"') and modified, (path, status, headers)
    conditional = paged_response(port, path, cookie, {"If-None-Match": etag})
    assert conditional[0] == 304 and not conditional[2], (path, conditional[0], len(conditional[2]))
    assert conditional[1].get("etag") == etag and conditional[1].get("last-modified") == modified
    conditional = paged_response(port, path, cookie, {"If-Modified-Since": modified})
    assert conditional[0] == 304 and not conditional[2], (path, conditional[0], len(conditional[2]))
    return etag, modified


def preview(port, document, cookie, csrf):
    parser = AvatarPreview()
    parser.feed(document.decode())
    assert parser.src, "bot edit form has no avatar preview"
    signed_path = urllib.parse.urlsplit(parser.src).path
    status, location, _ = request(port, "GET", signed_path, cookie, csrf)
    assert status == 302 and location, (status, location)
    disk_path = urllib.parse.urlsplit(location).path
    disk_status, _, body = request(port, "GET", disk_path, cookie, csrf)
    assert disk_status == 200 and body == PNG, (disk_status, len(body))
    return signed_path, body


def attachment(port, document, cookie, csrf):
    candidates = re.findall(r"/rails/active_storage/blobs/redirect/[^\"'<> ]+/notes\.txt", html.unescape(document.decode()))
    assert candidates, "room page has no signed imported attachment link"
    path = candidates[0]
    status, location, _ = request(port, "GET", path, cookie, csrf)
    assert status == 302 and location, (status, location)
    disk_status, _, body = request(port, "GET", urllib.parse.urlsplit(location).path, cookie, csrf)
    assert disk_status == 200 and body == b"Imported attachment\n", (disk_status, body)
    return path


def measure_imported_reads(temp, repository, ruby, env, camp_port, rust_db, rust_port,
        uploads, cookie, clients_list, seconds, campfire_workers, message_count):
    binary = temp / "checked_get"
    subprocess.run(("go", "build", "-o", str(binary), "bench/checked_get.go"), check=True)
    trials = []
    for clients in clients_list:
        for order in (("Campfire", "Rustfire"), ("Rustfire", "Campfire")):
            for name in order:
                if name == "Campfire":
                    trial_env = dict(env, WEB_CONCURRENCY=str(campfire_workers),
                        PIDFILE=str(temp / "benchmark-puma.pid"))
                    with (temp / "benchmark-puma.log").open("w+") as log:
                        process = subprocess.Popen((str(ruby), str(ruby.parent / "bundle"), "exec", "puma", "-C", "config/puma.rb"),
                            cwd=repository, env=trial_env, stdout=log, stderr=log)
                        try:
                            wait_for_server(camp_port, process)
                            report = measure_room_page(binary, camp_port, cookie, clients, seconds, messages=message_count)
                        except Exception:
                            log.flush()
                            log.seek(0)
                            print(log.read()[-3000:])
                            raise
                        finally:
                            stop_server(process)
                else:
                    process = start_server(rust_db, rust_port, {
                        "RUSTFIRE_UPLOAD_DIR": str(uploads),
                        "RUSTFIRE_CAMPFIRE_SECRET_KEY_BASE": env["SECRET_KEY_BASE"],
                    })
                    try:
                        report = measure_room_page(binary, rust_port, cookie, clients, seconds, messages=message_count)
                    finally:
                        stop_server(process)
                trials.append({"clients": clients, "order": list(order), "app": name, **report})
                print(f"{clients} clients {'-'.join(order)} {name}: {report['rps']:.1f} rps, p95 {report['p95_ms']:.2f} ms, {report['errors']} errors")
    return trials


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--campfire-repo", type=Path, default=Path("/tmp/once-campfire-reference"))
    parser.add_argument("--ruby", type=Path, default=Path("/tmp/rustfire-baseline/local/bin/ruby"))
    parser.add_argument("--bundle-path", type=Path, default=Path("/tmp/rustfire-baseline/bundle"))
    parser.add_argument("--sample-dir", type=Path)
    parser.add_argument("--extra-messages", type=int, default=0, help="additional rich messages after the initial text and file messages (0-39)")
    parser.add_argument("--read-clients", type=int, nargs="*", default=[])
    parser.add_argument("--seconds", type=float, default=5.0)
    parser.add_argument("--campfire-workers", type=int, default=22)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    if any(clients < 1 for clients in args.read_clients) or args.seconds <= 0 or args.campfire_workers < 1:
        parser.error("positive clients, seconds, and Campfire worker count are required")
    if args.report and not args.read_clients:
        parser.error("--report requires --read-clients")
    if not 0 <= args.extra_messages <= 39:
        parser.error("--extra-messages must be between 0 and 39")
    repository, ruby, bundle_path = args.campfire_repo.resolve(), args.ruby.resolve(), args.bundle_path.resolve()
    revision = subprocess.check_output(("git", "rev-parse", "HEAD"), cwd=repository, text=True).strip()
    assert revision == "91d294f4a09f9bbe37f9548959bfcb43645678fb", revision
    with tempfile.TemporaryDirectory(prefix="paired-import-roundtrip-") as directory:
        temp = Path(directory)
        source_database = repository / "storage/db/production.sqlite3"
        camp_db, rust_db, uploads = temp / "campfire.sqlite3", temp / "rustfire.sqlite3", temp / "uploads"
        camp_port, rust_port = free_port(), free_port()
        env = seed_campfire(repository, ruby, bundle_path, source_database, camp_db, [], camp_port, temp)
        generator_x = bytes.fromhex("6b17d1f2e12c4247f8bce6e563a440f277037d812deb33a0f4a13945d898c296")
        generator_y = bytes.fromhex("4fe342e2fe1a7f9b8ee7eb4a7c0f9e162bce33576b315ececbb6406837bf51f5")
        vapid_public = base64.urlsafe_b64encode(b"\x04" + generator_x + generator_y).rstrip(b"=").decode()
        vapid_private = base64.urlsafe_b64encode(bytes(31) + b"\x01").rstrip(b"=").decode()
        env["VAPID_PUBLIC_KEY"], env["VAPID_PRIVATE_KEY"] = vapid_public, vapid_private
        log = (temp / "puma.log").open("w+")
        process = subprocess.Popen((str(ruby), str(ruby.parent / "bundle"), "exec", "puma", "-C", "config/puma.rb"),
            cwd=repository, env=env, stdout=log, stderr=log)
        try:
            wait_for_server(camp_port, process)
            cookie, csrf = login_campfire(camp_port)
            body, content_type = multipart("Imported Bot", "", avatar=True)
            status, location, _ = request(camp_port, "POST", "/account/bots", cookie, csrf, body, content_type)
            assert status == 302 and urllib.parse.urlsplit(location).path == "/account/bots", (status, location)
            with sqlite3.connect(camp_db) as db:
                bot_id = db.execute("SELECT id FROM users WHERE name='Imported Bot'").fetchone()[0]
            message = urllib.parse.urlencode({"message[body]": "<div>Imported hello</div>"}).encode()
            status, _, _ = request(camp_port, "POST", "/rooms/1/messages", cookie, csrf, message,
                "application/x-www-form-urlencoded", {"Accept": "text/vnd.turbo-stream.html"})
            assert status == 200, status
            post_attachment(camp_port, cookie, csrf,
                ("import-text", "notes.txt", "text/plain", b"Imported attachment\n"))
            for number in range(args.extra_messages):
                rich = urllib.parse.urlencode({"message[body]": f"<div>Imported item {number:02d} <strong>bold</strong></div>"}).encode()
                status, _, _ = request(camp_port, "POST", "/rooms/1/messages", cookie, csrf, rich,
                    "application/x-www-form-urlencoded", {"Accept": "text/vnd.turbo-stream.html"})
                assert status == 200, (number, status)
            paths = {"bot_index": "/account/bots", "bot_edit": f"/account/bots/{bot_id}/edit", "room": "/rooms/1"}
            source = {name: page(camp_port, path, cookie, csrf) for name, path in paths.items()}
            if args.extra_messages == 39:
                visible_ids = [int(value) for value in re.findall(rb'data-message-id=["\'](\d+)', source["room"])]
                assert len(visible_ids) == 40, visible_ids
                paths["older_page"] = f"/rooms/1/messages?before={min(visible_ids)}"
                paths["after_page"] = f"/rooms/1/messages?after={min(visible_ids)}"
                source["older_page"] = page(camp_port, paths["older_page"], cookie, csrf)
                source["after_page"] = page(camp_port, paths["after_page"], cookie, csrf)
                source_cache = {name: check_paged_cache(camp_port, paths[name], cookie)
                    for name in ("older_page", "after_page")}
                for name in source_cache:
                    assert source_cache[name][0] == campfire_page_etag(camp_db, source[name]), (name, source_cache[name])
                oldest_id = min(int(value) for value in re.findall(rb'data-message-id=["\'](\d+)', source["older_page"]))
                assert paged_response(camp_port, f"/rooms/1/messages?before={oldest_id}", cookie)[0] == 204
            source_preview, _ = preview(camp_port, source["bot_edit"], cookie, csrf)
            source_attachment = attachment(camp_port, source["room"], cookie, csrf)
        except Exception:
            log.flush()
            log.seek(0)
            print(log.read()[-3000:])
            cleanup_campfire_uploads(source_database, camp_db, repository)
            raise
        finally:
            stop_server(process)
            log.close()
        try:
            with sqlite3.connect(camp_db) as db:
                db.execute("UPDATE memberships SET updated_at='2025-01-02 03:04:05.123456' WHERE id=(SELECT MIN(id) FROM memberships)")
                source_membership_times = db.execute("SELECT id,created_at,updated_at FROM memberships ORDER BY id").fetchall()
                db.execute("""INSERT INTO push_subscriptions(user_id,endpoint,p256dh_key,auth_key,user_agent,created_at,updated_at)
                    VALUES(1,'https://push.example.test/import','test-p256dh','test-auth','test',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)""")
                live_max = db.execute("SELECT MAX(id) FROM active_storage_blobs").fetchone()[0]
                source_highwater = live_max + 7
                db.execute("""INSERT INTO active_storage_blobs(id,byte_size,created_at,filename,key,service_name)
                    VALUES(?,0,CURRENT_TIMESTAMP,'deleted.txt','paired-import-deleted-blob','local')""", (source_highwater,))
                db.execute("DELETE FROM active_storage_blobs WHERE id=?", (source_highwater,))
                assert db.execute("SELECT seq FROM sqlite_sequence WHERE name='active_storage_blobs'").fetchone()[0] == source_highwater
            importer_env = dict(os.environ, RUSTFIRE_CAMPFIRE_SECRET_KEY_BASE=env["SECRET_KEY_BASE"],
                RUSTFIRE_CAMPFIRE_VAPID_PUBLIC_KEY=vapid_public, RUSTFIRE_CAMPFIRE_VAPID_PRIVATE_KEY=vapid_private)
            command = (sys.executable, "tools/import_campfire.py", "--source-db", str(camp_db),
                "--source-files", str(repository / "storage/files"), "--target-db", str(rust_db),
                "--target-uploads", str(uploads), "--rustfire-bin", "target/release/rustfire")
            result = subprocess.run(command, env=importer_env, check=True, capture_output=True, text=True)
            print("import:", result.stdout.strip())
            with sqlite3.connect(rust_db) as db:
                imported_highwater = db.execute("SELECT last_id FROM id_sequences WHERE name='attachments'").fetchone()[0]
                imported_membership_times = db.execute("SELECT id,created_at,updated_at FROM memberships ORDER BY id").fetchall()
            assert imported_highwater == source_highwater, (imported_highwater, source_highwater)
            assert imported_membership_times == source_membership_times, (imported_membership_times, source_membership_times)
            print("imported deleted-blob ID high-water mark matches")
            print("imported membership creation and update timestamps match")
            rust_process = start_server(rust_db, rust_port, {
                "RUSTFIRE_UPLOAD_DIR": str(uploads),
                "RUSTFIRE_CAMPFIRE_SECRET_KEY_BASE": env["SECRET_KEY_BASE"],
            })
            try:
                target = {name: page(rust_port, path, cookie, csrf) for name, path in paths.items()}
                target_preview, _ = preview(rust_port, target["bot_edit"], cookie, csrf)
                target_attachment = attachment(rust_port, target["room"], cookie, csrf)
                if args.extra_messages == 39:
                    target_cache = {name: check_paged_cache(rust_port, paths[name], cookie)
                        for name in ("older_page", "after_page")}
                    assert paged_response(rust_port, f"/rooms/1/messages?before={oldest_id}", cookie)[0] == 204
            finally:
                stop_server(rust_process)
            assert source_preview == target_preview, (source_preview, target_preview)
            print("imported original avatar signed path and bytes match")
            assert source_attachment == target_attachment, (source_attachment, target_attachment)
            print("imported message attachment signed path and bytes match")
            if args.extra_messages == 39:
                assert source_cache == target_cache, (source_cache, target_cache)
                print("imported older/after ETag, Last-Modified, and empty-page status match")
            if args.sample_dir:
                args.sample_dir.mkdir(parents=True, exist_ok=True)
                for label, documents in (("campfire", source), ("rustfire", target)):
                    for name, document in documents.items():
                        (args.sample_dir / f"{label}-import-{name}.html").write_bytes(document)
            for name in paths:
                for part in (("body",) if name.endswith("_page") else ("head", "body")):
                    options = {"normalize_times": True, "normalize_avatar_paths": True, "normalize_blob_paths": True,
                        "normalize_text_origins": True, "ignore_csrf_inputs": name == "room" or name.endswith("_page")}
                    original = source[name] if not name.endswith("_page") else b"<body>" + source[name] + b"</body>"
                    imported = target[name] if not name.endswith("_page") else b"<body>" + target[name] + b"</body>"
                    assert_equal(f"imported {name} {part}", section(original, part, **options), section(imported, part, **options))
            if args.read_clients:
                trials = measure_imported_reads(temp, repository, ruby, env, camp_port, rust_db, rust_port,
                    uploads, cookie, args.read_clients, args.seconds, args.campfire_workers, min(40, 2 + args.extra_messages))
                if args.report:
                    args.report.parent.mkdir(parents=True, exist_ok=True)
                    args.report.write_text(json.dumps({
                        "source_revision": revision,
                        "rustfire_revision": subprocess.check_output(("git", "rev-parse", "HEAD"), text=True).strip(),
                        "fixture": f"imported Campfire account with {1 + args.extra_messages} rich messages, one text attachment, and one bot avatar",
                        "rendered_messages": min(40, 2 + args.extra_messages),
                        "seconds": args.seconds,
                        "campfire_workers": args.campfire_workers,
                        "trials": trials,
                    }, indent=2) + "\n")
        finally:
            cleanup_campfire_uploads(source_database, camp_db, repository)


if __name__ == "__main__":
    main()
