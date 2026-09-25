"""Seed a disposable Campfire or Rustfire SQLite database with matching messages.

Create the first account in each app before running this script. Stop the server
while seeding. The database must have no messages unless --replace is provided.
"""
import argparse
from datetime import datetime, timezone
import pathlib
import sqlite3


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("app", choices=("campfire", "rustfire"))
    parser.add_argument("database", type=pathlib.Path)
    parser.add_argument("--count", type=int, default=10_000)
    parser.add_argument("--replace", action="store_true", help="delete existing messages in this disposable benchmark database")
    args = parser.parse_args()
    if args.count < 40 or not args.database.is_file():
        parser.error("--count must be at least 40 and the database file must exist")

    db = sqlite3.connect(args.database, timeout=30)
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=NORMAL")
    if db.execute("SELECT COUNT(*) FROM users WHERE id=1 AND status=0").fetchone()[0] != 1:
        parser.error("user 1 must be an active benchmark administrator")
    if db.execute("SELECT COUNT(*) FROM rooms WHERE id=1").fetchone()[0] != 1:
        parser.error("room 1 must exist")
    existing = db.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    if existing and not args.replace:
        parser.error(f"database already has {existing} messages; use a fresh database or --replace")

    timestamp = "2026-09-24 20:05:00.000000"
    timestamp_ns = int(datetime.strptime(timestamp, "%Y-%m-%d %H:%M:%S.%f").replace(tzinfo=timezone.utc).timestamp()) * 1_000_000_000
    with db:
        if args.replace:
            if args.app == "campfire":
                db.execute("DELETE FROM action_text_rich_texts WHERE record_type='Message'")
            db.execute("DELETE FROM messages")
        if args.app == "campfire":
            db.executemany(
                "INSERT INTO messages(id,client_message_id,created_at,creator_id,room_id,updated_at) VALUES(?,?,?,?,?,?)",
                ((i, f"fixture-{i}", timestamp, 1, 1, timestamp) for i in range(1, args.count + 1)),
            )
            db.executemany(
                "INSERT INTO action_text_rich_texts(name,body,record_type,record_id,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                (("body", f"<div>benchmark message {i}</div>", "Message", i, timestamp, timestamp)
                 for i in range(1, args.count + 1)),
            )
        else:
            db.executemany(
                "INSERT INTO messages(id,room_id,creator_id,body,client_message_id,created_at,created_at_ns,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                ((i, 1, 1, f"benchmark message {i}", f"fixture-{i}", timestamp, timestamp_ns, timestamp)
                 for i in range(1, args.count + 1)),
            )
    print(f"Seeded {db.execute('SELECT COUNT(*) FROM messages').fetchone()[0]} {args.app} messages in room 1")


if __name__ == "__main__":
    main()
