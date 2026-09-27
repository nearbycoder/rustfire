"""Compare queued attachment analysis and purge after Campfire and Rustfire message edits."""

import json
import http.client
import os
import pathlib
import re
import signal
import sqlite3
import subprocess
import tempfile
import time
import urllib.error
import urllib.request

from direct_lookup import free_port
from paired_attachment_mime import presentation
from paired_banned_content import BUNDLE, REPOSITORY, REVISION, RUBY, isolated_campfire, start_redis, wait_for_worker
from paired_direct_lookup import seed_campfire, seed_rustfire
from paired_message_parameter_edges import IMAGE, VIDEO, OLD_CAMP_KEY, OLD_FILE, OLD_RUST_STORED, multipart_body, seed_existing_attachment
from paired_turbo_fanout import seed_boost_message
from paired_valid_write_route_inventory import run_case


CASES = (
    ("text", "new.txt", "text/plain", b"replacement text\n"),
    ("JPEG", "moon.jpg", "image/jpeg", IMAGE.read_bytes()),
    ("QuickTime", "alpha-centuri.mov", "video/quicktime", VIDEO.read_bytes()),
    ("malformed JPEG", "notes.txt", "image/jpeg", b"A plain text attachment.\n"),
)


def get(port, cookie, path):
    request = urllib.request.Request(f"http://127.0.0.1:{port}{path}", headers={"Cookie": cookie})
    try:
        response = urllib.request.urlopen(request, timeout=30)
    except urllib.error.HTTPError as error:
        response = error
    with response:
        return response.status, response.read()


def blob_path(port, cookie, filename):
    status, page = get(port, cookie, "/rooms/1/messages/1")
    assert status == 200, status
    match = re.search(rb"/rails/active_storage/blobs/redirect/[^'\"<>\s]+/" + re.escape(filename.encode()), page)
    assert match, page[-1000:]
    return match.group(0).decode()


def csrf_token(port, cookie):
    status, page = get(port, cookie, "/rooms/1")
    assert status == 200, status
    match = re.search(rb"<meta name=['\"]csrf-token['\"] content=['\"]([^'\"]+)", page)
    assert match, page[:1000]
    return match.group(1).decode()


def edit_attachment(port, cookie, csrf, filename, content_type, data):
    body, form_type = multipart_body((("message[attachment]", data, filename, content_type),))
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    try:
        connection.request("PATCH", "/rooms/1/messages/1", body, {
            "Cookie": cookie, "X-CSRF-Token": csrf, "Content-Type": form_type, "Accept": "text/html",
        })
        response = connection.getresponse()
        response.read()
        return response.status, response.getheader("Location")
    finally:
        connection.close()


def rust_state(database, old_file):
    with sqlite3.connect(database) as db:
        new = db.execute("SELECT id,width,height FROM attachments WHERE message_id=1").fetchone()
        archived = db.execute("SELECT count(*) FROM replaced_attachments WHERE id=1").fetchone()[0]
        jobs = db.execute("SELECT kind FROM attachment_jobs ORDER BY id").fetchall()
    return new, archived, tuple(kind for (kind,) in jobs), old_file.exists()


def camp_state(database, old_file):
    with sqlite3.connect(database) as db:
        rows = db.execute("SELECT id,metadata FROM active_storage_blobs ORDER BY id").fetchall()
    return tuple((identifier, json.loads(metadata)) for identifier, metadata in rows), old_file.exists()


def normalized_page(port, cookie):
    status, page = get(port, cookie, "/rooms/1/messages/1")
    assert status == 200, status
    return tuple(presentation(page, "boost-fixture"))


def main():
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPOSITORY, text=True).strip() == REVISION
    with tempfile.TemporaryDirectory(prefix="paired-message-edit-jobs-") as scratch:
        temp = pathlib.Path(scratch)
        rust_base, camp_base = temp / "rust-base.sqlite3", temp / "camp-base.sqlite3"
        seed_rustfire(rust_base, free_port(), [])
        env = seed_campfire(REPOSITORY, RUBY, BUNDLE, REPOSITORY / "storage/db/production.sqlite3", camp_base, [], free_port(), temp)
        seed_boost_message(rust_base, camp_base)
        seed_existing_attachment(rust_base, camp_base)
        with sqlite3.connect(camp_base) as db:
            db.execute("DELETE FROM sessions")
        redis_port = free_port()
        checkout = isolated_campfire(temp, redis_port)
        env["REDIS_URL"] = f"redis://127.0.0.1:{redis_port}"
        redis, redis_log = start_redis(temp, redis_port)
        try:
            for index, (label, filename, content_type, data) in enumerate(CASES):
                rust_old = temp / f"rust-uploads-{index}" / OLD_RUST_STORED
                rust_old.parent.mkdir(parents=True, exist_ok=True)
                rust_old.write_bytes(OLD_FILE)
                camp_old = checkout / "storage/files" / OLD_CAMP_KEY[:2] / OLD_CAMP_KEY[2:4] / OLD_CAMP_KEY
                camp_old.parent.mkdir(parents=True, exist_ok=True)
                camp_old.write_bytes(OLD_FILE)
                old_paths = {}
                rust_db, camp_db = temp / f"rust-{index}.sqlite3", temp / f"camp-{index}.sqlite3"

                def before(rust_port, rust_cookie, camp_port, camp_cookie):
                    old_paths["rust"] = blob_path(rust_port, rust_cookie, "old.txt")
                    old_paths["camp"] = blob_path(camp_port, camp_cookie, "old.txt")

                def after(rust_port, rust_cookie, camp_port, camp_cookie):
                    rust_before, camp_before = rust_state(rust_db, rust_old), camp_state(camp_db, camp_old)
                    assert rust_before[0][0] == 2 and rust_before[0][1:] == (None, None), (label, rust_before)
                    assert rust_before[1] == 1 and rust_before[3], (label, rust_before)
                    assert [row[0] for row in camp_before[0]] == [1, 2] and camp_before[1], (label, camp_before)
                    assert get(rust_port, rust_cookie, old_paths["rust"]) == (200, OLD_FILE)
                    assert get(camp_port, camp_cookie, old_paths["camp"]) == (200, OLD_FILE)
                    worker_env = dict(env, DATABASE_URL=f"sqlite3:{camp_db}", PORT=str(camp_port), QUEUE="default", INTERVAL="0.1")
                    with open(temp / f"worker-{index}.log", "w+") as worker_log:
                        worker = subprocess.Popen(
                            [str(RUBY), str(RUBY.parent / "bundle"), "exec", "rake", "resque:work"],
                            cwd=checkout, env=worker_env, stdout=worker_log, stderr=worker_log, start_new_session=True,
                        )
                        try:
                            wait_for_worker(redis_port, worker)
                            deadline = time.monotonic() + 15
                            while time.monotonic() < deadline:
                                rust_after, camp_after = rust_state(rust_db, rust_old), camp_state(camp_db, camp_old)
                                if not rust_after[2] and rust_after[1] == 0 and not rust_after[3] and len(camp_after[0]) == 1 and not camp_after[1] and camp_after[0][0][1].get("analyzed"):
                                    break
                                time.sleep(.05)
                            else:
                                raise AssertionError((label, "queued jobs did not finish", rust_after, camp_after))
                            if label == "JPEG":
                                with sqlite3.connect(rust_db) as db:
                                    rust_key = db.execute("SELECT stored_name FROM attachments WHERE id=2").fetchone()[0]
                                rust_jpeg = rust_old.parent / rust_key
                                with sqlite3.connect(camp_db) as db:
                                    camp_key = db.execute("SELECT key FROM active_storage_blobs WHERE id=2").fetchone()[0]
                                camp_jpeg = checkout / "storage/files" / camp_key[:2] / camp_key[2:4] / camp_key
                                jpeg_rust_url = blob_path(rust_port, rust_cookie, "moon.jpg")
                                jpeg_camp_url = blob_path(camp_port, camp_cookie, "moon.jpg")
                                assert get(rust_port, rust_cookie, jpeg_rust_url) == (200, IMAGE.read_bytes())
                                assert get(camp_port, camp_cookie, jpeg_camp_url) == (200, IMAGE.read_bytes())
                                second = (
                                    edit_attachment(rust_port, rust_cookie, "benchmark-csrf", "second.txt", "text/plain", b"second replacement\n"),
                                    edit_attachment(camp_port, camp_cookie, csrf_token(camp_port, camp_cookie), "second.txt", "text/plain", b"second replacement\n"),
                                )
                                assert second[0][0] == second[1][0] == 302, second
                                deadline = time.monotonic() + 15
                                while time.monotonic() < deadline:
                                    with sqlite3.connect(rust_db) as db:
                                        rust_second = (db.execute("SELECT id FROM attachments WHERE message_id=1").fetchone()[0],
                                                       db.execute("SELECT count(*) FROM replaced_attachments").fetchone()[0],
                                                       db.execute("SELECT count(*) FROM attachment_jobs").fetchone()[0])
                                    with sqlite3.connect(camp_db) as db:
                                        camp_second = db.execute("SELECT id FROM active_storage_blobs ORDER BY id").fetchall()
                                    if rust_second == (3, 0, 0) and camp_second == [(3,)] and not rust_jpeg.exists() and not camp_jpeg.exists():
                                        break
                                    time.sleep(.05)
                                else:
                                    raise AssertionError(("second replacement did not purge", rust_second, camp_second))
                                assert get(rust_port, rust_cookie, jpeg_rust_url)[0] == 404
                                assert get(camp_port, camp_cookie, jpeg_camp_url)[0] == 404
                                assert normalized_page(rust_port, rust_cookie) == normalized_page(camp_port, camp_cookie)
                        finally:
                            if worker.poll() is None:
                                os.killpg(worker.pid, signal.SIGTERM)
                            worker.wait(timeout=10)
                    rust_dims = rust_after[0][1:]
                    camp_metadata = camp_after[0][0][1]
                    camp_dims = camp_metadata.get("width"), camp_metadata.get("height")
                    assert rust_dims == camp_dims, (label, rust_dims, camp_metadata)
                    assert get(rust_port, rust_cookie, old_paths["rust"])[0] == 404
                    assert get(camp_port, camp_cookie, old_paths["camp"])[0] == 404
                    rust_page = normalized_page(rust_port, rust_cookie)
                    camp_page = normalized_page(camp_port, camp_cookie)
                    return (rust_dims, rust_page), (camp_dims, camp_page)

                body, form_type = multipart_body((("message[attachment]", data, filename, content_type),))
                rust_result, camp_result = run_case(
                    ("PATCH", "/rooms/1/messages/1", "text/html"), index, temp,
                    rust_base, camp_base, checkout, env, redis_port, body=body, content_type=form_type,
                    before_request=before, after_request=after,
                )
                assert rust_result == camp_result, (label, rust_result, camp_result)
                print(f"PASS {label} delayed analysis/purge and old signed blob lifecycle", flush=True)
        finally:
            redis.terminate()
            redis.wait(timeout=10)
            redis_log.close()


if __name__ == "__main__":
    main()
