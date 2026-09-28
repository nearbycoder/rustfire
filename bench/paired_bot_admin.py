"""Compare bot administration against the pinned Campfire on disposable databases.

Run after ``cargo build --release`` with the pinned Ruby bundle and Redis available.
This is a behavior probe, not a performance claim.
"""

import argparse
import base64
import hashlib
import html
import http.client
from html.parser import HTMLParser
import pathlib
import re
import sqlite3
import subprocess
import tempfile
import urllib.parse

from direct_lookup import free_port, start_server, stop_server
from paired_direct_lookup import login_campfire, seed_campfire, seed_rustfire, wait_for_server


PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/lXcAAAAASUVORK5CYII=")


class InputValues(HTMLParser):
    def __init__(self):
        super().__init__()
        self.values = {}

    def handle_starttag(self, tag, attrs):
        if tag == "input":
            attributes = dict(attrs)
            if label := attributes.get("aria-label"):
                self.values[label] = attributes.get("value", "")


class AvatarPreview(HTMLParser):
    def __init__(self):
        super().__init__()
        self.src = None

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == "img" and values.get("data-upload-preview-target") == "image":
            self.src = values.get("src")


def request(port, method, path, cookie, csrf, body=b"", content_type=None, extra_headers=None):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    headers = {"Cookie": cookie, "X-CSRF-Token": csrf, "Accept": "text/html"}
    if content_type:
        headers["Content-Type"] = content_type
    headers.update(extra_headers or {})
    try:
        connection.request(method, path, body, headers)
        response = connection.getresponse()
        return response.status, response.getheader("Location"), response.read()
    finally:
        connection.close()


def multipart(name, webhook, avatar=True, method=None, webhook_field="user[webhook_url]", extra_fields=()):
    boundary = b"bot-admin-probe"
    fields = [("user[name]", name.encode())]
    if webhook_field:
        fields.append((webhook_field, webhook.encode()))
    fields.extend((key, value.encode()) for key, value in extra_fields)
    if method:
        fields.insert(0, ("_method", method.encode()))
    body = b"".join(
        b"--" + boundary + b"\r\nContent-Disposition: form-data; name=\"" + key.encode() + b"\"\r\n\r\n" + value + b"\r\n"
        for key, value in fields
    )
    if avatar:
        body += b"--" + boundary + b"\r\nContent-Disposition: form-data; name=\"user[avatar]\"; filename=\"pixel.png\"\r\nContent-Type: image/png\r\n\r\n" + PNG + b"\r\n"
    body += b"--" + boundary + b"--\r\n"
    return body, "multipart/form-data; boundary=" + boundary.decode()


def run_workflow(port, cookie, csrf, database, campfire, storage_root):
    statuses = {}
    documents = {}
    for asset in ("default-bot-avatar-de1d12f7.svg", "key-82330955.svg", "web-e179f247.svg"):
        status, _, body = request(port, "GET", f"/assets/{asset}", cookie, csrf)
        assert status == 200, (asset, status)
        statuses[f"asset_{asset}"] = hashlib.sha256(body).hexdigest()
    status, _, page = request(port, "GET", "/account/bots/new", cookie, csrf)
    assert status == 200, (status, page[:200])
    documents["new"] = page
    form_fields = sorted(set(re.findall(rb'name=["\'](user\[[^"\']+\])["\']', page)))
    statuses["new_fields"] = [field.decode() for field in form_fields]

    body, content_type = multipart("Paired Bot", "https://example.com/first", extra_fields=(("name", "Flat Bot"), ("webhook_url", "https://example.com/flat")))
    status, location, payload = request(port, "POST", "/account/bots", cookie, csrf, body, content_type)
    statuses["create"] = (status, urllib.parse.urlsplit(location).path)
    assert status in (302, 303), (status, payload[:300])
    with sqlite3.connect(database) as db:
        row = db.execute("SELECT id,name,bot_token,role,status FROM users WHERE name='Paired Bot'").fetchone()
        assert row, "Bot was not created"
        bot_id, name, token, role, state = row
        webhook = db.execute("SELECT url FROM webhooks WHERE user_id=?", [bot_id]).fetchone()
        attachment = db.execute(
            "SELECT COUNT(*) FROM active_storage_attachments WHERE record_type='User' AND record_id=? AND name='avatar'" if campfire else "SELECT COUNT(*) FROM avatars WHERE user_id=?",
            [bot_id],
        ).fetchone()[0]
        if campfire:
            key = db.execute("SELECT b.key FROM active_storage_attachments a JOIN active_storage_blobs b ON b.id=a.blob_id WHERE a.record_type='User' AND a.record_id=? AND a.name='avatar'", [bot_id]).fetchone()[0]
            avatar_file = storage_root / key[:2] / key[2:4] / key
        else:
            stored = db.execute("SELECT stored_name FROM avatars WHERE user_id=?", [bot_id]).fetchone()[0]
            avatar_file = storage_root / stored
        memberships = db.execute("SELECT COUNT(*) FROM memberships WHERE user_id=?", [bot_id]).fetchone()[0]
    statuses["created_record"] = (name, role, state, len(token), webhook[0] if webhook else None, attachment, memberships)
    statuses["avatar_original_matches"] = avatar_file.read_bytes() == PNG
    assert re.fullmatch(r"[A-Za-z0-9]{12}", token), token
    with sqlite3.connect(database) as db:
        original_room_name = db.execute("SELECT name FROM rooms WHERE id=1").fetchone()[0]
        db.execute("UPDATE rooms SET name=NULL WHERE id=1")
    try:
        null_room_status, _, null_room_page = request(port, "GET", "/account/bots", cookie, csrf)
        assert null_room_status == 200, (null_room_status, null_room_page[:300])
        documents["index_null_room"] = null_room_page
    finally:
        with sqlite3.connect(database) as db:
            db.execute("UPDATE rooms SET name=? WHERE id=1", [original_room_name])
    status, _, page = request(port, "GET", "/account/bots", cookie, csrf)
    assert status == 200, (status, page[:300])
    documents["index"] = page
    input_values = InputValues()
    input_values.feed(page.decode())
    statuses["index_examples"] = tuple(
        re.sub(r"https?://127\.0\.0\.1:\d+", "ORIGIN", input_values.values.get(label, ""))
        .replace(f"/rooms/1/{bot_id}-{token}/messages", "/rooms/1/BOT_KEY/messages")
        for label in ("curl command for posting messages", "curl command for posting attachments")
    )
    status, _, payload = request(port, "GET", f"/rooms/1/{bot_id}-{token}/messages", "", "")
    statuses["bot_api"] = status
    assert status == 200, (status, payload[:300])

    status, _, page = request(port, "GET", f"/account/bots/{bot_id}/edit", cookie, csrf)
    assert status == 200, (status, page[:300])
    documents["edit"] = page
    preview = AvatarPreview()
    preview.feed(page.decode())
    assert preview.src, "Bot editor has no uploaded avatar preview"
    preview_status, preview_location, _ = request(port, "GET", urllib.parse.urlsplit(preview.src).path, cookie, csrf)
    disk_status, _, preview_bytes = request(port, "GET", urllib.parse.urlsplit(preview_location).path, cookie, csrf)
    statuses["edit_avatar_preview"] = (preview_status, disk_status, preview_bytes == PNG, len(preview_bytes))
    decoded_page = html.unescape(page.decode())
    statuses["edit_form"] = ("user[avatar]" in decoded_page, "Paired Bot" in decoded_page, "https://example.com/first" in decoded_page)

    body, content_type = multipart("Renamed Bot", "https://example.com/second", method="patch", extra_fields=(("name", "Flat Bot"), ("webhook_url", "https://example.com/flat")))
    status, location, payload = request(port, "POST", f"/account/bots/{bot_id}", cookie, csrf, body, content_type)
    statuses["update"] = (status, urllib.parse.urlsplit(location).path)
    assert status in (302, 303), (status, payload[:300])
    with sqlite3.connect(database) as db:
        name = db.execute("SELECT name FROM users WHERE id=?", [bot_id]).fetchone()[0]
        webhook = db.execute("SELECT url FROM webhooks WHERE user_id=?", [bot_id]).fetchone()[0]
    statuses["updated_record"] = (name, webhook)
    status, _, updated_edit = request(port, "GET", f"/account/bots/{bot_id}/edit", cookie, csrf)
    assert status == 200, status
    documents["edit_after_update"] = updated_edit
    updated_preview = AvatarPreview()
    updated_preview.feed(updated_edit.decode())
    assert updated_preview.src, "Updated bot editor has no avatar preview"
    updated_status, updated_location, _ = request(port, "GET", urllib.parse.urlsplit(updated_preview.src).path, cookie, csrf)
    updated_disk_status, _, updated_bytes = request(port, "GET", urllib.parse.urlsplit(updated_location).path, cookie, csrf)
    statuses["updated_avatar_preview"] = (updated_preview.src != preview.src, updated_status, updated_disk_status, updated_bytes == PNG, len(updated_bytes))

    for label, field, url, expected_url in (
        ("blank_webhook", "user[webhook_url]", "", None),
        ("restore_webhook", "user[webhook_url]", "https://example.com/restored", "https://example.com/restored"),
        ("misspelled_webhook", "user[webook_url]", "https://example.com/ignored", None),
        ("restore_before_omission", "user[webhook_url]", "https://example.com/restored", "https://example.com/restored"),
    ):
        body, content_type = multipart("Renamed Bot", url, avatar=False, webhook_field=field)
        status, location, payload = request(port, "PUT", f"/account/bots/{bot_id}", cookie, csrf, body, content_type)
        assert status == 302, (label, status, payload[:300])
        with sqlite3.connect(database) as db:
            webhook = db.execute("SELECT url FROM webhooks WHERE user_id=?", [bot_id]).fetchone()
        saved_url = webhook[0] if webhook else None
        assert saved_url == expected_url, (label, saved_url, expected_url)
        statuses[label] = (status, urllib.parse.urlsplit(location).path, saved_url)

    body, content_type = multipart("Renamed Bot", "", avatar=False, webhook_field=None)
    status, location, payload = request(port, "PUT", f"/account/bots/{bot_id}", cookie, csrf, body, content_type)
    assert status == 302, ("omitted_webhook", status, payload[:300])
    with sqlite3.connect(database) as db:
        webhook = db.execute("SELECT url FROM webhooks WHERE user_id=?", [bot_id]).fetchone()
    assert webhook is None, ("omitted_webhook", webhook)
    statuses["omitted_webhook"] = (status, urllib.parse.urlsplit(location).path, webhook[0] if webhook else None)

    status, location, _ = request(port, "POST", f"/account/bots/{bot_id}/key", cookie, csrf)
    with sqlite3.connect(database) as db:
        unchanged_token = db.execute("SELECT bot_token FROM users WHERE id=?", [bot_id]).fetchone()[0]
    assert unchanged_token == token, (status, unchanged_token, token)
    statuses["key_plain_post"] = (status, location)
    for label, body in (("key_form_without_override", b""), ("key_wrong_override", b"_method=delete")):
        status, location, _ = request(port, "POST", f"/account/bots/{bot_id}/key", cookie, csrf, body, "application/x-www-form-urlencoded")
        with sqlite3.connect(database) as db:
            unchanged_token = db.execute("SELECT bot_token FROM users WHERE id=?", [bot_id]).fetchone()[0]
        assert unchanged_token == token, (label, status, unchanged_token, token)
        statuses[label] = (status, location)

    status, location, payload = request(port, "PUT", f"/account/bots/{bot_id}/key", cookie, csrf)
    assert status in (302, 303), (status, payload[:300])
    with sqlite3.connect(database) as db:
        active_token = db.execute("SELECT bot_token FROM users WHERE id=?", [bot_id]).fetchone()[0]
    assert active_token != token and re.fullmatch(r"[A-Za-z0-9]{12}", active_token)
    old_status, _, _ = request(port, "GET", f"/rooms/1/{bot_id}-{token}/messages", "", "")
    new_status, _, _ = request(port, "GET", f"/rooms/1/{bot_id}-{active_token}/messages", "", "")
    statuses["key_rotation"] = (status, urllib.parse.urlsplit(location).path, old_status, new_status)

    form_body = b"_method=put"
    status, location, payload = request(port, "POST", f"/account/bots/{bot_id}/key", cookie, csrf, form_body, "application/x-www-form-urlencoded")
    assert status in (302, 303), (status, payload[:300])
    with sqlite3.connect(database) as db:
        form_token = db.execute("SELECT bot_token FROM users WHERE id=?", [bot_id]).fetchone()[0]
    assert form_token != active_token and re.fullmatch(r"[A-Za-z0-9]{12}", form_token)
    statuses["key_form_rotation"] = (status, urllib.parse.urlsplit(location).path)

    status, location, payload = request(port, "PATCH", f"/account/bots/{bot_id}/key", cookie, csrf)
    assert status in (302, 303), (status, payload[:300])
    with sqlite3.connect(database) as db:
        patched_token = db.execute("SELECT bot_token FROM users WHERE id=?", [bot_id]).fetchone()[0]
    assert patched_token != form_token and re.fullmatch(r"[A-Za-z0-9]{12}", patched_token)
    active_token = patched_token
    statuses["key_patch_rotation"] = (status, urllib.parse.urlsplit(location).path)

    status, location, payload = request(port, "POST", f"/account/bots/{bot_id}/key", cookie, csrf, b"_method=PUT", "application/x-www-form-urlencoded")
    assert status in (302, 303), (status, payload[:300])
    with sqlite3.connect(database) as db:
        upper_token = db.execute("SELECT bot_token FROM users WHERE id=?", [bot_id]).fetchone()[0]
    assert upper_token != active_token and re.fullmatch(r"[A-Za-z0-9]{12}", upper_token)
    active_token = upper_token
    statuses["key_uppercase_form_rotation"] = (status, urllib.parse.urlsplit(location).path)

    status, location, payload = request(port, "POST", f"/account/bots/{bot_id}/key", cookie, csrf, extra_headers={"X-HTTP-Method-Override": "PATCH"})
    assert status in (302, 303), (status, payload[:300])
    with sqlite3.connect(database) as db:
        header_token = db.execute("SELECT bot_token FROM users WHERE id=?", [bot_id]).fetchone()[0]
    assert header_token != active_token and re.fullmatch(r"[A-Za-z0-9]{12}", header_token)
    active_token = header_token
    statuses["key_header_rotation"] = (status, urllib.parse.urlsplit(location).path)

    delete_body = b"_method=delete"
    status, location, payload = request(port, "POST", f"/account/bots/{bot_id}", cookie, csrf, delete_body, "application/x-www-form-urlencoded")
    statuses["delete"] = (status, urllib.parse.urlsplit(location).path)
    assert status in (302, 303), (status, payload[:300])
    with sqlite3.connect(database) as db:
        state, old_token = db.execute("SELECT status,bot_token FROM users WHERE id=?", [bot_id]).fetchone()
        memberships = db.execute("SELECT COUNT(*) FROM memberships WHERE user_id=?", [bot_id]).fetchone()[0]
        statuses["deactivated"] = (state, old_token == active_token, memberships)
    body, content_type = multipart("Avatarless Bot", "", avatar=False)
    status, location, _ = request(port, "POST", "/account/bots", cookie, csrf, body, content_type)
    assert status == 302, status
    statuses["avatarless_create"] = status, urllib.parse.urlsplit(location).path
    with sqlite3.connect(database) as db:
        avatarless_id = db.execute("SELECT id FROM users WHERE name='Avatarless Bot'").fetchone()[0]
    status, _, page = request(port, "GET", f"/account/bots/{avatarless_id}/edit", cookie, csrf)
    assert status == 200, status
    documents["edit_without_avatar"] = page
    preview = AvatarPreview()
    preview.feed(page.decode())
    statuses["avatarless_preview"] = preview.src
    return statuses, documents


def verify_legacy_migration(database, port, uploads):
    seed_rustfire(database, port, [])
    stamp = "2026-01-01T00:00:00Z"
    with sqlite3.connect(database) as db:
        db.execute("INSERT INTO users(id,name,role,status,bot_token,created_at,updated_at) VALUES(52,'Legacy Bot',2,0,'52-legacyabc123',?1,?1)", [stamp])
        db.execute("INSERT INTO memberships(room_id,user_id,involvement,created_at) VALUES(1,52,'mentions',?)", [stamp])
    process = start_server(database, port, {"RUSTFIRE_UPLOAD_DIR": str(uploads)})
    try:
        with sqlite3.connect(database) as db:
            token = db.execute("SELECT bot_token FROM users WHERE id=52").fetchone()[0]
        assert token == "legacyabc123", token
        status, _, _ = request(port, "GET", "/rooms/1/52-legacyabc123/messages", "", "")
        assert status == 200, status
    finally:
        stop_server(process)


def cleanup_campfire_uploads(source_database, database, repository):
    with sqlite3.connect(source_database) as source:
        previous_id = source.execute("SELECT COALESCE(MAX(id),0) FROM active_storage_blobs").fetchone()[0]
    with sqlite3.connect(database) as db:
        keys = [row[0] for row in db.execute("SELECT key FROM active_storage_blobs WHERE id>?", [previous_id])]
    for key in keys:
        if not re.fullmatch(r"[A-Za-z0-9_-]+", key):
            continue
        file = repository / "storage/files" / key[:2] / key[2:4] / key
        file.unlink(missing_ok=True)
        for directory in (file.parent, file.parent.parent):
            try:
                directory.rmdir()
            except OSError:
                pass


def main():
    from paired_room_shell import assert_equal, section
    parser = argparse.ArgumentParser()
    parser.add_argument("--campfire-repo", type=pathlib.Path, default=pathlib.Path("/tmp/once-campfire-reference"))
    parser.add_argument("--ruby", type=pathlib.Path, default=pathlib.Path("/tmp/rustfire-baseline/local/bin/ruby"))
    parser.add_argument("--bundle-path", type=pathlib.Path, default=pathlib.Path("/tmp/rustfire-baseline/bundle"))
    parser.add_argument("--sample-dir", type=pathlib.Path)
    args = parser.parse_args()
    repository, ruby, bundle_path = args.campfire_repo.resolve(), args.ruby.resolve(), args.bundle_path.resolve()
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repository, text=True).strip()
    assert revision == "91d294f4a09f9bbe37f9548959bfcb43645678fb", revision
    with tempfile.TemporaryDirectory(prefix="paired-bot-admin-") as temporary:
        temp = pathlib.Path(temporary)
        rust_db, camp_db = temp / "rustfire.sqlite3", temp / "campfire.sqlite3"
        rust_port, camp_port = free_port(), free_port()
        seed_rustfire(rust_db, rust_port, [])
        source_database = repository / "storage/db/production.sqlite3"
        env = seed_campfire(repository, ruby, bundle_path, source_database, camp_db, [], camp_port, temp)
        with sqlite3.connect(camp_db) as source:
            owner_name = source.execute("SELECT name FROM users WHERE id=1").fetchone()[0]
            room_name = source.execute("SELECT name FROM rooms WHERE id=1").fetchone()[0]
        with sqlite3.connect(rust_db) as target:
            target.execute("UPDATE users SET name=? WHERE id=1", [owner_name])
            target.execute("UPDATE rooms SET name=? WHERE id=1", [room_name])
        vapid_code = 'require "openssl"; require "base64"; key=OpenSSL::PKey::EC.new(File.binread(ARGV[0])); puts Base64.urlsafe_encode64(key.public_key.to_bn.to_s(2), padding: false); puts Base64.urlsafe_encode64(key.private_key.to_s(2).rjust(32,"\\0"), padding: false)'
        env["VAPID_PUBLIC_KEY"], env["VAPID_PRIVATE_KEY"] = subprocess.check_output(
            [str(ruby), "-e", vapid_code, str(rust_db.with_suffix(".vapid.der"))], text=True,
        ).splitlines()

        rust_process = start_server(rust_db, rust_port, {"RUSTFIRE_UPLOAD_DIR": str(temp / "uploads")})
        try:
            rust, rust_documents = run_workflow(rust_port, "session_token=benchmark-session", "benchmark-csrf", rust_db, False, temp / "uploads/avatars")
        finally:
            stop_server(rust_process)

        log = open(temp / "puma.log", "w+")
        camp_process = subprocess.Popen([str(ruby), str(ruby.parent / "bundle"), "exec", "puma", "-C", "config/puma.rb"], cwd=repository, env=env, stdout=log, stderr=log)
        try:
            wait_for_server(camp_port, camp_process)
            cookie, csrf = login_campfire(camp_port)
            camp, camp_documents = run_workflow(camp_port, cookie, csrf, camp_db, True, repository / "storage/files")
        except Exception:
            log.flush()
            log.seek(0)
            print(log.read()[-3000:])
            raise
        finally:
            stop_server(camp_process)
            log.close()
            cleanup_campfire_uploads(source_database, camp_db, repository)
        for key in rust:
            assert rust[key] == camp[key], (key, rust[key], camp[key])
            print(f"{key}: {rust[key]!r}")
        if args.sample_dir:
            args.sample_dir.mkdir(parents=True, exist_ok=True)
            for name, documents in (("rustfire", rust_documents), ("campfire", camp_documents)):
                for page, body in documents.items():
                    (args.sample_dir / f"{name}-bot-{page}.html").write_bytes(body)
        for page in ("new", "index", "index_null_room", "edit", "edit_after_update", "edit_without_avatar"):
            for part in ("head", "body"):
                assert_equal(
                    f"bot {page} complete {part}",
                    section(camp_documents[page], part, normalize_blob_paths=True, normalize_avatar_paths=True, normalize_bot_keys=True),
                    section(rust_documents[page], part, normalize_blob_paths=True, normalize_avatar_paths=True, normalize_bot_keys=True),
                )
        verify_legacy_migration(temp / "legacy.sqlite3", free_port(), temp / "legacy-uploads")
        print("legacy_migration: passed")


if __name__ == "__main__":
    main()
