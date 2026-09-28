"""Compare concurrent attachment edits and background purge on paired accounts."""

import argparse
import concurrent.futures
import hashlib
import http.client
import json
import os
import pathlib
import signal
import sqlite3
import statistics
import subprocess
import tempfile
import threading
import time
import urllib.request

from direct_lookup import free_port, p95, start_server, stop_server
from paired_attachment_mime import presentation
from paired_banned_content import BUNDLE, REPOSITORY, REVISION, RUBY, isolated_campfire, start_redis, wait_for_worker
from paired_direct_lookup import login_campfire, seed_campfire, seed_rustfire, wait_for_server
from paired_message_parameter_edges import multipart_body


def old_rust_key(index):
    return f"11111111-1111-4111-8111-{index:012d}"


def old_camp_key(index):
    return f"oldattachmentfixturekey{index:06d}"


def seed_messages(rust_database, camp_database, rust_uploads, camp_files, clients):
    rust_time = "2026-01-01T00:00:00Z"
    camp_time = "2026-01-01 00:00:00.000000"
    rust_uploads.mkdir(parents=True)
    with sqlite3.connect(rust_database) as db:
        for index in range(1, clients + 1):
            db.execute(
                "INSERT INTO messages(id,room_id,creator_id,body,client_message_id,created_at,updated_at) VALUES(?,1,1,?,?,?,?)",
                (index, f"Original {index}", f"edit-{index}", rust_time, rust_time),
            )
            db.execute(
                "INSERT INTO attachments(id,message_id,filename,content_type,stored_name,created_at) VALUES(?,?,'old.txt','text/plain',?,?)",
                (index, index, old_rust_key(index), rust_time),
            )
            (rust_uploads / old_rust_key(index)).write_bytes(f"old {index}".encode())
    with sqlite3.connect(camp_database) as db:
        for index in range(1, clients + 1):
            db.execute(
                "INSERT INTO messages(id,room_id,creator_id,client_message_id,created_at,updated_at) VALUES(?,1,1,?,?,?)",
                (index, f"edit-{index}", camp_time, camp_time),
            )
            db.execute(
                "INSERT INTO action_text_rich_texts(name,body,record_type,record_id,created_at,updated_at) VALUES('body',?,'Message',?,?,?)",
                (f"Original {index}", index, camp_time, camp_time),
            )
            db.execute(
                "INSERT INTO active_storage_blobs(id,key,filename,content_type,metadata,service_name,byte_size,created_at) VALUES(?,?,'old.txt','text/plain','{}','local',?,?)",
                (index, old_camp_key(index), len(f"old {index}".encode()), camp_time),
            )
            db.execute(
                "INSERT INTO active_storage_attachments(name,record_type,record_id,blob_id,created_at) VALUES('attachment','Message',?,?,?)",
                (index, index, camp_time),
            )
            key = old_camp_key(index)
            original = camp_files / key[:2] / key[2:4] / key
            original.parent.mkdir(parents=True, exist_ok=True)
            original.write_bytes(f"old {index}".encode())


def edit_many(port, cookie, csrf, contents):
    ready = threading.Barrier(len(contents) + 1, timeout=60)
    start = threading.Event()

    def edit(index, data):
        body, content_type = multipart_body((("message[attachment]", data, f"edited-{index}.txt", "text/plain"),))
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=120)
        try:
            ready.wait()
            assert start.wait(30)
            begun = time.perf_counter()
            connection.request("PATCH", f"/rooms/1/messages/{index}", body, {
                "Cookie": cookie,
                "X-CSRF-Token": csrf,
                "Content-Type": content_type,
                "Accept": "text/html",
            })
            response = connection.getresponse()
            payload = response.read()
            elapsed_ms = (time.perf_counter() - begun) * 1000
            assert response.status == 302, (index, response.status, payload[:200])
            assert response.getheader("Location", "").endswith(f"/rooms/1/messages/{index}")
            return elapsed_ms
        finally:
            connection.close()

    with concurrent.futures.ThreadPoolExecutor(max_workers=len(contents)) as executor:
        futures = [executor.submit(edit, index, data) for index, data in contents.items()]
        ready.wait()
        begun = time.perf_counter()
        start.set()
        latencies = [future.result() for future in futures]
        elapsed = time.perf_counter() - begun
    return {
        "edits": len(contents), "elapsed_s": round(elapsed, 3),
        "edits_per_s": round(len(contents) / elapsed, 2),
        "median_ms": round(statistics.median(latencies), 2),
        "p95_ms": round(p95(latencies), 2),
    }


def final_state(database, uploads, campfire, contents):
    with sqlite3.connect(database) as db:
        if campfire:
            rows = db.execute("""SELECT m.id,m.client_message_id,b.id,b.filename,b.content_type,b.key
                FROM messages m JOIN active_storage_attachments a ON a.record_type='Message' AND a.record_id=m.id AND a.name='attachment'
                JOIN active_storage_blobs b ON b.id=a.blob_id ORDER BY m.id""").fetchall()
            blobs = db.execute("SELECT COUNT(*) FROM active_storage_blobs").fetchone()[0]
            assert blobs == len(contents), blobs
        else:
            rows = db.execute("""SELECT m.id,m.client_message_id,a.id,a.filename,a.content_type,a.stored_name
                FROM messages m JOIN attachments a ON a.message_id=m.id ORDER BY m.id""").fetchall()
            assert db.execute("SELECT COUNT(*) FROM attachment_jobs").fetchone()[0] == 0
            assert db.execute("SELECT COUNT(*) FROM replaced_attachments").fetchone()[0] == 0
    assert len(rows) == len(contents), rows
    blob_ids = set()
    for index, client_id, blob_id, filename, content_type, key in rows:
        assert (client_id, filename, content_type) == (f"edit-{index}", f"edited-{index}.txt", "text/plain")
        blob_ids.add(blob_id)
        original = uploads / key[:2] / key[2:4] / key if campfire else uploads / key
        with original.open("rb") as stream:
            assert hashlib.file_digest(stream, "sha256").digest() == hashlib.sha256(contents[index]).digest()
        old_key = old_camp_key(index) if campfire else old_rust_key(index)
        old_file = uploads / old_key[:2] / old_key[2:4] / old_key if campfire else uploads / old_key
        assert not old_file.exists(), old_file
    assert blob_ids == set(range(len(contents) + 1, 2 * len(contents) + 1)), blob_ids
    if not campfire:
        assert not list(uploads.glob("message-edit-upload-*"))


def collect_pages(port, cookie, clients):
    pages = []
    for index in range(1, clients + 1):
        request = urllib.request.Request(f"http://127.0.0.1:{port}/rooms/1/messages/{index}", headers={"Cookie": cookie})
        with urllib.request.urlopen(request, timeout=30) as response:
            assert response.status == 200
            pages.append(tuple(presentation(response.read(), f"edit-{index}")))
    return pages


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--clients", type=int, default=4)
    parser.add_argument("--file-mib", type=int, default=8)
    parser.add_argument("--campfire-workers", type=int, default=22)
    parser.add_argument("--rustfire-first", action="store_true")
    parser.add_argument("--report", type=pathlib.Path)
    args = parser.parse_args()
    if min(args.clients, args.file_mib, args.campfire_workers) < 1:
        parser.error("clients, file MiB, and Campfire workers must be positive")
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPOSITORY, text=True).strip() == REVISION
    contents = {index: bytes([65 + index % 26]) * (args.file_mib * 1048576) for index in range(1, args.clients + 1)}
    with tempfile.TemporaryDirectory(prefix="paired-concurrent-edits-") as scratch:
        temp = pathlib.Path(scratch)
        rust_db, camp_db = temp / "rust.sqlite3", temp / "camp.sqlite3"
        rust_port, camp_port, redis_port = free_port(), free_port(), free_port()
        seed_rustfire(rust_db, rust_port, [])
        camp_env = seed_campfire(REPOSITORY, RUBY, BUNDLE, REPOSITORY / "storage/db/production.sqlite3", camp_db, [], camp_port, temp)
        camp_env["WEB_CONCURRENCY"] = str(args.campfire_workers)
        camp_env["REDIS_URL"] = f"redis://127.0.0.1:{redis_port}"
        checkout = isolated_campfire(temp, redis_port)
        rust_uploads, camp_uploads = temp / "rust-uploads", checkout / "storage/files"
        seed_messages(rust_db, camp_db, rust_uploads, camp_uploads, args.clients)
        redis, redis_log = start_redis(temp, redis_port)
        try:
            def run_rustfire():
                process = start_server(rust_db, rust_port, {"RUSTFIRE_UPLOAD_DIR": str(rust_uploads)})
                try:
                    result = edit_many(rust_port, "session_token=benchmark-session", "benchmark-csrf", contents)
                    drain_started = time.perf_counter()
                    deadline = time.monotonic() + 30
                    while time.monotonic() < deadline:
                        with sqlite3.connect(rust_db) as db:
                            pending = db.execute("SELECT COUNT(*) FROM attachment_jobs").fetchone()[0]
                        if pending == 0:
                            break
                        time.sleep(.05)
                    else:
                        raise AssertionError(("Rustfire attachment jobs did not drain", pending))
                    result["purge_drain_s"] = round(time.perf_counter() - drain_started, 3)
                    result["edit_and_purge_s"] = round(result["elapsed_s"] + result["purge_drain_s"], 3)
                    final_state(rust_db, rust_uploads, False, contents)
                    return result, collect_pages(rust_port, "session_token=benchmark-session", args.clients)
                finally:
                    stop_server(process)

            def run_campfire():
                environment = dict(camp_env, DATABASE_URL=f"sqlite3:{camp_db}", PORT=str(camp_port), PIDFILE=str(temp / "puma.pid"))
                with open(temp / "puma.log", "w+") as log, open(temp / "worker.log", "w+") as worker_log:
                    process = subprocess.Popen([str(RUBY), str(RUBY.parent / "bundle"), "exec", "puma", "-C", "config/puma.rb"], cwd=checkout, env=environment, stdout=log, stderr=log)
                    worker = None
                    try:
                        wait_for_server(camp_port, process)
                        cookie, csrf = login_campfire(camp_port)
                        worker = subprocess.Popen([str(RUBY), str(RUBY.parent / "bundle"), "exec", "rake", "resque:work"], cwd=checkout, env=dict(environment, QUEUE="default", INTERVAL="0.1"), stdout=worker_log, stderr=worker_log, start_new_session=True)
                        wait_for_worker(redis_port, worker)
                        result = edit_many(camp_port, cookie, csrf, contents)
                        drain_started = time.perf_counter()
                        deadline = time.monotonic() + 30
                        while time.monotonic() < deadline:
                            with sqlite3.connect(camp_db) as db:
                                remaining = db.execute("SELECT COUNT(*) FROM active_storage_blobs").fetchone()[0]
                            if remaining == args.clients:
                                break
                            time.sleep(.05)
                        else:
                            raise AssertionError(("Campfire purge jobs did not drain", remaining))
                        result["purge_drain_s"] = round(time.perf_counter() - drain_started, 3)
                        result["edit_and_purge_s"] = round(result["elapsed_s"] + result["purge_drain_s"], 3)
                        final_state(camp_db, camp_uploads, True, contents)
                        return result, collect_pages(camp_port, cookie, args.clients)
                    finally:
                        if worker is not None:
                            if worker.poll() is None:
                                os.killpg(worker.pid, signal.SIGTERM)
                            worker.wait(timeout=10)
                        stop_server(process)

            if args.rustfire_first:
                (rust_result, rust_pages), (camp_result, camp_pages) = run_rustfire(), run_campfire()
            else:
                (camp_result, camp_pages), (rust_result, rust_pages) = run_campfire(), run_rustfire()
            for index, (rust_page, camp_page) in enumerate(zip(rust_pages, camp_pages), 1):
                assert rust_page == camp_page, (index, rust_page, camp_page)
        finally:
            redis.terminate()
            redis.wait(timeout=10)
            redis_log.close()
    report = {"clients": args.clients, "file_mib": args.file_mib, "campfire_workers": args.campfire_workers,
              "rustfire_first": args.rustfire_first, "rustfire": rust_result, "campfire": camp_result}
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print("PASS paired concurrent message edits, saved bytes, and background purge")
    print(json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    main()
