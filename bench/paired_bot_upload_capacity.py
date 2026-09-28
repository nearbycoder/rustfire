"""Measure equivalent concurrent bot file uploads against pinned Campfire."""

import argparse
import concurrent.futures
import hashlib
import http.client
import json
import pathlib
import sqlite3
import statistics
import subprocess
import tempfile
import threading
import time
from urllib.parse import urlsplit

from direct_lookup import free_port, p95, start_server, stop_server
from paired_banned_content import BUNDLE, REPOSITORY, REVISION, RUBY, isolated_campfire, start_redis
from paired_direct_lookup import seed_campfire, seed_rustfire, wait_for_server
from paired_mention_webhook import BOT_ID, BOT_TOKEN, seed_bot
from paired_message_cache import resource_snapshot


def multipart(data):
    boundary = "paired-bot-upload-capacity"
    prefix = (f'--{boundary}\r\nContent-Disposition: form-data; name="attachment"; filename="capacity.bin"\r\n'
              'Content-Type: application/octet-stream\r\n\r\n').encode()
    return prefix + data + f"\r\n--{boundary}--\r\n".encode(), boundary


def post(connection, payload, boundary):
    begun = time.perf_counter()
    connection.request("POST", f"/rooms/1/{BOT_ID}-{BOT_TOKEN}/messages", payload,
                       {"Accept": "application/json", "Content-Type": f"multipart/form-data; boundary={boundary}"})
    response = connection.getresponse()
    body = response.read()
    elapsed_ms = (time.perf_counter() - begun) * 1000
    location = urlsplit(response.getheader("Location", "")).path
    assert response.status == 201 and response.getheader("Content-Type", "").startswith("application/json") \
        and location.startswith("/messages/") and not body, (response.status, location, body[:200])
    return int(location.rsplit("/", 1)[1]), elapsed_ms


def measure(process_pids, port, payload, boundary, file_bytes, clients, count):
    ready = threading.Barrier(clients + 1, timeout=120)
    start = threading.Event()
    stop_sampling = threading.Event()
    memory_samples = []
    sample_errors = []

    def sample_resources():
        while not stop_sampling.wait(0.05):
            try:
                memory_samples.append(resource_snapshot(process_pids)[0])
            except Exception as error:
                sample_errors.append(error)
                return

    def worker(_client):
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=120)
        try:
            try:
                warmup_id, _ = post(connection, payload, boundary)
            except Exception:
                ready.abort()
                raise
            ready.wait()
            assert start.wait(30), "Timed upload start signal was not sent"
            pairs = [post(connection, payload, boundary) for _ in range(count)]
            return warmup_id, pairs
        finally:
            connection.close()

    with concurrent.futures.ThreadPoolExecutor(max_workers=clients) as executor:
        futures = [executor.submit(worker, client) for client in range(clients)]
        try:
            ready.wait()
        except threading.BrokenBarrierError:
            for future in futures:
                future.result(timeout=30)
            raise
        baseline_pss, baseline_cpu, _ = resource_snapshot(process_pids)
        memory_samples.append(baseline_pss)
        sampler = threading.Thread(target=sample_resources, daemon=True)
        sampler.start()
        try:
            begun = time.perf_counter()
            start.set()
            results = [future.result() for future in futures]
            elapsed = time.perf_counter() - begun
        finally:
            stop_sampling.set()
            sampler.join(timeout=2)
    if sample_errors:
        raise RuntimeError("Server resource sampling failed") from sample_errors[0]
    final_pss, final_cpu, _ = resource_snapshot(process_pids)
    memory_samples.append(final_pss)
    latencies = [latency for _, pairs in results for _, latency in pairs]
    ids = [warmup for warmup, _ in results] + [message_id for _, pairs in results for message_id, _ in pairs]
    assert len(set(ids)) == len(ids), "Duplicate message IDs in bot upload responses"
    uploads = clients * count
    return ids, {
        "uploads": uploads,
        "warmup_uploads": clients,
        "elapsed_s": round(elapsed, 3),
        "uploads_per_s": round(uploads / elapsed, 2),
        "payload_mib_per_s": round(uploads * file_bytes / 1048576 / elapsed, 2),
        "median_ms": round(statistics.median(latencies), 2),
        "p95_ms": round(p95(latencies), 2),
        "baseline_pss_mib": round(baseline_pss / 1048576, 2),
        "sampled_peak_pss_mib": round(max(memory_samples) / 1048576, 2),
        "memory_samples": len(memory_samples),
        "cpu_seconds": round(final_cpu - baseline_cpu, 2),
    }


def verify(database, uploads, campfire, ids, data):
    with sqlite3.connect(database) as db:
        if campfire:
            rows = db.execute("""SELECT m.id,b.filename,b.content_type,b.key,b.byte_size
                FROM messages m JOIN active_storage_attachments a ON a.record_type='Message' AND a.record_id=m.id AND a.name='attachment'
                JOIN active_storage_blobs b ON b.id=a.blob_id ORDER BY m.id""").fetchall()
        else:
            rows = db.execute("""SELECT m.id,a.filename,a.content_type,a.stored_name,NULL
                FROM messages m JOIN attachments a ON a.message_id=m.id ORDER BY m.id""").fetchall()
    assert {row[0] for row in rows} == set(ids) and len(rows) == len(ids), (len(rows), len(ids))
    digest = hashlib.sha256(data).digest()
    for message_id, filename, content_type, stored, byte_size in rows:
        assert filename == "capacity.bin" and content_type == "application/octet-stream", (message_id, filename, content_type)
        if byte_size is not None:
            assert byte_size == len(data), (message_id, byte_size)
        path = uploads / stored[:2] / stored[2:4] / stored if campfire else uploads / stored
        with path.open("rb") as saved:
            assert hashlib.file_digest(saved, "sha256").digest() == digest, (message_id, path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--clients", type=int, default=8)
    parser.add_argument("--uploads-per-client", type=int, default=3)
    parser.add_argument("--file-mib", type=int, default=8)
    parser.add_argument("--campfire-workers", type=int, default=22)
    parser.add_argument("--rustfire-first", action="store_true")
    parser.add_argument("--report", type=pathlib.Path)
    args = parser.parse_args()
    if min(args.clients, args.uploads_per_client, args.file_mib, args.campfire_workers) < 1:
        parser.error("all count and size options must be positive")
    import psutil  # noqa: F401
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPOSITORY, text=True).strip() == REVISION
    data = b"R" * (args.file_mib * 1024 * 1024)
    payload, boundary = multipart(data)
    with tempfile.TemporaryDirectory(prefix="paired-bot-upload-capacity-") as scratch:
        temp = pathlib.Path(scratch)
        rust_db, camp_db = temp / "rust.sqlite3", temp / "camp.sqlite3"
        rust_port, camp_port, redis_port = free_port(), free_port(), free_port()
        seed_rustfire(rust_db, rust_port, [])
        checkout = isolated_campfire(temp, redis_port)
        camp_env = seed_campfire(checkout, RUBY, BUNDLE, REPOSITORY / "storage/db/production.sqlite3", camp_db, [], camp_port, temp)
        camp_env["REDIS_URL"] = f"redis://127.0.0.1:{redis_port}"
        camp_env["WEB_CONCURRENCY"] = str(args.campfire_workers)
        seed_bot(rust_db, False, "", with_webhooks=False)
        seed_bot(camp_db, True, "", with_webhooks=False)
        rust_uploads = temp / "rust-uploads"
        redis, redis_log = start_redis(temp, redis_port)
        try:
            def run_rustfire():
                process = start_server(rust_db, rust_port, {"RUSTFIRE_UPLOAD_DIR": str(rust_uploads), "RUSTFIRE_DISABLE_WEBHOOKS": "1"})
                try:
                    ids, result = measure([process.pid], rust_port, payload, boundary, len(data), args.clients, args.uploads_per_client)
                    verify(rust_db, rust_uploads, False, ids, data)
                    return result
                finally:
                    stop_server(process)

            def run_campfire():
                with open(temp / "puma.log", "w+") as log:
                    process = subprocess.Popen([str(RUBY), str(RUBY.parent / "bundle"), "exec", "puma", "-C", "config/puma.rb"],
                                               cwd=checkout, env=camp_env, stdout=log, stderr=log)
                    try:
                        wait_for_server(camp_port, process)
                        ids, result = measure([process.pid, redis.pid], camp_port, payload, boundary, len(data), args.clients, args.uploads_per_client)
                        verify(camp_db, checkout / "storage/files", True, ids, data)
                        return result
                    except Exception:
                        log.flush()
                        log.seek(0)
                        print(log.read()[-3000:])
                        raise
                    finally:
                        stop_server(process)

            if args.rustfire_first:
                rust_result, camp_result = run_rustfire(), run_campfire()
            else:
                camp_result, rust_result = run_campfire(), run_rustfire()
        finally:
            redis.terminate()
            redis.wait(timeout=10)
            redis_log.close()
    report = {"clients": args.clients, "uploads_per_client": args.uploads_per_client,
              "file_bytes": len(data), "campfire_workers": args.campfire_workers,
              "rustfire_first": args.rustfire_first, "rustfire": rust_result, "campfire": camp_result}
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print("PASS paired concurrent bot uploads and saved file hashes")
    print(json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    main()
