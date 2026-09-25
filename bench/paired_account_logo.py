"""Compare account logo uploads and rendered variants with pinned Campfire.

Run after ``cargo build --release`` with the pinned Ruby bundle and Redis.
"""

import argparse
import concurrent.futures
import hashlib
import http.client
import pathlib
import shutil
import sqlite3
import statistics
import subprocess
import tempfile
import threading
import time
import urllib.parse

from direct_lookup import free_port, p95, start_server, stop_server
from paired_bot_admin import cleanup_campfire_uploads, request
from paired_direct_lookup import login_campfire, seed_campfire, seed_rustfire, wait_for_server


def png_summary(body):
    assert body[:8] == b"\x89PNG\r\n\x1a\n", body[:16]
    return int.from_bytes(body[16:20], "big"), int.from_bytes(body[20:24], "big"), hashlib.sha256(body).hexdigest()


def multipart(jpeg):
    boundary = b"account-logo-probe"
    body = (
        b"--" + boundary + b"\r\nContent-Disposition: form-data; name=\"account[name]\"\r\n\r\nLogo Probe\r\n"
        b"--" + boundary + b"\r\nContent-Disposition: form-data; name=\"account[settings][restrict_room_creation_to_administrators]\"\r\n\r\ntrue\r\n"
        b"--" + boundary + b"\r\nContent-Disposition: form-data; name=\"account[logo]\"; filename=\"moon.jpg\"\r\nContent-Type: image/jpeg\r\n\r\n"
        + jpeg + b"\r\n--" + boundary + b"--\r\n"
    )
    return body, "multipart/form-data; boundary=" + boundary.decode()


def measure_concurrent(port, expected, clients, seconds):
    ready = threading.Barrier(clients + 1, timeout=30)
    start = threading.Event()
    clock = {}

    def worker():
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
        samples = []
        errors = 0
        try:
            for _ in range(2):
                connection.request("GET", "/account/logo")
                response = connection.getresponse()
                if response.status != 200 or response.read() != expected:
                    raise AssertionError("Logo warmup differs from the verified PNG")
            ready.wait()
            start.wait()
            while time.perf_counter() < clock["deadline"]:
                begun = time.perf_counter()
                try:
                    connection.request("GET", "/account/logo")
                    response = connection.getresponse()
                    body = response.read()
                    if response.status == 200 and body == expected:
                        samples.append((time.perf_counter() - begun) * 1000)
                    else:
                        errors += 1
                except (OSError, ValueError):
                    errors += 1
                    connection.close()
                    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
        finally:
            connection.close()
        return samples, errors, time.perf_counter()

    with concurrent.futures.ThreadPoolExecutor(max_workers=clients) as executor:
        futures = [executor.submit(worker) for _ in range(clients)]
        ready.wait()
        clock["begun"] = time.perf_counter()
        clock["deadline"] = clock["begun"] + seconds
        start.set()
        workers = [future.result() for future in futures]
    samples = [elapsed for worker_samples, _, _ in workers for elapsed in worker_samples]
    errors = sum(worker_errors for _, worker_errors, _ in workers)
    duration = max(ended for _, _, ended in workers) - clock["begun"]
    assert samples and errors == 0, (len(samples), errors)
    return len(samples) / duration, statistics.median(samples), p95(samples), errors, len(samples)


def workflow(port, cookie, csrf, database, campfire, jpeg, bmp, sample_dir, clients, seconds):
    result = {}
    performance = {}
    sample_dir.mkdir()
    for size, path in (("large", "/account/logo"), ("small", "/account/logo?size=small")):
        status, _, body = request(port, "GET", path, "", "")
        assert status == 200
        result[f"stock_{size}"] = png_summary(body)

    body, content_type = multipart(jpeg)
    status, location, payload = request(port, "PATCH", "/account", cookie, csrf, body, content_type)
    assert status in (302, 303), (status, payload[:300])
    result["update"] = status, urllib.parse.urlsplit(location).path
    with sqlite3.connect(database) as db:
        name = db.execute("SELECT name FROM accounts WHERE id=1").fetchone()[0]
        if campfire:
            settings = db.execute("SELECT settings FROM accounts WHERE id=1").fetchone()[0]
            restricted = '"restrict_room_creation_to_administrators":true' in settings.replace(" ", "")
            count = db.execute("SELECT COUNT(*) FROM active_storage_attachments WHERE record_type='Account' AND record_id=1 AND name='logo'").fetchone()[0]
        else:
            restricted = bool(db.execute("SELECT restrict_room_creation FROM account_settings WHERE id=1").fetchone()[0])
            count = db.execute("SELECT COUNT(*) FROM account_logos WHERE id=1").fetchone()[0]
        result["persisted"] = name, restricted, count
    for size, path in (("large", "/account/logo"), ("small", "/account/logo?size=small")):
        status, _, body = request(port, "GET", path, "", "")
        assert status == 200
        result[f"custom_{size}"] = png_summary(body)
        (sample_dir / f"{size}.png").write_bytes(body)
        if size == "large":
            for count in clients:
                performance[count] = measure_concurrent(port, body, count, seconds)

    status, location, payload = request(port, "DELETE", "/account/logo", cookie, csrf)
    assert status in (302, 303), (status, payload[:300])
    result["delete"] = status, urllib.parse.urlsplit(location).path
    for size, path in (("large", "/account/logo"), ("small", "/account/logo?size=small")):
        status, _, body = request(port, "GET", path, "", "")
        assert status == 200
        result[f"restored_{size}"] = png_summary(body)
    boundary = b"account-bmp-probe"
    bmp_body = b"--" + boundary + b"\r\nContent-Disposition: form-data; name=\"account[logo]\"; filename=\"pixel.bmp\"\r\nContent-Type: image/bmp\r\n\r\n" + bmp + b"\r\n--" + boundary + b"--\r\n"
    status, location, payload = request(port, "PATCH", "/account", cookie, csrf, bmp_body, "multipart/form-data; boundary=" + boundary.decode())
    assert status in (302,303), (status,payload[:300])
    result["bmp_update"] = status, urllib.parse.urlsplit(location).path
    for size, path in (("large", "/account/logo"), ("small", "/account/logo?size=small")):
        status, _, body = request(port, "GET", path, "", "")
        assert status == 200
        result[f"bmp_fallback_{size}"] = png_summary(body)
    return result, performance


def pixel_hash(path):
    raw = path.with_suffix(".raw")
    subprocess.run(["vips", "rawsave", str(path), str(raw)], check=True, stderr=subprocess.DEVNULL)
    return hashlib.sha256(raw.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--campfire-repo", type=pathlib.Path, default=pathlib.Path("/tmp/once-campfire-reference"))
    parser.add_argument("--ruby", type=pathlib.Path, default=pathlib.Path("/tmp/rustfire-baseline/local/bin/ruby"))
    parser.add_argument("--bundle-path", type=pathlib.Path, default=pathlib.Path("/tmp/rustfire-baseline/bundle"))
    parser.add_argument("--sample-dir", type=pathlib.Path, help="Keep the four uploaded-logo PNG responses for inspection")
    parser.add_argument("--clients", type=int, nargs="*", default=[], help="concurrent clients for the uploaded 512-pixel PNG")
    parser.add_argument("--seconds", type=float, default=3.0, help="duration of each concurrent trial")
    parser.add_argument("--campfire-workers", type=int, default=1, help="Puma workers; the packaged default on this host is 22")
    args = parser.parse_args()
    if any(client < 1 for client in args.clients) or args.seconds <= 0 or args.campfire_workers < 1:
        parser.error("client counts, duration, and Puma workers must be positive")
    repository, ruby, bundle_path = args.campfire_repo.resolve(), args.ruby.resolve(), args.bundle_path.resolve()
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repository, text=True).strip()
    assert revision == "91d294f4a09f9bbe37f9548959bfcb43645678fb", revision
    jpeg = (repository / "test/fixtures/files/moon.jpg").read_bytes()
    bmp = (repository / "test/fixtures/files/pixel.bmp").read_bytes()
    with tempfile.TemporaryDirectory(prefix="paired-account-logo-") as temporary:
        temp = pathlib.Path(temporary)
        rust_db, camp_db = temp / "rustfire.sqlite3", temp / "campfire.sqlite3"
        rust_port, camp_port = free_port(), free_port()
        seed_rustfire(rust_db, rust_port, [])
        source_database = repository / "storage/db/production.sqlite3"
        env = seed_campfire(repository, ruby, bundle_path, source_database, camp_db, [], camp_port, temp)
        rust_process = start_server(rust_db, rust_port, {"RUSTFIRE_UPLOAD_DIR": str(temp / "uploads")})
        try:
            rust, rust_performance = workflow(rust_port, "session_token=benchmark-session", "benchmark-csrf", rust_db, False, jpeg, bmp, temp / "rust", args.clients, args.seconds)
        finally:
            stop_server(rust_process)
        log = open(temp / "puma.log", "w+")
        env["WEB_CONCURRENCY"] = str(args.campfire_workers)
        camp_process = subprocess.Popen([str(ruby), str(ruby.parent / "bundle"), "exec", "puma", "-C", "config/puma.rb"], cwd=repository, env=env, stdout=log, stderr=log)
        try:
            wait_for_server(camp_port, camp_process)
            cookie, csrf = login_campfire(camp_port)
            camp, camp_performance = workflow(camp_port, cookie, csrf, camp_db, True, jpeg, bmp, temp / "camp", args.clients, args.seconds)
        except Exception:
            log.flush()
            log.seek(0)
            print(log.read()[-3000:])
            raise
        finally:
            stop_server(camp_process)
            log.close()
            cleanup_campfire_uploads(source_database, camp_db, repository)
        for key in rust:
            assert rust[key]==camp[key],(key,rust[key],camp[key])
            print(f"{key}: rustfire={rust[key]!r} campfire={camp[key]!r}")
        for size in ("large", "small"):
            rust_pixels = pixel_hash(temp / "rust" / f"{size}.png")
            camp_pixels = pixel_hash(temp / "camp" / f"{size}.png")
            assert rust_pixels==camp_pixels,(size,rust_pixels,camp_pixels)
            print(f"custom_{size}_pixels: rustfire={rust_pixels} campfire={camp_pixels} equal={rust_pixels==camp_pixels}")
        for count in args.clients:
            print(f"clients={count} seconds={args.seconds:g} campfire_workers={args.campfire_workers} logo_bytes={(temp / 'rust' / 'large.png').stat().st_size} "
                  f"rustfire_rps={rust_performance[count][0]:.1f} rustfire_median_ms={rust_performance[count][1]:.3f} rustfire_p95_ms={rust_performance[count][2]:.3f} rustfire_errors={rust_performance[count][3]} "
                  f"campfire_rps={camp_performance[count][0]:.1f} campfire_median_ms={camp_performance[count][1]:.3f} campfire_p95_ms={camp_performance[count][2]:.3f} campfire_errors={camp_performance[count][3]}")
        if args.sample_dir:
            args.sample_dir.mkdir(parents=True,exist_ok=True)
            for app in ("rust","camp"):
                for size in ("large","small"):
                    shutil.copyfile(temp / app / f"{size}.png", args.sample_dir / f"{app}-{size}.png")


if __name__ == "__main__":
    main()
