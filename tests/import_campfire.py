#!/usr/bin/env python3
"""Exercise the offline importer against a disposable copy of the pinned Campfire DB."""

import base64
import hashlib
import hmac
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.request


SOURCE = Path(os.environ.get("CAMPFIRE_TEST_DB", "/tmp/once-campfire-reference/storage/db/production.sqlite3"))


def main():
    if not SOURCE.exists():
        raise SystemExit(f"pinned Campfire test database missing: {SOURCE}")
    with tempfile.TemporaryDirectory(prefix="rustfire-import-test-") as temporary:
        root = Path(temporary)
        source_db = root / "campfire.sqlite3"
        source_files = root / "campfire-files"
        source_files.mkdir()
        with sqlite3.connect(f"{SOURCE.resolve().as_uri()}?mode=ro", uri=True) as original:
            with sqlite3.connect(source_db) as fixture:
                original.backup(fixture)
        with sqlite3.connect(source_db) as fixture:
            fixture.execute("PRAGMA foreign_keys=OFF")
            for table in ("active_storage_variant_records", "active_storage_attachments", "active_storage_blobs", "boosts", "action_text_rich_texts", "messages", "message_search_index", "searches", "sessions", "bans", "push_subscriptions", "webhooks"):
                fixture.execute(f"DELETE FROM {table}")
            fixture.execute("UPDATE accounts SET settings=?,custom_styles=?", (json.dumps({"restrict_room_creation_to_administrators": True}), "body { color: navy; }"))
            fixture.execute("UPDATE rooms SET name='Imported room' WHERE id=1")
            fixture.execute("INSERT INTO sessions(id,user_id,token,created_at,updated_at,last_active_at,user_agent) VALUES(1,1,'imported-session','2026-01-01 00:00:00','2026-01-01 00:00:00','2026-01-01 00:00:00','test')")
            fixture.execute("""INSERT INTO push_subscriptions(id,user_id,endpoint,p256dh_key,auth_key,user_agent,created_at,updated_at)
                VALUES(1,1,'https://push.example.test/1','test-p256dh','test-auth','test','2026-01-01 00:00:00','2026-01-01 00:00:00')""")
            for mid in (1, 2, 3):
                fixture.execute("INSERT INTO messages(id,room_id,creator_id,client_message_id,created_at,updated_at) VALUES(?,1,1,?,?,?)", (mid, f"imported-{mid}", "2026-01-01 00:00:00.000000", "2026-01-01 00:00:00.000000"))
            source_body = "<div>Hello</div><ul><li>One</li><li>Two</li></ul>"
            fixture.execute("INSERT INTO action_text_rich_texts(id,record_type,record_id,name,body,created_at,updated_at) VALUES(1,'Message',1,'body',?,?,?)", (source_body, "2026-01-01 00:00:00", "2026-01-01 00:00:00"))
            fixture.execute("INSERT INTO message_search_index(rowid,body) VALUES(1,?)", ("Hello\n• One\n• Two",))
            fixture.execute("INSERT INTO message_search_index(rowid,body) VALUES(2,?)", ("imported.txt",))
            secret = "0123456789abcdef" * 4
            signing_key = hashlib.pbkdf2_hmac("sha256", secret.encode(), b"signed_global_ids", 1000, 64)
            mention_payload = b'{"_rails":{"data":"gid://campfire/User/2?expires_in","pur":"attachable"}}'
            encoded = base64.urlsafe_b64encode(mention_payload).decode()
            sgid = f"{encoded}--{hmac.new(signing_key, encoded.encode(), hashlib.sha1).hexdigest()}"
            mention_body = f'<div><action-text-attachment sgid="{sgid}" content-type="application/vnd.campfire.mention"></action-text-attachment> hello</div>'
            fixture.execute("INSERT INTO action_text_rich_texts(id,record_type,record_id,name,body,created_at,updated_at) VALUES(2,'Message',3,'body',?,?,?)", (mention_body, "2026-01-01 00:00:00", "2026-01-01 00:00:00"))
            fixture.execute("INSERT INTO message_search_index(rowid,body) VALUES(3,?)", ("@Rustfire Compare hello",))
            fixture.execute("INSERT INTO boosts(id,message_id,booster_id,content,created_at,updated_at) VALUES(1,1,2,'Great','2026-01-01 00:00:00','2026-01-01 00:00:00')")
            fixture.execute("""INSERT INTO searches(id,user_id,query,created_at,updated_at)
                VALUES(1,1,'One','2025-01-01 00:00:00','2026-01-02 00:00:00')""")
            file_bytes = b"imported file bytes\n"
            key = "abcdef1234567890"
            path = source_files / key[:2] / key[2:4] / key
            path.parent.mkdir(parents=True)
            path.write_bytes(file_bytes)
            fixture.execute("""INSERT INTO active_storage_blobs(id,key,filename,content_type,metadata,service_name,byte_size,created_at)
                VALUES(7,?,'imported.txt','text/plain','{}','local',?,'2026-01-01 00:00:00')""", (key, len(file_bytes)))
            fixture.execute("""INSERT INTO active_storage_attachments(id,name,record_type,record_id,blob_id,created_at)
                VALUES(7,'attachment','Message',2,7,'2026-01-01 00:00:00')""")
            for blob_id, record_type, record_id, name in ((8, "User", 1, "avatar"), (9, "Account", 1, "logo")):
                media_key = f"ab{blob_id}cdef1234567890"
                media_file = source_files / media_key[:2] / media_key[2:4] / media_key
                media_file.parent.mkdir(parents=True, exist_ok=True)
                media_file.write_bytes(file_bytes)
                fixture.execute("""INSERT INTO active_storage_blobs(id,key,filename,content_type,metadata,service_name,byte_size,created_at)
                    VALUES(?,?,'media.txt','text/plain','{}','local',?,'2026-01-01 00:00:00')""", (blob_id, media_key, len(file_bytes)))
                fixture.execute("""INSERT INTO active_storage_attachments(id,name,record_type,record_id,blob_id,created_at)
                    VALUES(?,?,?,?,?,'2026-01-01 00:00:00')""", (blob_id, name, record_type, record_id, blob_id))
            fixture.commit()
        target_db = root / "rustfire.sqlite3"
        target_uploads = root / "uploads"
        private_bytes = bytes(31) + b"\x01"
        generator_x = bytes.fromhex("6b17d1f2e12c4247f8bce6e563a440f277037d812deb33a0f4a13945d898c296")
        generator_y = bytes.fromhex("4fe342e2fe1a7f9b8ee7eb4a7c0f9e162bce33576b315ececbb6406837bf51f5")
        public_key = base64.urlsafe_b64encode(b"\x04" + generator_x + generator_y).rstrip(b"=").decode()
        private_key = base64.urlsafe_b64encode(private_bytes).rstrip(b"=").decode()
        environment = dict(os.environ, RUSTFIRE_CAMPFIRE_SECRET_KEY_BASE=secret,
            RUSTFIRE_CAMPFIRE_VAPID_PRIVATE_KEY=private_key, RUSTFIRE_CAMPFIRE_VAPID_PUBLIC_KEY=public_key)
        command = [sys.executable, "tools/import_campfire.py", "--source-db", str(source_db), "--source-files", str(source_files), "--target-db", str(target_db), "--target-uploads", str(target_uploads), "--rustfire-bin", "target/debug/rustfire"]
        completed = subprocess.run(command, env=environment, capture_output=True, text=True, check=True)
        result = json.loads(completed.stdout)
        assert result["messages"] == 3 and result["attachments"] == 1, result
        assert result["push_subscriptions"] == 1
        assert target_db.with_suffix(".vapid.der").is_file()
        with sqlite3.connect(target_db) as imported:
            assert imported.execute("PRAGMA foreign_key_check").fetchall() == []
            assert imported.execute("SELECT body,body_source,body_html FROM messages WHERE id=1").fetchone() == (
                "Hello\n• One\n• Two", source_body, source_body,
            )
            assert imported.execute("SELECT body FROM messages WHERE id=2").fetchone() == ("",)
            assert imported.execute("SELECT body FROM message_search_index WHERE rowid=2").fetchone() == ("imported.txt",)
            assert imported.execute("SELECT message_id,user_id FROM message_mentions").fetchall() == [(3, 2)]
            assert "@Rustfire Compare" in imported.execute("SELECT body_html FROM messages WHERE id=3").fetchone()[0]
            assert imported.execute("SELECT id FROM attachments WHERE message_id=2").fetchone() == (7,)
            stored = imported.execute("SELECT stored_name FROM attachments WHERE id=7").fetchone()[0]
            assert (target_uploads / stored).read_bytes() == file_bytes
            assert imported.execute("SELECT count(*) FROM boosts").fetchone() == (1,)
            assert imported.execute("SELECT query,created_at FROM searches WHERE id=1").fetchone() == ("One", "2026-01-02 00:00:00")
            assert imported.execute("SELECT restrict_room_creation FROM account_settings").fetchone() == (1,)
            assert imported.execute("SELECT css FROM account_custom_styles").fetchone() == ("body { color: navy; }",)
            assert imported.execute("SELECT token FROM sessions WHERE id=1").fetchone() == ("imported-session",)
            for table in ("avatars", "account_logos"):
                media = imported.execute(f"SELECT stored_name FROM {table}").fetchone()[0]
                assert (target_uploads / media).read_bytes() == file_bytes
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        server_env = dict(environment, RUSTFIRE_DB=str(target_db), RUSTFIRE_UPLOAD_DIR=str(target_uploads), RUSTFIRE_ADDR=f"127.0.0.1:{port}")
        server = subprocess.Popen(("target/debug/rustfire",), env=server_env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            for _ in range(100):
                try:
                    request = urllib.request.Request(f"http://127.0.0.1:{port}/rooms/1", headers={"Cookie": "session_token=imported-session"})
                    with urllib.request.urlopen(request, timeout=1) as response:
                        page = response.read().decode()
                    break
                except Exception:
                    time.sleep(.05)
            else:
                raise AssertionError("imported Rustfire server did not serve the room")
            assert "Imported room" in page and "Hello" in page and "imported.txt" in page
            assert public_key in page, page[:800]
        finally:
            server.terminate()
            server.wait(timeout=5)
        with sqlite3.connect(source_db) as fixture:
            fixture.execute("""INSERT INTO active_storage_attachments(id,name,record_type,record_id,blob_id,created_at)
                VALUES(10,'embeds','ActionText::RichText',1,7,'2026-01-01 00:00:00')""")
        bad_target = root / "must-not-exist.sqlite3"
        bad_uploads = root / "must-not-exist-uploads"
        bad_command = command.copy()
        bad_command[bad_command.index("--target-db") + 1] = str(bad_target)
        bad_command[bad_command.index("--target-uploads") + 1] = str(bad_uploads)
        failure = subprocess.run(bad_command, env=environment, capture_output=True, text=True)
        assert failure.returncode != 0 and not bad_target.exists() and not bad_uploads.exists()
        print("PASS Campfire account, users, room, rich messages, search text, boost, session, push key, file, avatar, and logo import; inline embeds fail safely")


if __name__ == "__main__":
    main()
