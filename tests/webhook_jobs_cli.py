"""Exercise failed-webhook CLI migration and atomic requeue on an older database."""

import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile


ROOT = Path(__file__).resolve().parents[1]
BINARY = ROOT / "target/release/rustfire"


def run(database, *args, check=True):
    return subprocess.run([str(BINARY), "webhook-jobs", *args],
                          env=dict(os.environ, RUSTFIRE_DB=str(database)),
                          capture_output=True, text=True, check=check)


def main():
    with tempfile.TemporaryDirectory(prefix="rustfire-webhook-jobs-") as scratch:
        database = Path(scratch) / "old.sqlite3"
        with sqlite3.connect(database) as db:
            db.executescript("""
                CREATE TABLE webhook_jobs(id INTEGER PRIMARY KEY,bot_id INTEGER NOT NULL,message_id INTEGER NOT NULL,created_at TEXT NOT NULL,claimed_at INTEGER);
                CREATE TABLE failed_webhook_jobs(job_id INTEGER PRIMARY KEY,bot_id INTEGER NOT NULL,message_id INTEGER NOT NULL,error TEXT NOT NULL,created_at TEXT NOT NULL,failed_at TEXT NOT NULL);
                INSERT INTO failed_webhook_jobs VALUES(7,52,3,'connection refused','2026-01-01','2026-01-02');
                INSERT INTO failed_webhook_jobs VALUES(8,53,4,'connection refused','2026-01-01','2026-01-02');
            """)
        listed = json.loads(run(database, "list").stdout)
        assert [item["job_id"] for item in listed] == [7, 8] and all(item["retried_at"] is None for item in listed)
        assert run(database, "retry", "7").stdout.strip() == "Requeued failed webhook job 7"
        assert run(database, "retry-all").stdout.strip() == "Requeued 2 failed webhook jobs"
        missing = run(database, "retry", "999", check=False)
        assert missing.returncode != 0
        with sqlite3.connect(database) as db:
            columns = [row[1] for row in db.execute("PRAGMA table_info(failed_webhook_jobs)")]
            queued = db.execute("SELECT bot_id,message_id FROM webhook_jobs ORDER BY id").fetchall()
            failures = db.execute("SELECT job_id,retried_at FROM failed_webhook_jobs ORDER BY job_id").fetchall()
        assert "retried_at" in columns
        assert queued == [(52, 3), (52, 3), (53, 4)], queued
        assert len(failures) == 2 and all(retried_at for _, retried_at in failures), failures
        print("PASS failed webhook CLI migrates old schema and retains/requeues failures atomically")


if __name__ == "__main__":
    main()
