#!/usr/bin/env python3
"""Exercise the offline importer against a disposable copy of the pinned Campfire DB."""

import base64
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request


SOURCE = Path(os.environ.get("CAMPFIRE_TEST_DB", "/tmp/once-campfire-reference/storage/db/production.sqlite3"))


def minimal_pdf():
    parts = [b"%PDF-1.4\n"]
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 72 72] /Contents 4 0 R /Resources << >> >>",
        b"<< /Length 0 >>\nstream\n\nendstream",
    ]
    offsets = []
    for index, value in enumerate(objects, 1):
        offsets.append(sum(map(len, parts)))
        parts.append(f"{index} 0 obj\n".encode() + value + b"\nendobj\n")
    xref = sum(map(len, parts))
    parts.append(b"xref\n0 5\n0000000000 65535 f \n" + b"".join(f"{offset:010d} 00000 n \n".encode() for offset in offsets))
    parts.append(f"trailer\n<< /Size 5 /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    return b"".join(parts)


def variation_data(url):
    encoded = url.split("/")[6].split("--")[0]
    return json.loads(base64.b64decode(encoded))["_rails"]["data"]


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
            fixture.execute("INSERT INTO users(id,name,role,status,created_at,updated_at) VALUES(3,'Outside',0,0,'2026-01-01 00:00:00','2026-01-01 00:00:00')")
            fixture.execute("INSERT INTO sessions(id,user_id,token,created_at,updated_at,last_active_at,user_agent) VALUES(1,1,'imported-session','2026-01-01 00:00:00','2026-01-01 00:00:00','2026-01-01 00:00:00','test')")
            fixture.execute("""INSERT INTO push_subscriptions(id,user_id,endpoint,p256dh_key,auth_key,user_agent,created_at,updated_at)
                VALUES(1,1,'https://push.example.test/1','test-p256dh','test-auth','test','2026-01-01 00:00:00','2026-01-01 00:00:00')""")
            fixture.execute("""INSERT INTO push_subscriptions(id,user_id,endpoint,p256dh_key,auth_key,user_agent,created_at,updated_at)
                VALUES(2,1,'https://push.example.test/1','rotated-p256dh','rotated-auth','test','2026-01-01 00:00:00','2026-01-01 00:00:00')""")
            for mid in range(1, 12):
                fixture.execute("INSERT INTO messages(id,room_id,creator_id,client_message_id,created_at,updated_at) VALUES(?,1,1,?,?,?)", (mid, f"imported-{mid}", "2026-01-01 00:00:00.000000", "2026-01-01 00:00:00.000000"))
            fixture.execute("UPDATE sqlite_sequence SET seq=20 WHERE name='messages'")
            source_body = "<div>Hello</div><ul><li>One</li><li>Two</li></ul>"
            fixture.execute("INSERT INTO action_text_rich_texts(id,record_type,record_id,name,body,created_at,updated_at) VALUES(1,'Message',1,'body',?,?,?)", (source_body, "2026-01-01 00:00:00", "2026-01-01 00:00:00"))
            fixture.execute("INSERT INTO message_search_index(rowid,body) VALUES(1,?)", ("Hello\n• One\n• Two",))
            fixture.execute("INSERT INTO message_search_index(rowid,body) VALUES(2,?)", ("imported.txt",))
            secret = "0123456789abcdef" * 4
            signing_key = hashlib.pbkdf2_hmac("sha256", secret.encode(), b"signed_global_ids", 1000, 64)
            mention_payload = b'{"_rails":{"data":"gid://campfire/User/2?expires_in","pur":"attachable"}}'
            encoded = base64.urlsafe_b64encode(mention_payload).decode()
            sgid = f"{encoded}--{hmac.new(signing_key, encoded.encode(), hashlib.sha1).hexdigest()}"
            outsider_payload = b'{"_rails":{"data":"gid://campfire/User/3?expires_in","pur":"attachable"}}'
            outsider_encoded = base64.urlsafe_b64encode(outsider_payload).decode()
            outsider_sgid = f"{outsider_encoded}--{hmac.new(signing_key, outsider_encoded.encode(), hashlib.sha1).hexdigest()}"
            mention_body = f'<div><action-text-attachment sgid="{sgid}" content-type="application/vnd.campfire.mention"></action-text-attachment> <action-text-attachment sgid="{outsider_sgid}" content-type="application/vnd.campfire.mention"></action-text-attachment> hello</div>'
            fixture.execute("INSERT INTO action_text_rich_texts(id,record_type,record_id,name,body,created_at,updated_at) VALUES(2,'Message',3,'body',?,?,?)", (mention_body, "2026-01-01 00:00:00", "2026-01-01 00:00:00"))
            fixture.execute("INSERT INTO message_search_index(rowid,body) VALUES(3,?)", ("@Rustfire Compare @Outside hello",))
            blob_payload = b'{"_rails":{"data":"gid://campfire/ActiveStorage::Blob/10?expires_in","pur":"attachable"}}'
            blob_encoded = base64.urlsafe_b64encode(blob_payload).decode()
            blob_sgid = f"{blob_encoded}--{hmac.new(signing_key, blob_encoded.encode(), hashlib.sha1).hexdigest()}"
            inline_body = f'<div>Before <action-text-attachment sgid="{blob_sgid}" content-type="text/plain" filename="inline.txt" filesize="1234"></action-text-attachment> after</div>'
            fixture.execute("INSERT INTO action_text_rich_texts(id,record_type,record_id,name,body,created_at,updated_at) VALUES(3,'Message',4,'body',?,?,?)", (inline_body, "2026-01-01 00:00:00", "2026-01-01 00:00:00"))
            fixture.execute("INSERT INTO message_search_index(rowid,body) VALUES(4,?)", ("Before [inline.txt] after",))
            image_payload = b'{"_rails":{"data":"gid://campfire/ActiveStorage::Blob/11?expires_in","pur":"attachable"}}'
            image_encoded = base64.urlsafe_b64encode(image_payload).decode()
            image_sgid = f"{image_encoded}--{hmac.new(signing_key, image_encoded.encode(), hashlib.sha1).hexdigest()}"
            image_body = f'<div>Picture <action-text-attachment sgid="{image_sgid}" content-type="image/png" filename="pixel.png" filesize="68" width="1" height="1" previewable="true"></action-text-attachment> end</div>'
            fixture.execute("INSERT INTO action_text_rich_texts(id,record_type,record_id,name,body,created_at,updated_at) VALUES(4,'Message',5,'body',?,?,?)", (image_body, "2026-01-01 00:00:00", "2026-01-01 00:00:00"))
            fixture.execute("INSERT INTO message_search_index(rowid,body) VALUES(5,?)", ("Picture [pixel.png] end",))
            pdf_payload = b'{"_rails":{"data":"gid://campfire/ActiveStorage::Blob/12?expires_in","pur":"attachable"}}'
            pdf_encoded = base64.urlsafe_b64encode(pdf_payload).decode()
            pdf_sgid = f"{pdf_encoded}--{hmac.new(signing_key, pdf_encoded.encode(), hashlib.sha1).hexdigest()}"
            pdf_body = f'<div>Document <action-text-attachment sgid="{pdf_sgid}" content-type="application/pdf" filename="page.pdf"></action-text-attachment> end</div>'
            fixture.execute("INSERT INTO action_text_rich_texts(id,record_type,record_id,name,body,created_at,updated_at) VALUES(5,'Message',6,'body',?,?,?)", (pdf_body, "2026-01-01 00:00:00", "2026-01-01 00:00:00"))
            fixture.execute("INSERT INTO message_search_index(rowid,body) VALUES(6,?)", ("Document [page.pdf] end",))
            video_payload = b'{"_rails":{"data":"gid://campfire/ActiveStorage::Blob/13?expires_in","pur":"attachable"}}'
            video_encoded = base64.urlsafe_b64encode(video_payload).decode()
            video_sgid = f"{video_encoded}--{hmac.new(signing_key, video_encoded.encode(), hashlib.sha1).hexdigest()}"
            video_body = f'<div>Clip <action-text-attachment sgid="{video_sgid}" content-type="video/mp4" filename="clip.mp4" width="16" height="16" previewable="true"></action-text-attachment> end</div>'
            fixture.execute("INSERT INTO action_text_rich_texts(id,record_type,record_id,name,body,created_at,updated_at) VALUES(6,'Message',7,'body',?,?,?)", (video_body, "2026-01-01 00:00:00", "2026-01-01 00:00:00"))
            fixture.execute("INSERT INTO message_search_index(rowid,body) VALUES(7,?)", ("Clip [clip.mp4] end",))
            gallery_body = f'<div class="attachment-gallery attachment-gallery--3"><action-text-attachment sgid="{image_sgid}" content-type="image/png" filename="pixel.png"></action-text-attachment><action-text-attachment sgid="{pdf_sgid}" content-type="application/pdf" filename="page.pdf"></action-text-attachment><action-text-attachment sgid="{video_sgid}" content-type="video/mp4" filename="clip.mp4"></action-text-attachment></div>'
            fixture.execute("INSERT INTO action_text_rich_texts(id,record_type,record_id,name,body,created_at,updated_at) VALUES(7,'Message',8,'body',?,?,?)", (gallery_body, "2026-01-01 00:00:00", "2026-01-01 00:00:00"))
            fixture.execute("INSERT INTO message_search_index(rowid,body) VALUES(8,?)", ("[pixel.png] [page.pdf] [clip.mp4]",))
            for message_id, rich_id, blob_id, content_type, filename, label in ((9, 8, 14, "image/tiff", "scan.tiff", "Scan"), (10, 9, 15, "image/svg+xml", "icon.svg", "Icon")):
                payload = f'{{"_rails":{{"data":"gid://campfire/ActiveStorage::Blob/{blob_id}?expires_in","pur":"attachable"}}}}'.encode()
                encoded = base64.urlsafe_b64encode(payload).decode()
                signed = f"{encoded}--{hmac.new(signing_key, encoded.encode(), hashlib.sha1).hexdigest()}"
                body = f'<div>{label} <action-text-attachment sgid="{signed}" content-type="{content_type}" filename="{filename}"></action-text-attachment></div>'
                fixture.execute("INSERT INTO action_text_rich_texts(id,record_type,record_id,name,body,created_at,updated_at) VALUES(?,'Message',?,'body',?,?,?)", (rich_id, message_id, body, "2026-01-01 00:00:00", "2026-01-01 00:00:00"))
                fixture.execute("INSERT INTO message_search_index(rowid,body) VALUES(?,?)", (message_id, f"{label} [{filename}]"))
            filtered_body = "<div>  Before<section><p>Nested <em>text</em></p></section>After  </div>"
            fixture.execute("INSERT INTO action_text_rich_texts(id,record_type,record_id,name,body,created_at,updated_at) VALUES(10,'Message',11,'body',?,?,?)", (filtered_body, "2026-01-01 00:00:00", "2026-01-01 00:00:00"))
            fixture.execute("INSERT INTO boosts(id,message_id,booster_id,content,created_at,updated_at) VALUES(1,1,2,'Great','2026-01-01 00:00:00','2026-01-01 00:00:00')")
            fixture.execute("UPDATE sqlite_sequence SET seq=20 WHERE name='boosts'")
            fixture.execute("""INSERT INTO searches(id,user_id,query,created_at,updated_at)
                VALUES(1,1,'One','2025-01-01 00:00:00','2026-01-02 00:00:00')""")
            file_bytes = b"x" * 1234
            key = "abcdef1234567890"
            path = source_files / key[:2] / key[2:4] / key
            path.parent.mkdir(parents=True)
            path.write_bytes(file_bytes)
            fixture.execute("""INSERT INTO active_storage_blobs(id,key,filename,content_type,metadata,service_name,byte_size,created_at)
                VALUES(7,?,'imported.txt','text/plain','{}','local',?,'2026-01-01 00:00:00')""", (key, len(file_bytes)))
            fixture.execute("""INSERT INTO active_storage_attachments(id,name,record_type,record_id,blob_id,created_at)
                VALUES(7,'attachment','Message',2,7,'2026-01-01 00:00:00')""")
            inline_key = "ab10cdef1234567890"
            inline_file = source_files / inline_key[:2] / inline_key[2:4] / inline_key
            inline_file.parent.mkdir(parents=True, exist_ok=True)
            inline_file.write_bytes(file_bytes)
            fixture.execute("""INSERT INTO active_storage_blobs(id,key,filename,content_type,metadata,service_name,byte_size,created_at)
                VALUES(10,?,'inline.txt','text/plain','{}','local',?,'2026-01-01 00:00:00')""", (inline_key, len(file_bytes)))
            fixture.execute("""INSERT INTO active_storage_attachments(id,name,record_type,record_id,blob_id,created_at)
                VALUES(10,'embeds','ActionText::RichText',3,10,'2026-01-01 00:00:00')""")
            image_bytes = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/lXcAAAAASUVORK5CYII=")
            image_key = "ab11cdef1234567890"
            image_file = source_files / image_key[:2] / image_key[2:4] / image_key
            image_file.parent.mkdir(parents=True, exist_ok=True)
            image_file.write_bytes(image_bytes)
            fixture.execute("""INSERT INTO active_storage_blobs(id,key,filename,content_type,metadata,service_name,byte_size,created_at)
                VALUES(11,?,'pixel.png','image/png',?,'local',?,'2026-01-01 00:00:00')""", (image_key, json.dumps({"width": 1, "height": 1, "identified": True}), len(image_bytes)))
            fixture.execute("""INSERT INTO active_storage_attachments(id,name,record_type,record_id,blob_id,created_at)
                VALUES(11,'embeds','ActionText::RichText',4,11,'2026-01-01 00:00:00')""")
            pdf_bytes = minimal_pdf()
            pdf_key = "ab12cdef1234567890"
            pdf_file = source_files / pdf_key[:2] / pdf_key[2:4] / pdf_key
            pdf_file.parent.mkdir(parents=True, exist_ok=True)
            pdf_file.write_bytes(pdf_bytes)
            fixture.execute("""INSERT INTO active_storage_blobs(id,key,filename,content_type,metadata,service_name,byte_size,created_at)
                VALUES(12,?,'page.pdf','application/pdf','{}','local',?,'2026-01-01 00:00:00')""", (pdf_key, len(pdf_bytes)))
            fixture.execute("""INSERT INTO active_storage_attachments(id,name,record_type,record_id,blob_id,created_at)
                VALUES(12,'embeds','ActionText::RichText',5,12,'2026-01-01 00:00:00')""")
            video_key = "ab13cdef1234567890"
            video_file = source_files / video_key[:2] / video_key[2:4] / video_key
            video_file.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory() as video_temp:
                generated = Path(video_temp) / "clip.mp4"
                subprocess.run(("ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=c=black:s=16x16:r=1", "-t", "1", "-c:v", "mpeg4", "-pix_fmt", "yuv420p", "-y", str(generated)), capture_output=True, check=True)
                video_bytes = generated.read_bytes()
            video_file.write_bytes(video_bytes)
            fixture.execute("""INSERT INTO active_storage_blobs(id,key,filename,content_type,metadata,service_name,byte_size,created_at)
                VALUES(13,?,'clip.mp4','video/mp4',?,'local',?,'2026-01-01 00:00:00')""", (video_key, json.dumps({"width": 16, "height": 16, "identified": True}), len(video_bytes)))
            fixture.execute("""INSERT INTO active_storage_attachments(id,name,record_type,record_id,blob_id,created_at)
                VALUES(13,'embeds','ActionText::RichText',6,13,'2026-01-01 00:00:00')""")
            fixture.execute("""INSERT INTO active_storage_attachments(id,name,record_type,record_id,blob_id,created_at)
                VALUES(14,'embeds','ActionText::RichText',7,11,'2026-01-01 00:00:00')""")
            fixture.execute("""INSERT INTO active_storage_attachments(id,name,record_type,record_id,blob_id,created_at)
                VALUES(15,'embeds','ActionText::RichText',7,12,'2026-01-01 00:00:00')""")
            fixture.execute("""INSERT INTO active_storage_attachments(id,name,record_type,record_id,blob_id,created_at)
                VALUES(16,'embeds','ActionText::RichText',7,13,'2026-01-01 00:00:00')""")
            tiff_key = "ab14cdef1234567890"
            tiff_file = source_files / tiff_key[:2] / tiff_key[2:4] / tiff_key
            tiff_file.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory() as tiff_temp:
                generated = Path(tiff_temp) / "scan.tiff"
                subprocess.run(("vips", "copy", str(image_file), str(generated)), capture_output=True, check=True)
                tiff_bytes = generated.read_bytes()
            tiff_file.write_bytes(tiff_bytes)
            fixture.execute("""INSERT INTO active_storage_blobs(id,key,filename,content_type,metadata,service_name,byte_size,created_at)
                VALUES(14,?,'scan.tiff','image/tiff',?,'local',?,'2026-01-01 00:00:00')""", (tiff_key, json.dumps({"width": 1, "height": 1, "identified": True}), len(tiff_bytes)))
            fixture.execute("""INSERT INTO active_storage_attachments(id,name,record_type,record_id,blob_id,created_at)
                VALUES(17,'embeds','ActionText::RichText',8,14,'2026-01-01 00:00:00')""")
            svg_key = "ab15cdef1234567890"
            svg_file = source_files / svg_key[:2] / svg_key[2:4] / svg_key
            svg_file.parent.mkdir(parents=True, exist_ok=True)
            svg_bytes = b'<svg xmlns="http://www.w3.org/2000/svg" width="1" height="1"></svg>'
            svg_file.write_bytes(svg_bytes)
            fixture.execute("""INSERT INTO active_storage_blobs(id,key,filename,content_type,metadata,service_name,byte_size,created_at)
                VALUES(15,?,'icon.svg','image/svg+xml','{}','local',?,'2026-01-01 00:00:00')""", (svg_key, len(svg_bytes)))
            fixture.execute("""INSERT INTO active_storage_attachments(id,name,record_type,record_id,blob_id,created_at)
                VALUES(18,'embeds','ActionText::RichText',9,15,'2026-01-01 00:00:00')""")
            for blob_id, record_type, record_id, name in ((8, "User", 1, "avatar"), (9, "Account", 1, "logo")):
                media_key = f"ab{blob_id}cdef1234567890"
                media_file = source_files / media_key[:2] / media_key[2:4] / media_key
                media_file.parent.mkdir(parents=True, exist_ok=True)
                media_file.write_bytes(file_bytes)
                fixture.execute("""INSERT INTO active_storage_blobs(id,key,filename,content_type,metadata,service_name,byte_size,created_at)
                    VALUES(?,?,'media.txt','text/plain','{}','local',?,'2026-01-01 00:00:00')""", (blob_id, media_key, len(file_bytes)))
                fixture.execute("""INSERT INTO active_storage_attachments(id,name,record_type,record_id,blob_id,created_at)
                    VALUES(?,?,?,?,?,'2026-01-01 00:00:00')""", (blob_id, name, record_type, record_id, blob_id))
            fixture.execute("DELETE FROM message_search_index WHERE rowid IN (1,2)")
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
        assert result["messages"] == 11 and result["attachments"] == 1 and result["inline_embeds"] == 9, result
        assert result["reindexed_messages"] == 3, result
        assert result["push_subscriptions"] == 2
        assert target_db.with_suffix(".vapid.der").is_file()
        with sqlite3.connect(target_db) as imported:
            assert imported.execute("PRAGMA foreign_key_check").fetchall() == []
            assert imported.execute("SELECT endpoint,p256dh_key,auth_key FROM push_subscriptions ORDER BY id").fetchall() == [
                ("https://push.example.test/1", "test-p256dh", "test-auth"),
                ("https://push.example.test/1", "rotated-p256dh", "rotated-auth"),
            ]
            assert imported.execute("SELECT last_id FROM id_sequences WHERE name='messages'").fetchone() == (20,)
            assert imported.execute("SELECT last_id FROM id_sequences WHERE name='boosts'").fetchone() == (20,)
            assert imported.execute("SELECT count(*) FROM sqlite_master WHERE name='import_missing_search'").fetchone() == (0,)
            assert imported.execute("SELECT body,body_source,body_html FROM messages WHERE id=1").fetchone() == (
                "Hello\n• One\n• Two", source_body, source_body,
            )
            assert imported.execute("SELECT body FROM message_search_index WHERE rowid=1").fetchone() == ("Hello\n• One\n• Two",)
            assert imported.execute("SELECT body FROM messages WHERE id=2").fetchone() == ("",)
            assert imported.execute("SELECT body FROM message_search_index WHERE rowid=2").fetchone() == ("imported.txt",)
            filtered_plain, filtered_source, filtered_html = imported.execute("SELECT body,body_source,body_html FROM messages WHERE id=11").fetchone()
            assert filtered_plain == "  BeforeNested text\n\nAfter  " and filtered_source == filtered_body, (filtered_plain, filtered_source)
            assert "Nested" not in filtered_html and "BeforeAfter" in filtered_html.replace("  ", ""), filtered_html
            assert imported.execute("SELECT body FROM message_search_index WHERE rowid=11").fetchone() == (filtered_plain,)
            assert imported.execute("SELECT message_id,user_id FROM message_mentions").fetchall() == [(3, 2)]
            mention_plain, mention_html = imported.execute("SELECT body,body_html FROM messages WHERE id=3").fetchone()
            assert mention_plain == "@Rustfire Compare @Outside hello"
            assert 'class="mention"' in mention_html and "Rustfire Compare</div>" in mention_html
            inline_html = imported.execute("SELECT body_html FROM messages WHERE id=4").fetchone()[0]
            assert "<action-text-attachment" in inline_html and "attachment--file attachment--txt" in inline_html and "inline.txt" in inline_html and "1.21 KB" in inline_html, inline_html
            inline_stored = imported.execute("SELECT stored_name FROM inline_blobs WHERE id=10").fetchone()[0]
            assert (target_uploads / inline_stored).read_bytes() == file_bytes
            image_html = imported.execute("SELECT body_html FROM messages WHERE id=5").fetchone()[0]
            assert "attachment--preview attachment--png" in image_html and 'width="1"' in image_html and 'height="1"' in image_html, image_html
            image_url = re.search(r'<img src="([^"]+)"', image_html)
            assert image_url and "/rails/active_storage/representations/redirect/" in image_url.group(1), image_html
            assert variation_data(image_url.group(1)) == {"format": "png", "resize_to_limit": [1024, 768]}
            image_stored = imported.execute("SELECT stored_name,width,height FROM inline_blobs WHERE id=11").fetchone()
            assert image_stored[1:] == (1, 1) and (target_uploads / image_stored[0]).read_bytes() == image_bytes
            image_variant = target_uploads / "variants" / f"{image_stored[0]}-inline.png"
            assert image_variant.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
            image_variant.unlink()
            pdf_html = imported.execute("SELECT body_html FROM messages WHERE id=6").fetchone()[0]
            assert "attachment--preview attachment--pdf" in pdf_html, pdf_html
            pdf_url = re.search(r'<img src="([^"]+)"', pdf_html)
            assert pdf_url and "/rails/active_storage/representations/redirect/" in pdf_url.group(1), pdf_html
            assert variation_data(pdf_url.group(1)) == {"resize_to_limit": [1024, 768]}
            pdf_stored = imported.execute("SELECT stored_name FROM inline_blobs WHERE id=12").fetchone()[0]
            assert (target_uploads / pdf_stored).read_bytes() == pdf_bytes
            pdf_variant = target_uploads / "variants" / f"{pdf_stored}-inline-pdf.png"
            assert pdf_variant.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
            pdf_variant.unlink()
            video_html = imported.execute("SELECT body_html FROM messages WHERE id=7").fetchone()[0]
            assert "attachment--preview attachment--mp4" in video_html and 'width="16"' in video_html, video_html
            video_url = re.search(r'<img src="([^"]+)"', video_html)
            assert video_url and "/rails/active_storage/representations/redirect/" in video_url.group(1), video_html
            assert variation_data(video_url.group(1)) == {"resize_to_limit": [1024, 768]}
            video_stored = imported.execute("SELECT stored_name FROM inline_blobs WHERE id=13").fetchone()[0]
            assert (target_uploads / video_stored).read_bytes() == video_bytes
            video_variant = target_uploads / "variants" / f"{video_stored}-inline-video.jpeg"
            assert video_variant.read_bytes().startswith(b"\xff\xd8")
            video_variant.unlink()
            gallery_html = imported.execute("SELECT body_html FROM messages WHERE id=8").fetchone()[0]
            assert 'class="attachment-gallery attachment-gallery--3"' in gallery_html, gallery_html
            gallery_urls = re.findall(r'<img src="([^"]+)"', gallery_html)
            assert len(gallery_urls) == 3, gallery_html
            assert variation_data(gallery_urls[0]) == {"format": "png", "resize_to_limit": [800, 600]}
            assert variation_data(gallery_urls[1]) == {"resize_to_limit": [800, 600]}
            assert variation_data(gallery_urls[2]) == {"resize_to_limit": [800, 600]}
            tiff_html = imported.execute("SELECT body_html FROM messages WHERE id=9").fetchone()[0]
            assert "attachment--preview attachment--tiff" in tiff_html, tiff_html
            tiff_url = re.search(r'<img src="([^"]+)"', tiff_html)
            assert tiff_url and variation_data(tiff_url.group(1)) == {"format": "png", "resize_to_limit": [1024, 768]}
            tiff_stored = imported.execute("SELECT stored_name FROM inline_blobs WHERE id=14").fetchone()[0]
            assert (target_uploads / tiff_stored).read_bytes() == tiff_bytes
            svg_html = imported.execute("SELECT body_html FROM messages WHERE id=10").fetchone()[0]
            assert "attachment--file attachment--svg" in svg_html and "<img" not in svg_html, svg_html
            svg_stored = imported.execute("SELECT stored_name FROM inline_blobs WHERE id=15").fetchone()[0]
            assert (target_uploads / svg_stored).read_bytes() == svg_bytes
            assert imported.execute("SELECT id FROM attachments WHERE message_id=2").fetchone() == (7,)
            stored = imported.execute("SELECT stored_name FROM attachments WHERE id=7").fetchone()[0]
            assert (target_uploads / stored).read_bytes() == file_bytes
            assert imported.execute("SELECT count(*) FROM boosts").fetchone() == (1,)
            assert imported.execute("SELECT query,created_at,updated_at FROM searches WHERE id=1").fetchone() == ("One", "2025-01-01 00:00:00", "2026-01-02 00:00:00")
            assert imported.execute("SELECT restrict_room_creation FROM account_settings").fetchone() == (1,)
            assert imported.execute("SELECT css FROM account_custom_styles").fetchone() == ("body { color: navy; }",)
            assert imported.execute("SELECT custom_styles FROM accounts").fetchone() == ("body { color: navy; }",)
            assert imported.execute("SELECT token FROM sessions WHERE id=1").fetchone() == ("imported-session",)
            csrf_token = imported.execute("SELECT csrf_token FROM sessions WHERE id=1").fetchone()[0]
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
            assert '<style data-turbo-track="reload">body { color: navy; }</style>' in page
            assert public_key in page, page[:800]
            cookie_key = hashlib.pbkdf2_hmac("sha256", secret.encode(), b"signed cookie", 1000, 64)
            message = base64.b64encode(json.dumps("imported-session").encode()).decode()
            envelope = {"_rails": {"message": message, "exp": "2099-01-01T00:00:00.000Z", "pur": "cookie.session_token"}}
            encoded_cookie = base64.b64encode(json.dumps(envelope, separators=(",", ":")).encode()).decode()
            signed_cookie = f"{encoded_cookie}--{hmac.new(cookie_key, encoded_cookie.encode(), hashlib.sha1).hexdigest()}"
            signed_request = urllib.request.Request(f"http://127.0.0.1:{port}/rooms/1", headers={"Cookie": f"session_token={signed_cookie}"})
            with urllib.request.urlopen(signed_request, timeout=2) as response:
                assert "Imported room" in response.read().decode()
            form = urllib.parse.urlencode({"message[body]": "posted with imported cookie"}).encode()
            post = urllib.request.Request(f"http://127.0.0.1:{port}/rooms/1/messages", data=form,
                headers={"Cookie": f"session_token={signed_cookie}", "X-CSRF-Token": csrf_token, "Accept": "application/json"})
            with urllib.request.urlopen(post, timeout=2) as response:
                assert response.status == 201
            blob_key = hashlib.pbkdf2_hmac("sha256", secret.encode(), b"ActiveStorage", 1000, 64)
            blob_payload = json.dumps({"_rails": {"data": 10, "pur": "blob_id"}}, separators=(",", ":")).encode()
            blob_encoded = base64.b64encode(blob_payload).decode()
            blob_token = f"{blob_encoded}--{hmac.new(blob_key, blob_encoded.encode(), hashlib.sha1).hexdigest()}"
            blob_request = urllib.request.Request(f"http://127.0.0.1:{port}/rails/active_storage/blobs/redirect/{blob_token}/inline.txt")
            with urllib.request.urlopen(blob_request, timeout=2) as response:
                assert response.read() == file_bytes
            with urllib.request.urlopen(f"http://127.0.0.1:{port}{image_url.group(1)}", timeout=10) as response:
                assert response.status == 200 and response.headers["Content-Type"] == "image/png"
                assert response.read().startswith(b"\x89PNG\r\n\x1a\n")
            assert image_variant.is_file()
            with urllib.request.urlopen(f"http://127.0.0.1:{port}{pdf_url.group(1)}", timeout=10) as response:
                assert response.status == 200 and response.headers["Content-Type"] == "image/png"
                assert response.read().startswith(b"\x89PNG\r\n\x1a\n")
            assert pdf_variant.is_file()
            with urllib.request.urlopen(f"http://127.0.0.1:{port}{video_url.group(1)}", timeout=10) as response:
                assert response.status == 200 and response.headers["Content-Type"] == "image/jpeg"
                assert response.read().startswith(b"\xff\xd8")
            assert video_variant.is_file()
            with urllib.request.urlopen(f"http://127.0.0.1:{port}{tiff_url.group(1)}", timeout=10) as response:
                assert response.status == 200 and response.headers["Content-Type"] == "image/png"
                assert response.read().startswith(b"\x89PNG\r\n\x1a\n")
            for gallery_url, media_type, signature in zip(gallery_urls, ("image/png", "image/png", "image/jpeg"), (b"\x89PNG\r\n\x1a\n", b"\x89PNG\r\n\x1a\n", b"\xff\xd8")):
                with urllib.request.urlopen(f"http://127.0.0.1:{port}{gallery_url}", timeout=10) as response:
                    assert response.status == 200 and response.headers["Content-Type"] == media_type
                    assert response.read().startswith(signature)
            image_gallery_variant = target_uploads / "variants" / f"{image_stored[0]}-inline-gallery.png"
            assert image_gallery_variant.is_file()
            trix_data = json.dumps({"sgid": image_sgid, "contentType": "image/png", "filename": "pixel.png"}, separators=(",", ":"))
            edited_body = f'<div>Edited picture <figure data-trix-attachment=\'{trix_data}\'><img src="ignored"></figure> end</div>'
            edit_image = urllib.request.Request(f"http://127.0.0.1:{port}/rooms/1/messages/5",
                data=urllib.parse.urlencode({"message[body]": edited_body}).encode(), method="PATCH",
                headers={"Cookie": f"session_token={signed_cookie}", "X-CSRF-Token": csrf_token, "Accept": "application/json"})
            with urllib.request.urlopen(edit_image, timeout=2) as response:
                assert response.status == 200
            with sqlite3.connect(target_db) as edited:
                plain, source, rendered = edited.execute("SELECT body,body_source,body_html FROM messages WHERE id=5").fetchone()
                assert plain == "Edited picture [pixel.png] end", (plain, rendered)
                assert "<action-text-attachment" in source and "data-trix-attachment" not in source, source
                assert "attachment--preview attachment--png" in rendered and "<img" in rendered, rendered
                assert edited.execute("SELECT count(*) FROM inline_embeds WHERE message_id=5").fetchone() == (1,)
            remove_image = urllib.request.Request(f"http://127.0.0.1:{port}/rooms/1/messages/5",
                data=urllib.parse.urlencode({"message[body]": "<div>No picture</div>"}).encode(), method="PATCH",
                headers={"Cookie": f"session_token={signed_cookie}", "X-CSRF-Token": csrf_token, "Accept": "application/json"})
            with urllib.request.urlopen(remove_image, timeout=2) as response:
                assert response.status == 200
            with sqlite3.connect(target_db) as edited:
                assert edited.execute("SELECT body FROM messages WHERE id=5").fetchone() == ("No picture",)
                assert edited.execute("SELECT count(*) FROM inline_embeds WHERE message_id=5").fetchone() == (0,)
                assert edited.execute("SELECT count(*) FROM inline_blobs WHERE id=11").fetchone() == (1,)
            delete_image = urllib.request.Request(f"http://127.0.0.1:{port}/rooms/1/messages/5/delete", data=b"",
                headers={"Cookie": f"session_token={signed_cookie}", "X-CSRF-Token": csrf_token, "Accept": "text/vnd.turbo-stream.html"})
            with urllib.request.urlopen(delete_image, timeout=2) as response:
                assert response.status == 200
            with sqlite3.connect(target_db) as cleaned:
                assert cleaned.execute("SELECT count(*) FROM inline_blobs WHERE id=11").fetchone() == (1,)
            assert (target_uploads / image_stored[0]).is_file() and image_variant.is_file()
            delete_gallery = urllib.request.Request(f"http://127.0.0.1:{port}/rooms/1/messages/8/delete", data=b"",
                headers={"Cookie": f"session_token={signed_cookie}", "X-CSRF-Token": csrf_token, "Accept": "text/vnd.turbo-stream.html"})
            with urllib.request.urlopen(delete_gallery, timeout=2) as response:
                assert response.status == 200
            with sqlite3.connect(target_db) as cleaned:
                assert cleaned.execute("SELECT count(*) FROM inline_blobs WHERE id=11").fetchone() == (0,)
                assert cleaned.execute("SELECT count(*) FROM inline_blobs WHERE id IN (10,12,13)").fetchone() == (3,)
            assert not (target_uploads / image_stored[0]).exists() and not image_variant.exists() and not image_gallery_variant.exists()
        finally:
            server.terminate()
            server.wait(timeout=5)
        with sqlite3.connect(source_db) as fixture:
            fixture.execute("UPDATE active_storage_blobs SET content_type='video/x-unsupported' WHERE id=10")
        bad_target = root / "must-not-exist.sqlite3"
        bad_uploads = root / "must-not-exist-uploads"
        bad_command = command.copy()
        bad_command[bad_command.index("--target-db") + 1] = str(bad_target)
        bad_command[bad_command.index("--target-uploads") + 1] = str(bad_uploads)
        failure = subprocess.run(bad_command, env=environment, capture_output=True, text=True)
        assert failure.returncode != 0 and not bad_target.exists() and not bad_uploads.exists()
        with sqlite3.connect(source_db) as fixture:
            fixture.execute("UPDATE active_storage_blobs SET content_type='text/plain' WHERE id=10")
            fixture.execute("UPDATE active_storage_blobs SET metadata=? WHERE id=11", (json.dumps({"width": 2, "height": 1, "identified": True}),))
        invalid_image = subprocess.run(bad_command, env=environment, capture_output=True, text=True)
        assert invalid_image.returncode != 0 and not bad_target.exists() and not bad_uploads.exists()
        with sqlite3.connect(source_db) as fixture:
            fixture.execute("UPDATE active_storage_blobs SET metadata='{}' WHERE id=11")
        derived_image = subprocess.run(bad_command, env=environment, capture_output=True, text=True)
        assert derived_image.returncode == 0, derived_image.stderr
        with sqlite3.connect(bad_target) as imported:
            assert imported.execute("SELECT width,height FROM inline_blobs WHERE id=11").fetchone() == (1, 1)
    print("PASS Campfire account, users, room, rich messages and inline files/images/PDFs/videos, member-only mention recipients, preview URLs, saved and rebuilt search text, boost, session, push key, avatar, and logo import; missing image dimensions are derived and inconsistent metadata fails safely")


if __name__ == "__main__":
    main()
