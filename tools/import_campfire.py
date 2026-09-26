#!/usr/bin/env python3
"""Import a stopped ONCE Campfire SQLite installation into a new Rustfire database."""

import argparse
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
import uuid


def rows(db, query, parameters=()):
    return db.execute(query, parameters)


def copy_table(source, target, table, columns, select=None):
    names = ",".join(columns)
    placeholders = ",".join("?" for _ in columns)
    count = 0
    for row in rows(source, select or f"SELECT {names} FROM {table}"):
        target.execute(f"INSERT INTO {table}({names}) VALUES({placeholders})", tuple(row))
        count += 1
    return count


def blob_file(source_files, key):
    if not key or not key.isalnum() or len(key) < 4:
        raise ValueError(f"invalid Active Storage key: {key!r}")
    path = source_files / key[:2] / key[2:4] / key
    if not path.is_file():
        raise FileNotFoundError(f"missing Active Storage file: {path}")
    return path


def store_blob(source_files, uploads, key, byte_size):
    source = blob_file(source_files, key)
    if source.stat().st_size != byte_size:
        raise ValueError(f"Active Storage byte size mismatch: {source}")
    stored = str(uuid.uuid4())
    shutil.copyfile(source, uploads / stored)
    return stored


def prepare_inline_image(uploads, stored, content_type, width, height):
    formats = {"image/png": "png", "image/jpeg": "jpeg", "image/gif": "gif", "image/webp": "webp", "image/avif": "avif", "image/tiff": "png"}
    variant_dir = uploads / "variants"
    variant_dir.mkdir(exist_ok=True)
    output = variant_dir / f"{stored}-inline.{formats[content_type]}"
    temporary = variant_dir / f"inline-{uuid.uuid4()}.{formats[content_type]}"
    try:
        dimensions = []
        for field in ("width", "height"):
            result = subprocess.run(("vipsheader", "-f", field, str(uploads / stored)), capture_output=True, text=True, check=True)
            dimensions.append(int(result.stdout.strip()))
        if (width is not None and width != dimensions[0]) or (height is not None and height != dimensions[1]):
            raise ValueError(f"inline image {stored} dimensions do not match Active Storage metadata")
        subprocess.run(("vips", "thumbnail", str(uploads / stored), str(temporary), "1024", "--height", "768", "--size", "down"), capture_output=True, check=True)
        os.replace(temporary, output)
        return dimensions
    except (OSError, subprocess.CalledProcessError, ValueError) as error:
        raise ValueError(f"cannot render inline image {stored} ({content_type})") from error
    finally:
        temporary.unlink(missing_ok=True)


def prepare_inline_pdf(uploads, stored):
    variant_dir = uploads / "variants"
    variant_dir.mkdir(exist_ok=True)
    prefix = variant_dir / f"pdf-{uuid.uuid4()}"
    frame = prefix.with_suffix(".png")
    output = variant_dir / f"{stored}-inline-pdf.png"
    temporary = variant_dir / f"pdf-{uuid.uuid4()}.png"
    try:
        subprocess.run(("pdftoppm", "-f", "1", "-singlefile", "-cropbox", "-r", "72", "-png", str(uploads / stored), str(prefix)), capture_output=True, check=True)
        subprocess.run(("vips", "thumbnail", str(frame), str(temporary), "1024", "--height", "768", "--size", "down"), capture_output=True, check=True)
        os.replace(temporary, output)
    except (OSError, subprocess.CalledProcessError) as error:
        raise ValueError(f"cannot render inline PDF {stored}") from error
    finally:
        frame.unlink(missing_ok=True)
        temporary.unlink(missing_ok=True)


def prepare_inline_video(uploads, stored):
    variant_dir = uploads / "variants"
    variant_dir.mkdir(exist_ok=True)
    frame = variant_dir / f"video-{uuid.uuid4()}.jpg"
    temporary = variant_dir / f"video-{uuid.uuid4()}.jpeg"
    output = variant_dir / f"{stored}-inline-video.jpeg"
    try:
        subprocess.run(("ffmpeg", "-v", "error", "-i", str(uploads / stored), "-y", "-vframes", "1", "-f", "image2", str(frame)), capture_output=True, check=True)
        subprocess.run(("vips", "thumbnail", str(frame), str(temporary), "1024", "--height", "768", "--size", "down"), capture_output=True, check=True)
        os.replace(temporary, output)
    except (OSError, subprocess.CalledProcessError) as error:
        raise ValueError(f"cannot render inline video {stored}") from error
    finally:
        frame.unlink(missing_ok=True)
        temporary.unlink(missing_ok=True)


def import_data(source, target, source_files, uploads):
    if target.execute("SELECT EXISTS(SELECT 1 FROM users)").fetchone()[0]:
        raise ValueError("the Rustfire database already contains users")
    accounts = list(rows(source, "SELECT id,name,join_code,created_at,updated_at,settings,custom_styles FROM accounts"))
    if len(accounts) != 1:
        raise ValueError("expected one Campfire account")
    unknown_media = list(source.execute("""SELECT DISTINCT record_type,name FROM active_storage_attachments
        WHERE NOT ((record_type='Message' AND name='attachment') OR (record_type='User' AND name='avatar')
        OR (record_type='Account' AND name='logo') OR (record_type='ActionText::RichText' AND name='embeds'))"""))
    if unknown_media:
        raise ValueError(f"unknown Active Storage attachment types: {unknown_media}")

    account = accounts[0]
    settings = json.loads(account[5] or "{}")
    target.execute("INSERT INTO accounts(id,name,join_code,created_at,updated_at) VALUES(?,?,?,?,?)", account[:5])
    target.execute("UPDATE accounts SET custom_styles=? WHERE id=1", (account[6],))
    target.execute("UPDATE account_settings SET restrict_room_creation=? WHERE id=1", (int(bool(settings.get("restrict_room_creation_to_administrators", False))),))
    target.execute("UPDATE account_custom_styles SET css=? WHERE id=1", (account[6] or "",))

    counts = {"accounts": 1}
    tables = {
        "users": ("id", "name", "email_address", "password_digest", "role", "status", "bot_token", "bio", "created_at", "updated_at"),
        "bans": ("id", "user_id", "ip_address"),
        "rooms": ("id", "name", "type", "creator_id", "created_at", "updated_at"),
        "boosts": ("id", "message_id", "booster_id", "content", "created_at"),
    }
    counts["users"] = copy_table(source, target, "users", tables["users"])
    counts["bans"] = copy_table(source, target, "bans", tables["bans"])
    counts["rooms"] = copy_table(source, target, "rooms", tables["rooms"])
    counts["memberships"] = copy_table(
        source, target, "memberships",
        ("id", "room_id", "user_id", "involvement", "unread_at", "created_at"),
        "SELECT id,room_id,user_id,COALESCE(involvement,'mentions'),unread_at,created_at FROM memberships",
    )
    counts["webhooks"] = copy_table(
        source, target, "webhooks", ("user_id", "url"),
        "SELECT user_id,url FROM webhooks WHERE url IS NOT NULL AND url<>''",
    )
    counts["push_subscriptions"] = copy_table(
        source, target, "push_subscriptions",
        ("id", "user_id", "endpoint", "p256dh_key", "auth_key", "user_agent", "created_at", "updated_at"),
    )
    counts["sessions"] = copy_table(
        source, target, "sessions",
        ("id", "user_id", "token", "created_at", "last_active_at", "ip_address"),
    )
    target.execute("CREATE TABLE import_missing_search(message_id INTEGER PRIMARY KEY REFERENCES messages(id) ON DELETE CASCADE)")

    messages = rows(source, """SELECT m.id,m.room_id,m.creator_id,m.client_message_id,m.created_at,m.updated_at,
        rich.body,idx.body,attachment.id FROM messages m
        LEFT JOIN action_text_rich_texts rich ON rich.record_type='Message' AND rich.record_id=m.id AND rich.name='body'
        LEFT JOIN active_storage_attachments attachment ON attachment.record_type='Message' AND attachment.record_id=m.id AND attachment.name='attachment'
        LEFT JOIN message_search_index idx ON idx.rowid=m.id ORDER BY m.id""")
    counts["messages"] = 0
    for message in messages:
        mid, room_id, creator_id, client_id, created, updated, source_body, plain, attachment_id = message
        missing_search = plain is None
        if missing_search:
            plain = ""
        body = "" if attachment_id is not None and not source_body else plain
        target.execute("""INSERT INTO messages(id,room_id,creator_id,body,body_source,client_message_id,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?)""", (mid, room_id, creator_id, body, source_body, client_id, created, updated))
        if missing_search:
            target.execute("INSERT INTO import_missing_search(message_id) VALUES(?)", (mid,))
        counts["messages"] += 1
    source_sequence = source.execute("SELECT seq FROM sqlite_sequence WHERE name='messages'").fetchone()
    if source_sequence:
        target.execute("UPDATE id_sequences SET last_id=MAX(last_id,?) WHERE name='messages'", (source_sequence[0],))
    counts["reindexed_messages"] = target.execute("SELECT count(*) FROM import_missing_search").fetchone()[0]
    counts["boosts"] = copy_table(source, target, "boosts", tables["boosts"])
    source_boost_sequence = source.execute("SELECT seq FROM sqlite_sequence WHERE name='boosts'").fetchone()
    if source_boost_sequence:
        target.execute("UPDATE id_sequences SET last_id=MAX(last_id,?) WHERE name='boosts'", (source_boost_sequence[0],))
    counts["searches"] = 0
    for search in rows(source, "SELECT id,user_id,query,created_at,updated_at FROM searches ORDER BY updated_at,id"):
        target.execute("""INSERT INTO searches(id,user_id,query,created_at,updated_at) VALUES(?,?,?,?,?)
            ON CONFLICT(user_id,query) DO UPDATE SET updated_at=excluded.updated_at""", tuple(search))
        counts["searches"] += 1

    blobs = rows(source, """SELECT attachment.record_id,blob.id,blob.key,blob.filename,
        COALESCE(blob.content_type,'application/octet-stream'),blob.byte_size,blob.created_at,blob.metadata
        FROM active_storage_attachments attachment JOIN active_storage_blobs blob ON blob.id=attachment.blob_id
        WHERE attachment.record_type='Message' AND attachment.name='attachment' ORDER BY attachment.record_id,attachment.id""")
    counts["attachments"] = 0
    max_blob_id = source.execute("SELECT COALESCE(max(id),0) FROM active_storage_blobs").fetchone()[0]
    next_id = max_blob_id + 1
    used_blob_ids = set()
    for record_id, blob_id, key, filename, content_type, size, created, metadata in blobs:
        if target.execute("SELECT EXISTS(SELECT 1 FROM attachments WHERE message_id=?)", (record_id,)).fetchone()[0]:
            raise ValueError(f"message {record_id} has multiple file attachments")
        attachment_id = blob_id
        if blob_id in used_blob_ids:
            attachment_id = next_id
            next_id += 1
        used_blob_ids.add(blob_id)
        details = json.loads(metadata or "{}")
        stored = store_blob(source_files, uploads, key, size)
        target.execute("""INSERT INTO attachments(id,message_id,filename,content_type,stored_name,created_at,width,height)
            VALUES(?,?,?,?,?,?,?,?)""", (attachment_id, record_id, filename, content_type, stored, created, details.get("width"), details.get("height")))
        counts["attachments"] += 1

    inline = rows(source, """SELECT rich.record_id,blob.id,blob.key,blob.filename,
        COALESCE(blob.content_type,'application/octet-stream'),blob.byte_size,blob.created_at,blob.metadata
        FROM active_storage_attachments attachment JOIN active_storage_blobs blob ON blob.id=attachment.blob_id
        JOIN action_text_rich_texts rich ON rich.id=attachment.record_id AND rich.record_type='Message' AND rich.name='body'
        WHERE attachment.record_type='ActionText::RichText' AND attachment.name='embeds'""")
    preview_images = ("image/png", "image/jpeg", "image/gif", "image/webp", "image/avif", "image/tiff")
    file_images = ("image/svg+xml", "image/bmp")
    counts["inline_embeds"] = 0
    for message_id, blob_id, key, filename, content_type, size, created, metadata in inline:
        if (content_type.startswith("image/") and content_type not in preview_images + file_images) or (content_type.startswith("video/") and content_type not in ("video/mp4", "video/webm", "video/quicktime", "video/ogg")):
            raise ValueError(f"inline media preview for blob {blob_id} ({content_type}) needs migration support")
        if not target.execute("SELECT EXISTS(SELECT 1 FROM inline_blobs WHERE id=?)", (blob_id,)).fetchone()[0]:
            details = json.loads(metadata or "{}")
            width, height = details.get("width"), details.get("height")
            if content_type in preview_images and any(value is not None and (type(value) is not int or value <= 0) for value in (width, height)):
                raise ValueError(f"inline image blob {blob_id} has invalid dimensions")
            stored = store_blob(source_files, uploads, key, size)
            if content_type in preview_images:
                width, height = prepare_inline_image(uploads, stored, content_type, width, height)
            elif content_type == "application/pdf":
                prepare_inline_pdf(uploads, stored)
            elif content_type.startswith("video/"):
                prepare_inline_video(uploads, stored)
            target.execute("""INSERT INTO inline_blobs(id,filename,content_type,stored_name,byte_size,created_at,width,height)
                VALUES(?,?,?,?,?,?,?,?)""", (blob_id, filename, content_type, stored, size, created, width, height))
        target.execute("INSERT INTO inline_embeds(message_id,blob_id) VALUES(?,?)", (message_id, blob_id))
        counts["inline_embeds"] += 1
    unmatched = source.execute("""SELECT count(*) FROM active_storage_attachments attachment
        LEFT JOIN action_text_rich_texts rich ON rich.id=attachment.record_id AND rich.record_type='Message' AND rich.name='body'
        WHERE attachment.record_type='ActionText::RichText' AND attachment.name='embeds' AND rich.id IS NULL""").fetchone()[0]
    if unmatched:
        raise ValueError(f"{unmatched} inline file embeds do not belong to a message body")

    media = (("User", "avatar", "avatars", "user_id"), ("Account", "logo", "account_logos", "id"))
    for record_type, name, table, key_column in media:
        count = 0
        query = """SELECT attachment.record_id,blob.key,COALESCE(blob.content_type,'application/octet-stream'),blob.byte_size
            FROM active_storage_attachments attachment JOIN active_storage_blobs blob ON blob.id=attachment.blob_id
            WHERE attachment.record_type=? AND attachment.name=?"""
        for record_id, key, content_type, size in rows(source, query, (record_type, name)):
            stored = store_blob(source_files, uploads, key, size)
            target.execute(f"INSERT INTO {table}({key_column},stored_name,content_type) VALUES(?,?,?)", (record_id, stored, content_type))
            count += 1
        counts[table] = count

    foreign_keys = list(target.execute("PRAGMA foreign_key_check"))
    if foreign_keys:
        raise ValueError(f"imported records fail foreign-key checks: {foreign_keys[:5]}")
    return counts


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-db", type=Path, required=True)
    parser.add_argument("--source-files", type=Path, required=True, help="Campfire storage/files directory")
    parser.add_argument("--target-db", type=Path, required=True)
    parser.add_argument("--target-uploads", type=Path, required=True)
    parser.add_argument("--rustfire-bin", type=Path, default=Path("target/release/rustfire"))
    args = parser.parse_args()
    if not os.environ.get("RUSTFIRE_CAMPFIRE_SECRET_KEY_BASE"):
        parser.error("RUSTFIRE_CAMPFIRE_SECRET_KEY_BASE must contain the source secret key base")
    for source_path in (args.source_db, args.source_files, args.rustfire_bin):
        if not source_path.exists():
            parser.error(f"missing source or binary: {source_path}")
    if args.target_db.exists() or args.target_uploads.exists():
        parser.error("target database and upload directory must not exist")
    source_check = sqlite3.connect(f"{args.source_db.resolve().as_uri()}?mode=ro", uri=True)
    try:
        has_push = source_check.execute("SELECT EXISTS(SELECT 1 FROM push_subscriptions)").fetchone()[0]
    finally:
        source_check.close()
    if has_push and not (os.environ.get("RUSTFIRE_CAMPFIRE_VAPID_PRIVATE_KEY") and os.environ.get("RUSTFIRE_CAMPFIRE_VAPID_PUBLIC_KEY")):
        parser.error("existing push subscriptions require RUSTFIRE_CAMPFIRE_VAPID_PRIVATE_KEY and RUSTFIRE_CAMPFIRE_VAPID_PUBLIC_KEY")
    target_vapid = args.target_db.with_suffix(".vapid.der")
    if has_push and target_vapid.exists():
        parser.error(f"target VAPID key file already exists: {target_vapid}")
    args.target_db.parent.mkdir(parents=True, exist_ok=True)
    args.target_uploads.parent.mkdir(parents=True, exist_ok=True)
    binary = args.rustfire_bin.resolve()
    with tempfile.TemporaryDirectory(prefix=".rustfire-import-", dir=args.target_db.parent) as temporary:
        stage_db = Path(temporary) / "rustfire.db"
        stage_uploads = Path(temporary) / "uploads"
        stage_uploads.mkdir()
        stage_vapid = stage_db.with_suffix(".vapid.der")
        environment = dict(os.environ, RUSTFIRE_DB=str(stage_db), RUSTFIRE_UPLOAD_DIR=str(stage_uploads), RUSTFIRE_VAPID_KEY_FILE=str(stage_vapid))
        subprocess.run((binary, "--init-db"), env=environment, check=True)
        if has_push:
            subprocess.run((binary, "--import-campfire-vapid"), env=environment, check=True)
        source = sqlite3.connect(f"{args.source_db.resolve().as_uri()}?mode=ro", uri=True)
        target = sqlite3.connect(stage_db)
        try:
            target.execute("PRAGMA foreign_keys=ON")
            target.execute("BEGIN IMMEDIATE")
            counts = import_data(source, target, args.source_files, stage_uploads)
            target.commit()
        finally:
            target.close()
            source.close()
        subprocess.run((binary, "--render-imported-rich-text"), env=environment, check=True)
        checkpoint = sqlite3.connect(stage_db)
        checkpoint.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        checkpoint.execute("PRAGMA journal_mode=DELETE")
        checkpoint.close()
        published = []
        try:
            for path in (args.target_db, args.target_uploads, target_vapid if has_push else None):
                if path is not None and path.exists():
                    raise FileExistsError(path)
            os.replace(stage_uploads, args.target_uploads)
            published.append(args.target_uploads)
            os.replace(stage_db, args.target_db)
            published.append(args.target_db)
            if has_push:
                os.replace(stage_vapid, target_vapid)
                published.append(target_vapid)
        except Exception:
            for path in reversed(published):
                if path.is_dir():
                    shutil.rmtree(path)
                else:
                    path.unlink()
            raise
    print(json.dumps(counts, sort_keys=True))


if __name__ == "__main__":
    main()
