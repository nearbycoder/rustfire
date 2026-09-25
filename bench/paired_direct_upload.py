"""Check authenticated Active Storage direct uploads against pinned Campfire.

Run after cargo build --release with the pinned Ruby bundle and Redis.
"""

import base64
import concurrent.futures
import hashlib
import http.client
import json
import pathlib
import re
import sqlite3
import subprocess
import tempfile
import urllib.parse

from direct_lookup import free_port, start_server, stop_server
from paired_bot_admin import request
from paired_direct_lookup import login_campfire, seed_campfire, seed_rustfire, wait_for_server


DATA = b"paired direct upload\n"
CHECKSUM = base64.b64encode(hashlib.md5(DATA).digest()).decode()
PAYLOAD = json.dumps({"blob": {"filename": "paired-upload.txt", "byte_size": len(DATA),
                               "checksum": CHECKSUM, "content_type": "text/plain"}}).encode()


def raw_request(port, method, path, body=b"", headers=None):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    try:
        connection.request(method, path, body, headers or {})
        response = connection.getresponse()
        return response.status, response.getheader("Location"), response.read()
    finally:
        connection.close()


def path_from_url(url):
    parsed = urllib.parse.urlsplit(url)
    return parsed.path + (f"?{parsed.query}" if parsed.query else "")


def anonymous_login_context(port):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    try:
        connection.request("GET", "/session/new")
        response = connection.getresponse()
        page = response.read().decode()
        assert response.status == 200
        csrf = re.search(r"<meta name=['\"]csrf-token['\"] content=['\"]([^'\"]+)", page)
        assert csrf, "Anonymous login page lacks a CSRF token"
        cookies = "; ".join(value.split(";", 1)[0] for value in response.headers.get_all("Set-Cookie", []))
        return cookies, csrf.group(1)
    finally:
        connection.close()


def signed_metadata(value):
    encoded, _signature = value.split("--", 1)
    return json.loads(base64.b64decode(encoded))["_rails"]


def workflow(port, cookie, csrf, database, upload_root, campfire):
    anonymous = raw_request(port, "POST", "/rails/active_storage/direct_uploads", PAYLOAD,
                            {"Content-Type": "application/json"})
    assert anonymous[0] == 422, ("anonymous metadata without CSRF context", anonymous[0])
    guest_cookie, guest_csrf = anonymous_login_context(port)
    guest = raw_request(port, "POST", "/rails/active_storage/direct_uploads", PAYLOAD,
                        {"Cookie": guest_cookie, "X-CSRF-Token": guest_csrf, "Content-Type": "application/json"})
    assert guest[0] == 401, ("anonymous metadata with CSRF context", guest[0])
    status, _, response = request(port, "POST", "/rails/active_storage/direct_uploads",
                                  cookie, csrf, PAYLOAD, "application/json")
    assert status == 200, ("metadata", status, response[:300])
    metadata = json.loads(response)
    assert set(metadata) == {"id", "byte_size", "checksum", "content_type", "created_at",
                             "filename", "key", "metadata", "service_name", "attachable_sgid",
                             "signed_id", "direct_upload"}
    assert (metadata["byte_size"], metadata["checksum"], metadata["content_type"],
            metadata["filename"], metadata["metadata"], metadata["service_name"]) == (
                len(DATA), CHECKSUM, "text/plain", "paired-upload.txt", {}, "local")
    assert re.fullmatch(r"[0-9a-z]{28}", metadata["key"]), metadata["key"]
    assert signed_metadata(metadata["signed_id"])["pur"] == "blob_id"
    assert signed_metadata(metadata["attachable_sgid"])["pur"] == "attachable"
    upload = metadata["direct_upload"]
    assert upload["headers"] == {"Content-Type": "text/plain"}
    upload_path = path_from_url(upload["url"])
    token = upload_path.rsplit("/", 1)[-1]
    token_data = signed_metadata(token)
    assert token_data["pur"] == "blob_token"
    assert token_data["data"]["key"] == metadata["key"]
    assert token_data["data"]["content_length"] == len(DATA)
    assert token_data["data"]["checksum"] == CHECKSUM
    assert raw_request(port, "PUT", upload_path, DATA, {"Content-Type": "text/plain"})[0] == 401
    headers = {"Cookie": cookie, "Content-Type": "text/plain"}
    assert raw_request(port, "PUT", upload_path, b"incorrect", headers)[0] == 422
    assert raw_request(port, "PUT", upload_path, b"x" * len(DATA), headers)[0] == 422
    assert raw_request(port, "PUT", upload_path, DATA, headers)[0] == 204
    blob_path = f"/rails/active_storage/blobs/redirect/{metadata['signed_id']}/paired-upload.txt"
    status, location, _ = raw_request(port, "GET", blob_path)
    assert status == 302, ("blob redirect", status)
    status, _, downloaded = raw_request(port, "GET", path_from_url(location))
    assert (status, downloaded) == (200, DATA), ("disk download", status, downloaded[:100])
    with sqlite3.connect(database) as db:
        if campfire:
            stored = db.execute("SELECT key FROM active_storage_blobs WHERE id=?", [metadata["id"]]).fetchone()[0]
            path = upload_root / stored[:2] / stored[2:4] / stored
        else:
            stored, uploaded = db.execute("SELECT storage_key,uploaded FROM direct_upload_blobs WHERE id=?", [metadata["id"]]).fetchone()
            assert uploaded == 1
            path = upload_root / stored
    assert path.read_bytes() == DATA
    return path


def concurrent_metadata(port, cookie, csrf):
    def create(_):
        status, _, response = request(port, "POST", "/rails/active_storage/direct_uploads",
                                      cookie, csrf, PAYLOAD, "application/json")
        assert status == 200, ("concurrent metadata", status, response[:100])
        return json.loads(response)["id"]

    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as executor:
        ids = list(executor.map(create, range(16)))
    assert len(set(ids)) == 16, "Concurrent metadata allocations reused a blob ID"


def main():
    repo = pathlib.Path("/tmp/once-campfire-reference")
    ruby = pathlib.Path("/tmp/rustfire-baseline/local/bin/ruby")
    bundle = pathlib.Path("/tmp/rustfire-baseline/bundle")
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    assert revision == "91d294f4a09f9bbe37f9548959bfcb43645678fb", revision
    with tempfile.TemporaryDirectory(prefix="paired-direct-upload-") as scratch:
        temp = pathlib.Path(scratch)
        rust_db, camp_db = temp / "rust.sqlite3", temp / "camp.sqlite3"
        rust_port, camp_port = free_port(), free_port()
        seed_rustfire(rust_db, rust_port, [])
        camp_env = seed_campfire(repo, ruby, bundle, repo / "storage/db/production.sqlite3", camp_db, [], camp_port, temp)
        rust_uploads = temp / "rust-uploads"
        rust = start_server(rust_db, rust_port, {"RUSTFIRE_UPLOAD_DIR": str(rust_uploads)})
        try:
            rust_file = workflow(rust_port, "session_token=benchmark-session", "benchmark-csrf", rust_db, rust_uploads, False)
            concurrent_metadata(rust_port, "session_token=benchmark-session", "benchmark-csrf")
        finally:
            stop_server(rust)
        log = open(temp / "puma.log", "w+")
        camp = subprocess.Popen([str(ruby), str(ruby.parent / "bundle"), "exec", "puma", "-C", "config/puma.rb"],
                                cwd=repo, env=camp_env, stdout=log, stderr=log)
        camp_file = None
        try:
            wait_for_server(camp_port, camp)
            cookie, csrf = login_campfire(camp_port)
            camp_file = workflow(camp_port, cookie, csrf, camp_db, repo / "storage/files", True)
            concurrent_metadata(camp_port, cookie, csrf)
        except Exception:
            log.flush()
            log.seek(0)
            print(log.read()[-2000:])
            raise
        finally:
            stop_server(camp)
            log.close()
            if camp_file is not None:
                camp_file.unlink(missing_ok=True)
                for parent in (camp_file.parent, camp_file.parent.parent):
                    try:
                        parent.rmdir()
                    except OSError:
                        pass
        assert rust_file.read_bytes() == DATA
        print("PASS matched direct-upload metadata, concurrent blob IDs, authenticated writes, checksum rejection, and public downloads")


if __name__ == "__main__":
    main()
