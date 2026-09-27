"""Measure equivalent generic message uploads under concurrent clients.

Run after cargo build --release. Both apps receive the same multipart file,
session workflow, and number of messages on disposable databases.
"""

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

from direct_lookup import free_port, p95, start_server, stop_server
from paired_banned_content import BUNDLE, REPOSITORY, REVISION, RUBY, start_redis
from paired_bot_admin import cleanup_campfire_uploads
from paired_direct_lookup import login_campfire, seed_campfire, seed_rustfire, wait_for_server
from paired_message_cache import resource_snapshot


def multipart(client_id, csrf, data):
    boundary = f"paired-upload-capacity-{client_id}"
    prefix = (
        f'--{boundary}\r\nContent-Disposition: form-data; name="authenticity_token"\r\n\r\n{csrf}\r\n'
        f'--{boundary}\r\nContent-Disposition: form-data; name="message[client_message_id]"\r\n\r\n{client_id}\r\n'
        f'--{boundary}\r\nContent-Disposition: form-data; name="message[attachment]"; filename="capacity.bin"\r\n'
        'Content-Type: application/octet-stream\r\n\r\n'
    ).encode()
    return prefix + data + f"\r\n--{boundary}--\r\n".encode(), boundary


def post(connection, cookie, csrf, client_id, data, form_csrf_only):
    payload, boundary = multipart(client_id, csrf, data)
    begun = time.perf_counter()
    headers = {
        "Cookie": cookie,
        "Accept": "text/vnd.turbo-stream.html, text/html",
        "Content-Type": f"multipart/form-data; boundary={boundary}",
    }
    if not form_csrf_only:
        headers["X-CSRF-Token"] = csrf
    connection.request("POST", "/rooms/1/messages", payload, headers)
    response = connection.getresponse()
    body = response.read()
    elapsed_ms = (time.perf_counter() - begun) * 1000
    assert response.status == 200 and response.getheader("Content-Type", "").startswith("text/vnd.turbo-stream.html"), (client_id, response.status, body[:200])
    assert f"message_{client_id}".encode() in body, (client_id, body[:200])
    return elapsed_ms


def verify(database, uploads, campfire, clients, count, data):
    with sqlite3.connect(database) as db:
        if campfire:
            rows = db.execute("""SELECT m.client_message_id,b.filename,b.content_type,b.key,b.byte_size
                FROM messages m JOIN active_storage_attachments a ON a.record_type='Message' AND a.record_id=m.id AND a.name='attachment'
                JOIN active_storage_blobs b ON b.id=a.blob_id ORDER BY m.id""").fetchall()
        else:
            rows = db.execute("""SELECT m.client_message_id,a.filename,a.content_type,a.stored_name,NULL
                FROM messages m JOIN attachments a ON a.message_id=m.id ORDER BY m.id""").fetchall()
    expected = {f"capacity-warmup-{client}" for client in range(clients)} | {
        f"capacity-{client}-{index}" for client in range(clients) for index in range(count)
    }
    assert len(rows) == len(expected) and {row[0] for row in rows} == expected, (len(rows), len(expected))
    digest = hashlib.sha256(data).digest()
    for client_id, filename, content_type, key, byte_size in rows:
        assert filename == "capacity.bin" and content_type == "application/octet-stream", (client_id, filename, content_type)
        if byte_size is not None:
            assert byte_size == len(data), (client_id, byte_size)
        path = uploads / key[:2] / key[2:4] / key if campfire else uploads / key
        with path.open("rb") as saved:
            assert hashlib.file_digest(saved, "sha256").digest() == digest, (client_id, path)
    if not campfire:
        assert not list(uploads.glob("message-upload-*")), "Rustfire left staged upload files"


def measure(process_pids, port, cookie, csrf, data, clients, count, form_csrf_only):
    ready = threading.Barrier(clients + 1, timeout=120)
    start = threading.Event()
    stop_sampling = threading.Event()
    samples = []
    sample_errors = []

    def sample_resources():
        while not stop_sampling.wait(0.05):
            try:
                samples.append(resource_snapshot(process_pids)[0])
            except Exception as error:
                sample_errors.append(error)
                return

    def worker(client):
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=120)
        latencies = []
        try:
            try:
                post(connection, cookie, csrf, f"capacity-warmup-{client}", data, form_csrf_only)
            except Exception:
                ready.abort()
                raise
            ready.wait()
            assert start.wait(30), "Timed upload start signal was not sent"
            for index in range(count):
                latencies.append(post(connection, cookie, csrf, f"capacity-{client}-{index}", data, form_csrf_only))
            return latencies
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
        samples.append(baseline_pss)
        sampler = threading.Thread(target=sample_resources, daemon=True)
        sampler.start()
        try:
            started = time.perf_counter()
            start.set()
            latencies = [latency for future in futures for latency in future.result()]
            elapsed = time.perf_counter() - started
        finally:
            stop_sampling.set()
            sampler.join(timeout=2)
    if sample_errors:
        raise RuntimeError("Server resource sampling failed") from sample_errors[0]
    final_pss, final_cpu, _ = resource_snapshot(process_pids)
    samples.append(final_pss)
    samples.sort()
    latencies.sort()
    uploads = clients * count
    return {
        "uploads": uploads,
        "warmup_uploads": clients,
        "elapsed_s": round(elapsed, 3),
        "uploads_per_s": round(uploads / elapsed, 2),
        "payload_mib_per_s": round(uploads * len(data) / elapsed / 1048576, 2),
        "median_ms": round(statistics.median(latencies), 2),
        "p95_ms": round(p95(latencies), 2),
        "baseline_pss_mib": round(baseline_pss / 1048576, 2),
        "sampled_peak_pss_mib": round(samples[-1] / 1048576, 2),
        "sampled_pss_growth_mib": round((samples[-1] - baseline_pss) / 1048576, 2),
        "memory_samples": len(samples),
        "cpu_seconds": round(final_cpu - baseline_cpu, 2),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--clients", type=int, default=4)
    parser.add_argument("--uploads-per-client", type=int, default=3)
    parser.add_argument("--file-mib", type=int, default=8)
    parser.add_argument("--campfire-workers", type=int, default=22)
    parser.add_argument("--rustfire-first", action="store_true")
    parser.add_argument("--form-csrf-only", action="store_true", help="authenticate with only the multipart form field, without the X-CSRF-Token header")
    parser.add_argument("--report", type=pathlib.Path)
    args = parser.parse_args()
    if min(args.clients, args.uploads_per_client, args.file_mib, args.campfire_workers) < 1:
        parser.error("clients, uploads per client, file MiB, and Campfire workers must be positive")
    try:
        import psutil  # noqa: F401
    except ImportError:
        parser.error("the psutil Python package is required for server resource sampling")
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPOSITORY, text=True).strip() == REVISION
    data = b"R" * (args.file_mib * 1024 * 1024)
    with tempfile.TemporaryDirectory(prefix="paired-upload-capacity-") as scratch:
        temp = pathlib.Path(scratch)
        rust_db, camp_db = temp / "rust.sqlite3", temp / "camp.sqlite3"
        rust_port, camp_port, redis_port = free_port(), free_port(), free_port()
        seed_rustfire(rust_db, rust_port, [])
        camp_env = seed_campfire(REPOSITORY, RUBY, BUNDLE, REPOSITORY / "storage/db/production.sqlite3", camp_db, [], camp_port, temp)
        camp_env["REDIS_URL"] = f"redis://127.0.0.1:{redis_port}"
        camp_env["WEB_CONCURRENCY"] = str(args.campfire_workers)
        rust_uploads = temp / "rust-uploads"
        redis, redis_log = start_redis(temp, redis_port)
        try:
            def run_rustfire():
                process = start_server(rust_db, rust_port, {"RUSTFIRE_UPLOAD_DIR": str(rust_uploads)})
                try:
                    result = measure([process.pid], rust_port, "session_token=benchmark-session", "benchmark-csrf", data, args.clients, args.uploads_per_client, args.form_csrf_only)
                    verify(rust_db, rust_uploads, False, args.clients, args.uploads_per_client, data)
                    return result
                finally:
                    stop_server(process)

            def run_campfire():
                with open(temp / "puma.log", "w+") as log:
                    process = subprocess.Popen([str(RUBY), str(RUBY.parent / "bundle"), "exec", "puma", "-C", "config/puma.rb"], cwd=REPOSITORY, env=camp_env, stdout=log, stderr=log)
                    try:
                        wait_for_server(camp_port, process)
                        cookie, csrf = login_campfire(camp_port)
                        result = measure([process.pid, redis.pid], camp_port, cookie, csrf, data, args.clients, args.uploads_per_client, args.form_csrf_only)
                        verify(camp_db, REPOSITORY / "storage/files", True, args.clients, args.uploads_per_client, data)
                        return result
                    except Exception:
                        log.flush()
                        log.seek(0)
                        print(log.read()[-3000:])
                        raise
                    finally:
                        stop_server(process)
                        cleanup_campfire_uploads(REPOSITORY / "storage/db/production.sqlite3", camp_db, REPOSITORY)

            if args.rustfire_first:
                rust_result, camp_result = run_rustfire(), run_campfire()
            else:
                camp_result, rust_result = run_campfire(), run_rustfire()
        finally:
            redis.terminate()
            redis.wait(timeout=10)
            redis_log.close()
    report = {
        "clients": args.clients, "uploads_per_client": args.uploads_per_client,
        "file_bytes": len(data), "campfire_workers": args.campfire_workers,
        "form_csrf_only": args.form_csrf_only,
        "rustfire_first": args.rustfire_first, "rustfire": rust_result, "campfire": camp_result,
    }
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print("PASS paired concurrent composer uploads and saved file hashes")
    print(json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    main()
