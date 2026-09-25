"""Check that pending banned-content cleanup resumes after a server restart."""

import pathlib
import sqlite3
import sys
import tempfile
import time
import uuid

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "bench"))
from direct_lookup import free_port, start_server, stop_server
from paired_direct_lookup import seed_rustfire


def main():
    with tempfile.TemporaryDirectory(prefix="rustfire-background-jobs-") as temporary:
        temp = pathlib.Path(temporary)
        database = temp / "rustfire.sqlite3"
        uploads = temp / "uploads"
        uploads.mkdir()
        port = free_port()
        seed_rustfire(database, port, [])
        stamp = "2026-01-01T00:00:00Z"
        attachment_name, inline_name = str(uuid.uuid4()), str(uuid.uuid4())
        (uploads / attachment_name).write_bytes(b"attachment fixture")
        (uploads / inline_name).write_bytes(b"inline fixture")
        with sqlite3.connect(database) as db:
            db.executemany(
                "INSERT INTO messages(id,room_id,creator_id,body,client_message_id,created_at,updated_at) VALUES(?1,1,2,?2,?3,?4,?4)",
                ((index, f"Banned message {index}", f"banned-{index}", stamp) for index in range(1, 56)),
            )
            db.execute(
                "INSERT INTO attachments(message_id,filename,content_type,stored_name,created_at) VALUES(1,'file.txt','text/plain',?1,?2)",
                (attachment_name, stamp),
            )
            db.execute(
                "INSERT INTO inline_blobs(id,filename,content_type,stored_name,byte_size,created_at) VALUES(1,'inline.txt','text/plain',?1,14,?2)",
                (inline_name, stamp),
            )
            db.execute("INSERT INTO inline_embeds(message_id,blob_id) VALUES(2,1)")
            db.execute(
                "INSERT INTO background_jobs(kind,user_id,created_at) VALUES('remove_banned_content',2,?1)",
                (stamp,),
            )
        process = start_server(database, port, {"RUSTFIRE_UPLOAD_DIR": str(uploads)})
        try:
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                with sqlite3.connect(database) as db:
                    counts = tuple(
                        db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                        for table in ("background_jobs", "messages", "attachments", "inline_blobs", "inline_embeds")
                    )
                if counts == (0, 0, 0, 0, 0) and not (uploads / attachment_name).exists() and not (uploads / inline_name).exists():
                    break
                time.sleep(.05)
            else:
                raise AssertionError((counts, (uploads / attachment_name).exists(), (uploads / inline_name).exists()))
        finally:
            stop_server(process)
        print("PASS queued 55-message ban cleanup resumes on startup, spans batches, and purges attachment and inline files")


if __name__ == "__main__":
    main()
