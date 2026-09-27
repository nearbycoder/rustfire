"""Check that Rustfire resumes queued attachment analysis and purge after a restart."""

import hashlib
import http.client
import pathlib
import sqlite3
import tempfile
import time

from direct_lookup import free_port, start_server, stop_server
from paired_direct_lookup import seed_rustfire
from paired_message_parameter_edges import IMAGE, OLD_FILE, OLD_RUST_STORED, multipart_body


def state(database, old_file):
    with sqlite3.connect(database) as db:
        attachment = db.execute("SELECT id,width,height,stored_name FROM attachments WHERE message_id=1").fetchone()
        archived = db.execute("SELECT COUNT(*) FROM replaced_attachments").fetchone()[0]
        jobs = db.execute("SELECT COUNT(*) FROM attachment_jobs").fetchone()[0]
    return attachment, archived, jobs, old_file.exists()


def main():
    with tempfile.TemporaryDirectory(prefix="attachment-jobs-restart-") as scratch:
        temp = pathlib.Path(scratch)
        database, uploads = temp / "rust.sqlite3", temp / "uploads"
        seed_rustfire(database, free_port(), [])
        with sqlite3.connect(database) as db:
            db.execute("INSERT INTO messages(id,room_id,creator_id,body,client_message_id,created_at,updated_at) VALUES(1,1,1,'Initial','restart-fixture','2026-01-01T00:00:00Z','2026-01-01T00:00:00Z')")
            db.execute("INSERT INTO attachments(id,message_id,filename,content_type,stored_name,created_at) VALUES(1,1,'old.txt','text/plain',?,'2026-01-01T00:00:00Z')", [OLD_RUST_STORED])
        uploads.mkdir()
        old_file = uploads / OLD_RUST_STORED
        old_file.write_bytes(OLD_FILE)
        port = free_port()
        process = start_server(database, port, {"RUSTFIRE_UPLOAD_DIR": str(uploads)})
        try:
            body, content_type = multipart_body((("message[attachment]", IMAGE.read_bytes(), "moon.jpg", "image/jpeg"),))
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
            try:
                connection.request("PATCH", "/rooms/1/messages/1", body, {
                    "Cookie": "session_token=benchmark-session", "X-CSRF-Token": "benchmark-csrf",
                    "Content-Type": content_type, "Accept": "text/html",
                })
                response = connection.getresponse()
                response.read()
                assert response.status == 302, response.status
            finally:
                connection.close()
            with sqlite3.connect(database) as db:
                db.execute("UPDATE attachment_jobs SET available_at_ms=?", [int(time.time() * 1000) + 10_000])
            before = state(database, old_file)
            assert before[0][:3] == (2, None, None) and before[1:] == (1, 2, True), before
        finally:
            stop_server(process)
        assert state(database, old_file) == before, "queued work changed while Rustfire was stopped"
        with sqlite3.connect(database) as db:
            db.execute("UPDATE attachment_jobs SET available_at_ms=?", [int(time.time() * 1000) - 1])
        process = start_server(database, port, {"RUSTFIRE_UPLOAD_DIR": str(uploads)})
        try:
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                after = state(database, old_file)
                if after[0][:3] == (2, 640.0, 640.0) and after[1:] == (0, 0, False):
                    break
                time.sleep(.05)
            else:
                raise AssertionError(("queued work did not resume", after))
            original = uploads / after[0][3]
            assert hashlib.sha256(original.read_bytes()).digest() == hashlib.sha256(IMAGE.read_bytes()).digest()
        finally:
            stop_server(process)
    print("PASS queued attachment analysis and purge resume after Rustfire restart")


if __name__ == "__main__":
    main()
