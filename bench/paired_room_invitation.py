"""Compare the original-room invitation lifecycle with pinned Campfire.

Run after ``cargo build --release`` with the pinned Ruby bundle and Redis available.
"""

import argparse
import base64
import html
import http.client
import pathlib
import re
import sqlite3
import subprocess
import tempfile
import urllib.parse

from direct_lookup import free_port, start_server, stop_server
from paired_bot_admin import request
from paired_direct_lookup import login_campfire, seed_campfire, seed_rustfire, wait_for_server


def room(port, cookie, csrf, room_id):
    status, _, page = request(port, "GET", f"/rooms/{room_id}", cookie, csrf)
    assert status == 200, (status, page[:200])
    return page.decode()


def invitation_contract(page, port, brand):
    assert 'id="system_welcome"' in page or "id='system_welcome'" in page
    assert f"Welcome to {brand}" in page
    assert "To invite people to chat, share the join link below." in page
    invite = re.search(r'id=["\']invite_url["\'][^>]*value=["\']([^"\']+)', page)
    if not invite:
        invite = re.search(r'value=["\']([^"\']+)["\'][^>]*id=["\']invite_url["\']', page)
    assert invite, "Original room has no join URL"
    url = html.unescape(invite.group(1))
    assert url.startswith(f"http://127.0.0.1:{port}/join/"), url
    qr = re.search(r'/qr_code/([A-Za-z0-9_=-]+)', page)
    assert qr, "Original room has no QR link"
    assert base64.urlsafe_b64decode(qr.group(1) + "===").decode() == url
    assert "Copy join link" in page and "Share join link" in page
    copy = re.search(r'data-copy-to-clipboard-content-value=["\']([^"\']+)', page)
    assert copy and html.unescape(copy.group(1)) == url


def post_message(port, cookie, csrf, number):
    body = urllib.parse.urlencode({
        "message[body]": f"Invitation lifecycle {number}",
        "message[client_message_id]": f"invitation-{number}",
    }).encode()
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    try:
        connection.request("POST", "/rooms/1/messages", body, {
            "Cookie": cookie,
            "X-CSRF-Token": csrf,
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "text/vnd.turbo-stream.html, text/html",
        })
        response = connection.getresponse()
        payload = response.read()
        assert response.status in (200, 201, 302, 303), (response.status, payload[:200])
    finally:
        connection.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--campfire-repo", type=pathlib.Path, default=pathlib.Path("/tmp/once-campfire-reference"))
    parser.add_argument("--ruby", type=pathlib.Path, default=pathlib.Path("/tmp/rustfire-baseline/local/bin/ruby"))
    parser.add_argument("--bundle-path", type=pathlib.Path, default=pathlib.Path("/tmp/rustfire-baseline/bundle"))
    args = parser.parse_args()
    repository, ruby, bundle_path = args.campfire_repo.resolve(), args.ruby.resolve(), args.bundle_path.resolve()
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repository, text=True).strip()
    assert revision == "91d294f4a09f9bbe37f9548959bfcb43645678fb", revision
    with tempfile.TemporaryDirectory(prefix="paired-room-invitation-") as directory:
        temp = pathlib.Path(directory)
        rust_db, camp_db = temp / "rust.sqlite3", temp / "camp.sqlite3"
        rust_port, camp_port = free_port(), free_port()
        peers = [(2, 3, 4)]
        seed_rustfire(rust_db, rust_port, peers)
        env = seed_campfire(repository, ruby, bundle_path, repository / "storage/db/production.sqlite3", camp_db, peers, camp_port, temp)
        with sqlite3.connect(rust_db) as db:
            db.execute("UPDATE rooms SET name='All Talk' WHERE id=1")
            db.execute("UPDATE rooms SET created_at='2026-12-31T00:00:00Z' WHERE id=2")
        with sqlite3.connect(camp_db) as db:
            db.execute("UPDATE rooms SET created_at='2026-12-31 00:00:00' WHERE id=2")
        rust = start_server(rust_db, rust_port, {"RUSTFIRE_CAMPFIRE_SECRET_KEY_BASE": env["SECRET_KEY_BASE"]})
        log = open(temp / "puma.log", "w+")
        camp = subprocess.Popen([str(ruby), str(ruby.parent / "bundle"), "exec", "puma", "-C", "config/puma.rb"], cwd=repository, env=env, stdout=log, stderr=log)
        try:
            wait_for_server(camp_port, camp)
            camp_cookie, camp_csrf = login_campfire(camp_port)
            rust_cookie, rust_csrf = "session_token=benchmark-session", "benchmark-csrf"
            for port, cookie, csrf, brand in ((camp_port, camp_cookie, camp_csrf, "Campfire"), (rust_port, rust_cookie, rust_csrf, "Rustfire")):
                invitation_contract(room(port, cookie, csrf, 1), port, brand)
                assert "system_welcome" not in room(port, cookie, csrf, 2)
            for number in range(1, 42):
                post_message(camp_port, camp_cookie, camp_csrf, number)
                post_message(rust_port, rust_cookie, rust_csrf, number)
                if number in (1, 40):
                    invitation_contract(room(camp_port, camp_cookie, camp_csrf, 1), camp_port, "Campfire")
                    invitation_contract(room(rust_port, rust_cookie, rust_csrf, 1), rust_port, "Rustfire")
            assert "system_welcome" not in room(camp_port, camp_cookie, camp_csrf, 1)
            assert "system_welcome" not in room(rust_port, rust_cookie, rust_csrf, 1)
            print("PASS original room invitation, QR, non-original room, and 40/41-message cutoff")
        except Exception:
            log.flush()
            log.seek(0)
            print(log.read()[-3000:])
            raise
        finally:
            stop_server(camp)
            stop_server(rust)
            log.close()


if __name__ == "__main__":
    main()
