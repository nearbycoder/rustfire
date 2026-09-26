"""Compare Rustfire QR SVG bytes with pinned Campfire's RQRCode renderer."""

import base64
import argparse
import hashlib
import gzip
import http.client
import json
import os
import pathlib
import subprocess
import tempfile

from direct_lookup import free_port, start_server, stop_server
from paired_avatar import BUNDLE, REPOSITORY, REVISION, RUBY
from paired_banned_content import start_redis
from paired_direct_lookup import seed_campfire, wait_for_server


URLS = [
    "http://example.com",
    "https://camp.example/join/abc123",
    "https://camp.example/session/transfers/eyJraWQiOiJkZXZpY2UifQ--signed",
    "12345678901234567890",
    "1" * 33,
    "1" * 34,
    "1" * 35,
    "HELLO WORLD",
    "https://example.com/join/%F0%9F%94%A5?x=1&y=2",
    "https://camp.example/join/" + "a" * 100,
]


def campfire_svg(urls):
    env = dict(os.environ)
    env["PATH"] = str(RUBY.parent) + os.pathsep + env.get("PATH", "")
    env["BUNDLE_PATH"] = str(BUNDLE)
    env["BUNDLE_WITHOUT"] = "development:test"
    script = 'puts JSON.parse(STDIN.read).map { |url| RQRCode::QRCode.new(url).as_svg(viewbox: true, fill: :white, color: :black) }.to_json'
    output = subprocess.check_output(
        [str(RUBY), str(RUBY.parent / "bundle"), "exec", "ruby", "-rjson", "-rrqrcode", "-e", script],
        input=json.dumps(urls).encode(), cwd=REPOSITORY, env=env,
    )
    return [svg.encode() for svg in json.loads(output)]


def qr_response(port, url, etag=None, accept_encoding=None):
    encoded = base64.urlsafe_b64encode(url.encode()).decode().rstrip("=")
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        headers = {}
        if etag:
            headers["If-None-Match"] = etag
        if accept_encoding:
            headers["Accept-Encoding"] = accept_encoding
        connection.request("GET", f"/qr_code/{encoded}", headers=headers)
        response = connection.getresponse()
        body = response.read()
        return response.status, dict(response.getheaders()), body
    finally:
        connection.close()


def header_value(headers, name):
    return next((value for key, value in headers.items() if key.lower() == name.lower()), None)


def benchmark(temp, rust_port, camp_port, rust_db, env, clients, seconds, workers, svg):
    binary = temp / "checked_get"
    subprocess.run(["go", "build", "-o", str(binary), "bench/checked_get.go"], check=True)
    url = URLS[1]
    path = "/qr_code/" + base64.urlsafe_b64encode(url.encode()).decode().rstrip("=")
    etag = 'W/"' + hashlib.sha256(svg).hexdigest()[:32] + '"'
    results = []
    for rust_first in (True, False):
        for app in (("rustfire", "campfire") if rust_first else ("campfire", "rustfire")):
            log = None
            if app == "rustfire":
                process = start_server(rust_db, rust_port)
                port = rust_port
            else:
                benchmark_env = dict(env, WEB_CONCURRENCY=str(workers))
                log = open(temp / f"qr-bench-{len(results)}.log", "w+")
                process = subprocess.Popen([str(RUBY), str(RUBY.parent / "bundle"), "exec", "puma", "-C", "config/puma.rb"], cwd=REPOSITORY, env=benchmark_env, stdout=log, stderr=log)
                try:
                    wait_for_server(camp_port, process)
                except Exception:
                    log.flush()
                    log.seek(0)
                    print(log.read()[-3000:])
                    stop_server(process)
                    log.close()
                    raise
                port = camp_port
            try:
                for count in clients:
                    for conditional in (False, True):
                        command = [str(binary), "--base", f"http://127.0.0.1:{port}", "--path", path,
                                   "--cookie", "probe=1", "--expected-sha256", hashlib.sha256(b"" if conditional else svg).hexdigest(),
                                   "--expected-etag", etag, "--expected-status", "304" if conditional else "200",
                                   "--clients", str(count), "--seconds", str(seconds), "--accept", "image/svg+xml"]
                        if conditional:
                            command += ["--if-none-match", etag]
                        else:
                            command += ["--expected-content-type", "image/svg+xml", "--expected-gzip"]
                        result = subprocess.run(command, capture_output=True, text=True)
                        assert result.returncode == 0, (command, result.stdout[-1000:], result.stderr[-2000:])
                        report = json.loads(result.stdout)
                        assert report["errors"] == 0 and report["successes"] > 0, report
                        results.append({"app": app, "rust_first": rust_first, "clients": count, "conditional": conditional, **report})
                        print(f"{app} rust_first={rust_first} clients={count} status={'304' if conditional else '200'} "
                              f"rps={report['rps']:.0f} p95_ms={report['p95_ms']:.2f} errors={report['errors']}", flush=True)
            finally:
                stop_server(process)
                if log:
                    log.close()
    return results


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--benchmark", action="store_true")
    parser.add_argument("--clients", nargs="+", type=int, default=[32, 128])
    parser.add_argument("--seconds", type=float, default=5)
    parser.add_argument("--campfire-workers", type=int, default=22)
    parser.add_argument("--report", type=pathlib.Path)
    args = parser.parse_args()
    if any(count < 1 for count in args.clients) or args.seconds <= 0 or args.campfire_workers < 1:
        parser.error("clients, seconds, and Campfire workers must be positive")
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPOSITORY, text=True).strip()
    assert revision == REVISION, revision
    expected = campfire_svg(URLS)
    with tempfile.TemporaryDirectory(prefix="paired-qr-") as scratch:
        temp = pathlib.Path(scratch)
        rust_port, camp_port, redis_port = free_port(), free_port(), free_port()
        source_db = REPOSITORY / "storage/db/production.sqlite3"
        camp_db = temp / "camp.sqlite3"
        env = seed_campfire(REPOSITORY, RUBY, BUNDLE, source_db, camp_db, [], camp_port, temp)
        env["REDIS_URL"] = f"redis://127.0.0.1:{redis_port}"
        redis, redis_log = start_redis(temp, redis_port)
        try:
            rust = start_server(temp / "rust.sqlite3", rust_port)
            try:
                with open(temp / "puma.log", "w+") as log:
                    camp = subprocess.Popen([str(RUBY), str(RUBY.parent / "bundle"), "exec", "puma", "-C", "config/puma.rb"], cwd=REPOSITORY, env=env, stdout=log, stderr=log)
                    try:
                        wait_for_server(camp_port, camp)
                        for url, svg in zip(URLS, expected):
                            rust_status, rust_headers, actual = qr_response(rust_port, url)
                            camp_status, camp_headers, source = qr_response(camp_port, url)
                            assert rust_status == camp_status == 200, (url, rust_status, camp_status)
                            assert actual == source == svg, (url, len(actual), len(source), len(svg), next((i for i, (a, b) in enumerate(zip(actual, source)) if a != b), None))
                            for name in ("Content-Type", "Cache-Control", "ETag", "Vary"):
                                assert header_value(rust_headers, name) == header_value(camp_headers, name), (url, name, rust_headers, camp_headers)
                            etag = header_value(camp_headers, "ETag")
                            assert etag and etag.startswith('W/"'), (url, etag)
                            rust_304, rust_cond_headers, rust_cond_body = qr_response(rust_port, url, etag)
                            camp_304, camp_cond_headers, camp_cond_body = qr_response(camp_port, url, etag)
                            assert rust_304 == camp_304 == 304 and rust_cond_body == camp_cond_body == b"", url
                            for name in ("Content-Type", "Cache-Control", "ETag", "Vary"):
                                assert header_value(rust_cond_headers, name) == header_value(camp_cond_headers, name), (url, name, rust_cond_headers, camp_cond_headers)
                            if url == URLS[1]:
                                rust_gzip_status, rust_gzip_headers, rust_gzip_body = qr_response(rust_port, url, accept_encoding="gzip")
                                camp_gzip_status, camp_gzip_headers, camp_gzip_body = qr_response(camp_port, url, accept_encoding="gzip")
                                assert rust_gzip_status == camp_gzip_status == 200, url
                                assert header_value(rust_gzip_headers, "Content-Encoding") == header_value(camp_gzip_headers, "Content-Encoding") == "gzip", url
                                for name in ("Content-Type", "Cache-Control", "ETag", "Vary"):
                                    assert header_value(rust_gzip_headers, name) == header_value(camp_gzip_headers, name), (url, name, rust_gzip_headers, camp_gzip_headers)
                                assert gzip.decompress(rust_gzip_body) == gzip.decompress(camp_gzip_body) == svg, url
                    except Exception:
                        log.flush()
                        log.seek(0)
                        print(log.read()[-3000:])
                        raise
                    finally:
                        stop_server(camp)
            finally:
                stop_server(rust)
            if args.benchmark:
                results = benchmark(temp, rust_port, camp_port, temp / "rust.sqlite3", env,
                                    args.clients, args.seconds, args.campfire_workers, expected[1])
                if args.report:
                    args.report.parent.mkdir(parents=True, exist_ok=True)
                    args.report.write_text(json.dumps(results, indent=2) + "\n")
        finally:
            redis.terminate()
            redis.wait(timeout=10)
            redis_log.close()
    print(f"PASS {len(URLS)} Campfire/Rustfire QR SVGs, cache headers, gzip, and conditional 304 responses")


if __name__ == "__main__":
    main()
