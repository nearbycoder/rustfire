"""Compare direct-uploaded ActionText files embedded in new rich messages."""

import base64
import hashlib
import http.client
import json
import pathlib
import sqlite3
import subprocess
import tempfile
import urllib.parse
import urllib.request

from direct_lookup import free_port, start_server, stop_server
from paired_banned_content import BUNDLE, REPOSITORY, REVISION, RUBY, start_redis
from paired_bot_admin import cleanup_campfire_uploads, request
from paired_direct_lookup import login_campfire, seed_campfire, seed_rustfire, wait_for_server
from paired_direct_upload import path_from_url, raw_request
from paired_link_preview import Presentation


CASES = [("text", "note.txt", "text/plain", b"An inline note.\n"),
         ("image", "moon.jpg", "image/jpeg", (REPOSITORY / "test/fixtures/files/moon.jpg").read_bytes())]


def upload(port, cookie, csrf, filename, content_type, data):
    payload = json.dumps({"blob": {"filename": filename, "byte_size": len(data),
                                   "checksum": base64.b64encode(hashlib.md5(data).digest()).decode(),
                                   "content_type": content_type}}).encode()
    status, _, body = request(port, "POST", "/rails/active_storage/direct_uploads", cookie, csrf, payload, "application/json")
    assert status == 200, (filename, status, body[:200])
    metadata = json.loads(body)
    status, _, _ = raw_request(port, "PUT", path_from_url(metadata["direct_upload"]["url"]), data,
                               {"Cookie": cookie, "Content-Type": content_type})
    assert status == 204, (filename, status)
    return metadata


def post(port, cookie, csrf, name, metadata, rich=None):
    attachment = f'<action-text-attachment sgid="{metadata["attachable_sgid"]}" content-type="{metadata["content_type"]}" filename="{metadata["filename"]}"></action-text-attachment>'
    rich = rich if rich is not None else f"<div>Before {attachment} After</div>"
    body = urllib.parse.urlencode({"message[body]": rich, "message[client_message_id]": f"inline-upload-{name}",
                                   "authenticity_token": csrf})
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    try:
        connection.request("POST", "/rooms/1/messages", body, {"Cookie": cookie,
                           "Content-Type": "application/x-www-form-urlencoded",
                           "Accept": "text/vnd.turbo-stream.html, text/html"})
        response = connection.getresponse()
        payload = response.read()
        parsed = Presentation(f"inline-upload-{name}")
        parsed.feed(payload.decode())
        return response.status, parsed.structure, payload
    finally:
        connection.close()


def normalized(structure):
    result = []
    for item in structure:
        if not isinstance(item, tuple):
            result.append(item)
            continue
        tag, attrs = item
        values = dict(attrs)
        if tag == "img" and "/rails/active_storage/representations/redirect/" in values.get("src", ""):
            path = urllib.parse.urlsplit(values["src"]).path
            parts = path.split("/")
            assert len(parts) >= 8 and parts[-1].endswith(".jpg"), path
            variation = json.loads(base64.b64decode(parts[-2].split("--", 1)[0]))["_rails"]["data"]
            values["src"] = f'/rails/active_storage/representations/redirect/<blob>/{variation["format"]}:{variation["resize_to_limit"]}/{parts[-1]}'
        result.append((tag, tuple(sorted(values.items()))))
    return result


def image_previews(port, cookie, structure, expected=1):
    paths = [dict(item[1])["src"] for item in structure
             if isinstance(item, tuple) and item[0] == "img" and
             "/rails/active_storage/representations/redirect/" in dict(item[1]).get("src", "")]
    assert len(paths) == expected, paths
    digests = []
    for path in paths:
        request = urllib.request.Request(f"http://127.0.0.1:{port}{urllib.parse.urlsplit(path).path}",
                                         headers={"Cookie": cookie})
        with urllib.request.urlopen(request, timeout=30) as response:
            assert response.status == 200 and response.headers.get_content_type() == "image/jpeg"
            digests.append(hashlib.sha256(response.read()).hexdigest())
    return tuple(digests)


def stored_blob(database, campfire, upload_root, blob_id, data, expected_links=1):
    with sqlite3.connect(database) as db:
        if campfire:
            key = db.execute("SELECT key FROM active_storage_blobs WHERE id=?", [blob_id]).fetchone()[0]
            linked = db.execute("SELECT count(*) FROM active_storage_attachments WHERE record_type='ActionText::RichText' AND name='embeds' AND blob_id=?", [blob_id]).fetchone()[0]
            path = upload_root / key[:2] / key[2:4] / key
        else:
            key = db.execute("SELECT stored_name FROM inline_blobs WHERE id=?", [blob_id]).fetchone()[0]
            linked = db.execute("SELECT count(*) FROM inline_embeds WHERE blob_id=?", [blob_id]).fetchone()[0]
            path = upload_root / key
    assert linked == expected_links, (blob_id, linked, expected_links)
    assert hashlib.sha256(path.read_bytes()).digest() == hashlib.sha256(data).digest(), path
    return path


def search_text(database, name):
    with sqlite3.connect(database) as db:
        return db.execute("SELECT s.body FROM message_search_index s JOIN messages m ON m.id=s.rowid WHERE m.client_message_id=?", [f"inline-upload-{name}"]).fetchone()[0]


def message_id(database, name):
    with sqlite3.connect(database) as db:
        return db.execute("SELECT id FROM messages WHERE client_message_id=?", [f"inline-upload-{name}"]).fetchone()[0]


def embedded_ids(database, message_id, campfire):
    with sqlite3.connect(database) as db:
        if campfire:
            return [row[0] for row in db.execute("""SELECT a.blob_id FROM active_storage_attachments a
                JOIN action_text_rich_texts t ON t.id=a.record_id AND a.record_type='ActionText::RichText'
                WHERE t.record_type='Message' AND t.record_id=? AND a.name='embeds' ORDER BY a.blob_id""", [message_id])]
        return [row[0] for row in db.execute("SELECT blob_id FROM inline_embeds WHERE message_id=? ORDER BY blob_id", [message_id])]


def edit(port, cookie, csrf, message_id, body):
    fields = urllib.parse.urlencode({"_method": "patch", "message[body]": body, "authenticity_token": csrf})
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    try:
        connection.request("POST", f"/rooms/1/messages/{message_id}", fields, {"Cookie": cookie,
                           "Content-Type": "application/x-www-form-urlencoded", "Accept": "text/html"})
        response = connection.getresponse()
        response.read()
        return response.status, urllib.parse.urlsplit(response.getheader("Location") or "").path
    finally:
        connection.close()


def get_presentation(port, cookie, name, message_id):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    try:
        connection.request("GET", f"/rooms/1/messages/{message_id}", headers={"Cookie": cookie})
        response = connection.getresponse()
        payload = response.read()
        assert response.status == 200, (message_id, response.status, payload[:200])
        parsed = Presentation(f"inline-upload-{name}")
        parsed.feed(payload.decode())
        assert parsed.structure, (message_id, payload[:200])
        return normalized(parsed.structure)
    finally:
        connection.close()


def workflow(port, cookie, csrf, database, upload_root, campfire):
    results = {}
    uploads = {}
    for name, filename, content_type, data in CASES:
        metadata = upload(port, cookie, csrf, filename, content_type, data)
        uploads[name] = metadata
        status, structure, payload = post(port, cookie, csrf, name, metadata)
        assert status == 200 and structure, (name, status, payload[:500])
        stored_blob(database, campfire, upload_root, metadata["id"], data)
        results[name] = normalized(structure), image_previews(port, cookie, structure) if name == "image" else None, search_text(database, name)
    image = uploads["image"]
    image_data = CASES[1][3]
    status, structure, payload = post(port, cookie, csrf, "image-copy", image)
    assert status == 200 and structure, ("image-copy", status, payload[:500])
    image_path = stored_blob(database, campfire, upload_root, image["id"], image_data, expected_links=2)
    results["image_copy"] = normalized(structure), image_previews(port, cookie, structure), search_text(database, "image-copy")
    image_id = message_id(database, "image")
    image_copy_id = message_id(database, "image-copy")
    status, location = edit(port, cookie, csrf, image_id, "<div>Original image removed</div>")
    assert (status, location) == (302, f"/rooms/1/messages/{image_id}"), (status, location)
    assert embedded_ids(database, image_id, campfire) == []
    assert embedded_ids(database, image_copy_id, campfire) == [image["id"]]
    stored_blob(database, campfire, upload_root, image["id"], image_data)
    results["image_copy_after_first_remove"] = get_presentation(port, cookie, "image-copy", image_copy_id)
    status, location = edit(port, cookie, csrf, image_copy_id, "<div>Copied image removed</div>")
    assert (status, location) == (302, f"/rooms/1/messages/{image_copy_id}"), (status, location)
    assert embedded_ids(database, image_copy_id, campfire) == []
    if not campfire:
        assert not image_path.exists(), image_path
    gallery_a = upload(port, cookie, csrf, "gallery-a.jpg", "image/jpeg", image_data)
    gallery_b = upload(port, cookie, csrf, "gallery-b.jpg", "image/jpeg", image_data)
    gallery_attachments = "".join(
        f'<action-text-attachment sgid="{blob["attachable_sgid"]}" content-type="image/jpeg" filename="{blob["filename"]}" presentation="gallery"></action-text-attachment>'
        for blob in (gallery_a, gallery_b))
    status, structure, payload = post(port, cookie, csrf, "gallery", gallery_a, f"<div>{gallery_attachments}</div>")
    assert status == 200 and structure, ("gallery", status, payload[:500])
    stored_blob(database, campfire, upload_root, gallery_a["id"], image_data)
    gallery_b_path = stored_blob(database, campfire, upload_root, gallery_b["id"], image_data)
    results["gallery"] = normalized(structure), image_previews(port, cookie, structure, 2), search_text(database, "gallery")
    gallery_id = message_id(database, "gallery")
    single = f'<div><action-text-attachment sgid="{gallery_a["attachable_sgid"]}" content-type="image/jpeg" filename="gallery-a.jpg" presentation="gallery"></action-text-attachment></div>'
    status, location = edit(port, cookie, csrf, gallery_id, single)
    assert (status, location) == (302, f"/rooms/1/messages/{gallery_id}"), (status, location)
    assert embedded_ids(database, gallery_id, campfire) == [gallery_a["id"]]
    gallery_a_path = stored_blob(database, campfire, upload_root, gallery_a["id"], image_data)
    if not campfire:
        assert not gallery_b_path.exists(), gallery_b_path
    results["gallery_single"] = get_presentation(port, cookie, "gallery", gallery_id), search_text(database, "gallery")
    status, location = edit(port, cookie, csrf, gallery_id, "<div>Gallery removed</div>")
    assert (status, location) == (302, f"/rooms/1/messages/{gallery_id}"), (status, location)
    assert embedded_ids(database, gallery_id, campfire) == []
    results["gallery_removed"] = get_presentation(port, cookie, "gallery", gallery_id), search_text(database, "gallery")
    if not campfire:
        assert not gallery_a_path.exists(), gallery_a_path
    text_id = message_id(database, "text")
    changed_data = b"Revised inline note.\n"
    changed = upload(port, cookie, csrf, "changed.txt", "text/plain", changed_data)
    attachment = f'<action-text-attachment sgid="{changed["attachable_sgid"]}" content-type="text/plain" filename="changed.txt"></action-text-attachment>'
    status, location = edit(port, cookie, csrf, text_id, f"<div>Edited {attachment}</div>")
    assert (status, location) == (302, f"/rooms/1/messages/{text_id}"), (status, location)
    changed_path = stored_blob(database, campfire, upload_root, changed["id"], changed_data)
    assert embedded_ids(database, text_id, campfire) == [changed["id"]]
    results["edited_search"] = search_text(database, "text")
    results["edited_presentation"] = get_presentation(port, cookie, "text", text_id)
    status, location = edit(port, cookie, csrf, text_id, "<div>Edited plain</div>")
    assert (status, location) == (302, f"/rooms/1/messages/{text_id}"), (status, location)
    assert embedded_ids(database, text_id, campfire) == []
    results["removed_search"] = search_text(database, "text")
    results["removed_presentation"] = get_presentation(port, cookie, "text", text_id)
    if not campfire:
        assert not changed_path.exists(), changed_path
    return results


def main():
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPOSITORY, text=True).strip() == REVISION
    with tempfile.TemporaryDirectory(prefix="paired-inline-upload-") as scratch:
        temp = pathlib.Path(scratch)
        rust_db, camp_db = temp / "rust.sqlite3", temp / "camp.sqlite3"
        rust_port, camp_port, redis_port = free_port(), free_port(), free_port()
        seed_rustfire(rust_db, rust_port, [])
        camp_env = seed_campfire(REPOSITORY, RUBY, BUNDLE, REPOSITORY / "storage/db/production.sqlite3", camp_db, [], camp_port, temp)
        camp_env["REDIS_URL"] = f"redis://127.0.0.1:{redis_port}"
        with sqlite3.connect(camp_db) as camp, sqlite3.connect(rust_db) as rust:
            camp.execute("DELETE FROM sqlite_sequence WHERE name='messages'")
            rust.executemany("UPDATE users SET name=?2,updated_at=?3 WHERE id=?1", camp.execute("SELECT id,name,updated_at FROM users WHERE id IN(1,2)").fetchall())
            rust.execute("UPDATE rooms SET name=? WHERE id=1", [camp.execute("SELECT name FROM rooms WHERE id=1").fetchone()[0]])
        redis, redis_log = start_redis(temp, redis_port)
        try:
            rust = start_server(rust_db, rust_port, {"RUSTFIRE_UPLOAD_DIR": str(temp / "uploads")})
            try:
                rust_results = workflow(rust_port, "session_token=benchmark-session", "benchmark-csrf", rust_db, temp / "uploads", False)
            finally:
                stop_server(rust)
            with open(temp / "puma.log", "w+") as log:
                camp = subprocess.Popen([str(RUBY), str(RUBY.parent / "bundle"), "exec", "puma", "-C", "config/puma.rb"],
                                        cwd=REPOSITORY, env=camp_env, stdout=log, stderr=log)
                try:
                    wait_for_server(camp_port, camp)
                    cookie, csrf = login_campfire(camp_port)
                    camp_results = workflow(camp_port, cookie, csrf, camp_db, REPOSITORY / "storage/files", True)
                except Exception:
                    log.flush()
                    log.seek(0)
                    print(log.read()[-3000:])
                    raise
                finally:
                    stop_server(camp)
            assert rust_results == camp_results, (rust_results, camp_results)
            print("PASS paired inline text/JPEG uploads, gallery variation and bytes, search, edit add/remove, and shared-blob retention")
        finally:
            redis.terminate()
            redis.wait(timeout=10)
            redis_log.close()
            cleanup_campfire_uploads(REPOSITORY / "storage/db/production.sqlite3", camp_db, REPOSITORY)


if __name__ == "__main__":
    main()
