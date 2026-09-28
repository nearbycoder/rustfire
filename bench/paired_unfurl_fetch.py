"""Compare the complete Open Graph HTTP fetch against pinned Campfire.

The fixture is reached through a test-only connect shim. Both applications
validate the public 1.1.1.1 address normally, then connect to a local server
on the chosen fixture port. This avoids live-site and DNS variability.
"""

import http.client
import json
import os
import pathlib
import subprocess
import tempfile
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from direct_lookup import ROOT, free_port, start_server, stop_server
from paired_direct_lookup import login_campfire, seed_campfire, seed_rustfire, wait_for_server


SOURCE = pathlib.Path("/tmp/once-campfire-reference")
RUBY = pathlib.Path("/tmp/rustfire-baseline/local/bin/ruby")
BUNDLE = pathlib.Path("/tmp/rustfire-baseline/bundle")
REVISION = "91d294f4a09f9bbe37f9548959bfcb43645678fb"
CASES = (
    "good", "no_image", "empty", "redirect", "relative_redirect",
    "private_redirect", "wrong_type", "large_header", "large_body",
    "markup", "bad_image", "image_redirect", "bad_canonical", "duplicate_blank",
    "latin1", "latin1_http_equiv", "windows1252", "no_content_length", "large_stream",
)


def fixture_handler(fixture_port):
    base = f"http://1.1.1.1:{fixture_port}"

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"

        def log_message(self, *_args):
            pass

        def do_HEAD(self):
            content_type = {
                "/image.png": "image/png",
                "/image.svg": "image/svg+xml",
            }.get(self.path)
            if self.path == "/image-redirect":
                self.reply(302, b"", location=base + "/image.png")
            elif content_type:
                self.reply(200, b"", content_type=content_type)
            else:
                self.reply(404, b"")

        def do_GET(self):
            if self.path == "/redirect":
                self.reply(302, b"", location=base + "/good")
                return
            if self.path == "/relative_redirect":
                self.reply(302, b"", location="/good")
                return
            if self.path == "/private_redirect":
                self.reply(302, b"", location=f"http://127.0.0.1:{fixture_port}/good")
                return
            if self.path == "/wrong_type":
                self.reply(200, b"not html", content_type="text/plain")
                return
            if self.path == "/large_header":
                self.reply(200, b"<html></html>", content_type="text/html", content_length=5 * 1024 * 1024 + 1)
                return
            if self.path == "/large_body":
                self.reply(200, b"x" * (5 * 1024 * 1024 + 1), content_type="text/html")
                return
            if self.path == "/large_stream":
                self.reply(200, b"x" * (5 * 1024 * 1024 + 1), content_type="text/html", omit_length=True)
                return
            if self.path == "/empty":
                self.reply(200, b"<html><head></head></html>", content_type="text/html")
                return

            title, description = "Hey!", "desc.."
            if self.path == "/markup":
                title = "&#x3c;img src=x&#x3e;Hey!"
                description = "&#x3c;img src=x&#x3e;desc.."
            if self.path in ("/latin1", "/latin1_http_equiv"):
                title, description = "Café", "Résumé"
            if self.path == "/windows1252":
                title, description = "“Café”", "It’s fine"
            image = {
                "/good": base + "/image.png",
                "/redirect": base + "/image.png",
                "/bad_image": base + "/image.svg",
                "/image_redirect": base + "/image-redirect",
                "/markup": base + "/image.png",
                "/bad_canonical": base + "/image.png",
                "/duplicate_blank": base + "/image.png",
            }.get(self.path)
            canonical = "/relative" if self.path == "/bad_canonical" else base + "/canonical"
            tags = [
                f'<meta property="og:url" content="{canonical}">',
                f'<meta property="og:title" content="{title}">',
                f'<meta property="og:description" content="{description}">',
            ]
            if image:
                tags.append(f'<meta property="og:image" content="{image}">')
            if self.path == "/duplicate_blank":
                tags.extend(('<meta property="og:title" content="">', '<meta property="og:description" content="">'))
            encoding = "iso-8859-1" if self.path in ("/latin1", "/latin1_http_equiv") else "cp1252" if self.path == "/windows1252" else "utf-8"
            declared_encoding = "windows-1252" if encoding == "cp1252" else encoding
            declaration = f"<meta charset='{declared_encoding}'>"
            if self.path == "/latin1_http_equiv":
                declaration = "<meta http-equiv='Content-Type' content='text/html; charset=ISO-8859-1'>"
            body = ("<html><head>" + declaration + "".join(tags) + "</head></html>").encode(encoding)
            self.reply(200, body, content_type=f"text/html; charset={encoding}", omit_length=self.path == "/no_content_length")

        def reply(self, status, body, content_type=None, location=None, content_length=None, omit_length=False):
            self.send_response(status)
            if content_type:
                self.send_header("Content-Type", content_type)
            if location:
                self.send_header("Location", location)
            if not omit_length:
                self.send_header("Content-Length", str(len(body) if content_length is None else content_length))
            self.end_headers()
            if self.command != "HEAD":
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass

    return Handler


def request(port, cookie, csrf, fixture_port, case):
    url = f"http://1.1.1.1:{fixture_port}/{case}"
    body = urllib.parse.urlencode({"url": url}).encode()
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    try:
        connection.request("POST", "/unfurl_link", body=body, headers={
            "Cookie": cookie,
            "X-CSRF-Token": csrf,
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        })
        response = connection.getresponse()
        payload = response.read()
        return response.status, response.getheader("Content-Type", "").split(";")[0], json.loads(payload) if payload else None
    finally:
        connection.close()


def fixture_env(env, shim, port):
    env = dict(env)
    for key in list(env):
        if key.lower() in {"http_proxy", "https_proxy", "all_proxy"}:
            env.pop(key)
    env["LD_PRELOAD"] = str(shim)
    env["RUSTFIRE_UNFURL_FIXTURE_PORT"] = str(port)
    return env


def main():
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=SOURCE, text=True).strip() == REVISION
    with tempfile.TemporaryDirectory(prefix="paired-unfurl-fetch-") as scratch:
        temp = pathlib.Path(scratch)
        shim = temp / "fixture-connect.so"
        subprocess.run(["cc", "-shared", "-fPIC", "-O2", "-o", str(shim), str(ROOT / "bench/unfurl_fixture_connect.c"), "-ldl"], check=True)
        fixture_port, rust_port, camp_port = free_port(), free_port(), free_port()
        fixture = ThreadingHTTPServer(("127.0.0.1", fixture_port), fixture_handler(fixture_port))
        thread = threading.Thread(target=fixture.serve_forever, daemon=True)
        thread.start()
        try:
            rust_db, camp_db = temp / "rust.sqlite3", temp / "camp.sqlite3"
            seed_rustfire(rust_db, rust_port, [])
            camp_env = seed_campfire(SOURCE, RUBY, BUNDLE, SOURCE / "storage/db/production.sqlite3", camp_db, [], camp_port, temp)
            rust = start_server(rust_db, rust_port, fixture_env({}, shim, fixture_port))
            try:
                rust_results = [request(rust_port, "session_token=benchmark-session", "benchmark-csrf", fixture_port, case) for case in CASES]
            finally:
                stop_server(rust)
            camp_env = fixture_env(camp_env, shim, fixture_port)
            with open(temp / "puma.log", "w+") as log:
                camp = subprocess.Popen([str(RUBY), str(RUBY.parent / "bundle"), "exec", "puma", "-C", "config/puma.rb"], cwd=SOURCE, env=camp_env, stdout=log, stderr=log)
                try:
                    wait_for_server(camp_port, camp)
                    cookie, csrf = login_campfire(camp_port)
                    camp_results = [request(camp_port, cookie, csrf, fixture_port, case) for case in CASES]
                except Exception:
                    log.flush()
                    log.seek(0)
                    print(log.read()[-3000:])
                    raise
                finally:
                    stop_server(camp)
        finally:
            fixture.shutdown()
            fixture.server_close()
            thread.join(timeout=5)
    differences = [(case, rust, camp) for case, rust, camp in zip(CASES, rust_results, camp_results) if rust != camp]
    for case, rust, camp in differences:
        print(f"{case}: Rustfire={rust!r} Campfire={camp!r}")
    assert not differences, f"{len(differences)} Open Graph fetch cases differ"
    print(f"PASS {len(CASES)} paired Open Graph fetch cases")


if __name__ == "__main__":
    main()
