"""Check authenticated Active Storage direct uploads against pinned Campfire.

Run after cargo build --release with the pinned Ruby bundle and Redis.
"""

import argparse
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
from paired_bot_admin import PNG, request
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


def read_response(port, path, headers=None):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    try:
        connection.request("GET", path, headers=headers or {})
        response = connection.getresponse()
        return response.status, {key.lower(): value for key, value in response.getheaders()}, response.read()
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


def mime_case(port, cookie, csrf, database, upload_root, campfire, filename, content_type, data):
    payload = json.dumps({"blob": {"filename": filename, "byte_size": len(data),
                                   "checksum": base64.b64encode(hashlib.md5(data).digest()).decode(),
                                   "content_type": content_type}}).encode()
    status, _, body = request(port, "POST", "/rails/active_storage/direct_uploads",
                              cookie, csrf, payload, "application/json")
    assert status == 200, (filename, "metadata", status, body[:200])
    metadata = json.loads(body)
    observations = [("metadata filename", metadata["filename"])]
    upload_path = path_from_url(metadata["direct_upload"]["url"])
    upload_status, _, _ = raw_request(port, "PUT", upload_path, data,
                                      {"Cookie": cookie, "Content-Type": content_type})
    assert upload_status == 204, (filename, "upload", upload_status)
    signed_name = urllib.parse.quote(metadata["filename"], safe="")
    proxy_path = f"/rails/active_storage/blobs/proxy/{metadata['signed_id']}/{signed_name}"
    for label, suffix in (("default", ""), ("inline", "?disposition=inline"),
                          ("attachment", "?disposition=attachment")):
        status, headers, body = read_response(port, proxy_path + suffix)
        assert (status, body) == (200, data), (filename, label, status, body[:50])
        observations.append((label, status, tuple((key, headers.get(key)) for key in (
            "content-type", "content-disposition", "cache-control", "last-modified",
            "referrer-policy", "x-content-type-options", "x-frame-options",
            "x-permitted-cross-domain-policies", "x-xss-protection", "content-security-policy"))))
    redirect_path = proxy_path.replace("/blobs/proxy/", "/blobs/redirect/", 1)
    redirect_status, redirect_location, _ = raw_request(port, "GET", redirect_path)
    assert redirect_status == 302, (filename, "redirect", redirect_status)
    disk_status, disk_headers, disk_body = read_response(port, path_from_url(redirect_location))
    assert (disk_status, disk_body) == (200, data), (filename, "disk", disk_status, disk_body[:50])
    observations.append(("disk", disk_status, tuple((key, disk_headers.get(key)) for key in (
        "content-type", "content-disposition", "x-content-type-options", "content-security-policy"))))
    with sqlite3.connect(database) as db:
        if campfire:
            stored, stored_filename = db.execute("SELECT key,filename FROM active_storage_blobs WHERE id=?", [metadata["id"]]).fetchone()
            path = upload_root / stored[:2] / stored[2:4] / stored
        else:
            stored, stored_filename = db.execute("SELECT storage_key,filename FROM direct_upload_blobs WHERE id=?", [metadata["id"]]).fetchone()
            path = upload_root / stored
    observations.append(("stored filename", stored_filename))
    assert path.read_bytes() == data, (filename, path)
    return path, observations


def missing_mime_case(port, cookie, csrf, database, upload_root, campfire):
    filename, data = "no-mime.bin", b"missing MIME payload\n"
    payload = json.dumps({"blob": {"filename": filename, "byte_size": len(data),
                                   "checksum": base64.b64encode(hashlib.md5(data).digest()).decode()}}).encode()
    status, _, body = request(port, "POST", "/rails/active_storage/direct_uploads",
                              cookie, csrf, payload, "application/json")
    assert status == 200, ("missing MIME metadata", status, body[:120])
    metadata = json.loads(body)
    assert metadata["content_type"] is None and metadata["direct_upload"]["headers"] == {"Content-Type": None}
    upload_status, _, _ = raw_request(port, "PUT", path_from_url(metadata["direct_upload"]["url"]),
                                      data, {"Cookie": cookie})
    assert upload_status == 204, ("missing MIME disk upload", upload_status)
    proxy_path = f"/rails/active_storage/blobs/proxy/{metadata['signed_id']}/{filename}"
    proxy_status, proxy_headers, proxy_body = read_response(port, proxy_path)
    assert (proxy_status, proxy_body) == (200, data), ("missing MIME proxy", proxy_status, proxy_body)
    redirect_status, redirect_location, _ = raw_request(port, "GET", proxy_path.replace("/blobs/proxy/", "/blobs/redirect/", 1))
    assert redirect_status == 302, ("missing MIME redirect", redirect_status)
    disk_status, disk_headers, disk_body = read_response(port, path_from_url(redirect_location))
    assert disk_status == 500, ("missing MIME disk error", disk_status)
    with sqlite3.connect(database) as db:
        if campfire:
            stored = db.execute("SELECT key FROM active_storage_blobs WHERE id=?", [metadata["id"]]).fetchone()[0]
            path = upload_root / stored[:2] / stored[2:4] / stored
        else:
            stored = db.execute("SELECT storage_key FROM direct_upload_blobs WHERE id=?", [metadata["id"]]).fetchone()[0]
            path = upload_root / stored
    assert path.read_bytes() == data
    observed = tuple((key, proxy_headers.get(key)) for key in
                     ("content-type", "content-disposition", "x-content-type-options"))
    return path, (observed, disk_headers.get("content-type"), disk_body)


def workflow(port, cookie, csrf, database, upload_root, campfire, large_data, large_mib):
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
    if not campfire:
        assert not (upload_root / metadata["key"]).exists(), "Rejected upload was published"
        assert not list(upload_root.glob(f"{metadata['key']}.upload-*")), "Rejected upload left temporary files"
    assert raw_request(port, "PUT", upload_path, DATA, headers)[0] == 204
    blob_path = f"/rails/active_storage/blobs/redirect/{metadata['signed_id']}/paired-upload.txt"
    status, location, _ = raw_request(port, "GET", blob_path)
    assert status == 302, ("blob redirect", status)
    status, _, downloaded = raw_request(port, "GET", path_from_url(location))
    assert (status, downloaded) == (200, DATA), ("disk download", status, downloaded[:100])
    renamed_disk_path = path_from_url(location).replace("paired-upload.txt", "another-name.txt")
    renamed_status, _, renamed_download = raw_request(port, "GET", renamed_disk_path)
    assert (renamed_status, renamed_download) == (200, DATA), ("disk filename alias", renamed_status)
    for redirect_path in (
        f"/rails/active_storage/blobs/{metadata['signed_id']}/paired-upload.txt",
        f"/rails/active_storage/blobs/redirect/{metadata['signed_id']}/another-name.txt",
    ):
        redirect_status, redirect_location, _ = raw_request(port, "GET", redirect_path)
        assert redirect_status == 302, ("blob redirect alias", redirect_path, redirect_status)
        final_status, _, final_body = raw_request(port, "GET", path_from_url(redirect_location))
        assert (final_status, final_body) == (200, DATA), ("blob redirect alias bytes", final_status)
    proxy_path = f"/rails/active_storage/blobs/proxy/{metadata['signed_id']}/paired-upload.txt"
    proxy_status, proxy_headers, proxy_body = read_response(port, proxy_path)
    assert (proxy_status, proxy_body) == (200, DATA), ("blob proxy", proxy_status, proxy_body[:100])
    assert proxy_headers.get("content-type", "").startswith("text/plain"), proxy_headers
    assert proxy_headers.get("content-disposition", "").startswith("attachment;"), proxy_headers
    range_status, range_headers, range_body = read_response(port, proxy_path, {"Range": "bytes=0-5"})
    assert (range_status, range_body) == (206, DATA[:6]), ("blob proxy range", range_status, range_body)
    assert range_headers.get("content-range") == f"bytes 0-5/{len(DATA)}", range_headers
    assert read_response(port, proxy_path.replace("paired-upload.txt", "another-name.txt"))[2] == DATA
    assert read_response(port, "/rails/active_storage/blobs/proxy/invalid/paired-upload.txt")[0] == 404
    assert proxy_headers.get("etag"), proxy_headers
    expected_etag = f'W/"{hashlib.sha256(proxy_path.encode()).hexdigest()[:32]}"'
    assert proxy_headers["etag"] == expected_etag, (proxy_headers["etag"], expected_etag)
    conditional_status, _, conditional_body = read_response(port, proxy_path, {"If-None-Match": proxy_headers["etag"]})
    assert (conditional_status, conditional_body) == (304, b""), (conditional_status, conditional_body)
    assert proxy_headers.get("last-modified"), proxy_headers
    modified_status, _, modified_body = read_response(port, proxy_path, {"If-Modified-Since": proxy_headers["last-modified"]})
    assert (modified_status, modified_body) == (304, b""), (modified_status, modified_body)
    with sqlite3.connect(database) as db:
        if campfire:
            stored = db.execute("SELECT key FROM active_storage_blobs WHERE id=?", [metadata["id"]]).fetchone()[0]
            path = upload_root / stored[:2] / stored[2:4] / stored
        else:
            stored, uploaded = db.execute("SELECT storage_key,uploaded FROM direct_upload_blobs WHERE id=?", [metadata["id"]]).fetchone()
            assert uploaded == 1
            path = upload_root / stored
    assert path.read_bytes() == DATA
    extra_files = []
    extra_results = []
    for filename, content_type, data in (
        ("sample.html", "text/html", b"<script>alert(1)</script>\n"),
        ("pixel.png", "image/png", PNG),
        ("vector.svg", "image/svg+xml", b"<svg xmlns='http://www.w3.org/2000/svg'></svg>\n"),
        ("café résumé.txt", "text/plain", "a Unicode filename\n".encode()),
        ("автомобиль.txt", "text/plain", b"a non-Latin filename\n"),
        ("argh+!#&^`~.txt", "text/plain", b"punctuation in filename\n"),
        ("  report:Q%$|?*.txt  ", "text/plain", b"an unsafe filename\n"),
        ("dir\\report/2026.txt", "text/plain", b"separators in filename\n"),
        ("bidirectional\u202eview.txt", "text/plain", b"direction override in filename\n"),
        (f"over-{large_mib}-mib.bin", "application/octet-stream", large_data),
    ):
        extra_path, observations = mime_case(port, cookie, csrf, database, upload_root, campfire,
                                              filename, content_type, data)
        extra_files.append(extra_path)
        extra_results.append((filename, observations))
    missing_path, missing_observations = missing_mime_case(port, cookie, csrf, database, upload_root, campfire)
    return [path, *extra_files, missing_path], (proxy_status, proxy_headers.get("content-type"), proxy_headers.get("content-disposition"),
                                  proxy_headers.get("cache-control"), bool(proxy_headers.get("etag")),
                                  range_status, range_headers.get("content-range"), extra_results, missing_observations)


def concurrent_metadata(port, cookie, csrf):
    def create(_):
        status, _, response = request(port, "POST", "/rails/active_storage/direct_uploads",
                                      cookie, csrf, PAYLOAD, "application/json")
        assert status == 200, ("concurrent metadata", status, response[:100])
        return json.loads(response)["id"]

    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as executor:
        ids = list(executor.map(create, range(16)))
    assert len(set(ids)) == 16, "Concurrent metadata allocations reused a blob ID"


def metadata_boundaries(port, cookie, csrf):
    observations = []
    for label, filename, content_type, byte_size in (
        ("long filename", "f" * 260 + ".txt", "text/plain", len(DATA)),
        ("long MIME type", "long-mime.txt", "application/x-" + "x" * 250, len(DATA)),
        ("negative size", "negative.txt", "text/plain", -1),
        ("empty filename", "", "text/plain", len(DATA)),
        ("empty MIME type", "empty-mime.txt", "", len(DATA)),
        ("missing MIME type", "missing-mime.txt", None, len(DATA)),
    ):
        blob = {"filename": filename, "byte_size": byte_size, "checksum": CHECKSUM}
        if content_type is not None:
            blob["content_type"] = content_type
        payload = json.dumps({"blob": blob}).encode()
        status, _, body = request(port, "POST", "/rails/active_storage/direct_uploads",
                                  cookie, csrf, payload, "application/json")
        metadata = json.loads(body) if status == 200 else None
        observations.append((label, status, None if metadata is None else (
            metadata["filename"], metadata["content_type"], metadata["byte_size"],
            metadata["direct_upload"]["headers"],
            signed_metadata(path_from_url(metadata["direct_upload"]["url"]).rsplit("/", 1)[-1])["data"].get("content_type"))))
    return observations


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--large-mib", type=int, default=25,
                        help="binary upload size in MiB, plus one byte (default: 25)")
    args = parser.parse_args()
    if args.large_mib < 1:
        parser.error("--large-mib must be positive")
    large_data = b"R" * (args.large_mib * 1024 * 1024 + 1)
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
            rust_files, rust_proxy = workflow(rust_port, "session_token=benchmark-session", "benchmark-csrf", rust_db, rust_uploads, False, large_data, args.large_mib)
            concurrent_metadata(rust_port, "session_token=benchmark-session", "benchmark-csrf")
            rust_boundaries = metadata_boundaries(rust_port, "session_token=benchmark-session", "benchmark-csrf")
        finally:
            stop_server(rust)
        log = open(temp / "puma.log", "w+")
        camp = subprocess.Popen([str(ruby), str(ruby.parent / "bundle"), "exec", "puma", "-C", "config/puma.rb"],
                                cwd=repo, env=camp_env, stdout=log, stderr=log)
        camp_files = []
        try:
            wait_for_server(camp_port, camp)
            cookie, csrf = login_campfire(camp_port)
            camp_files, camp_proxy = workflow(camp_port, cookie, csrf, camp_db, repo / "storage/files", True, large_data, args.large_mib)
            concurrent_metadata(camp_port, cookie, csrf)
            camp_boundaries = metadata_boundaries(camp_port, cookie, csrf)
        except Exception:
            log.flush()
            log.seek(0)
            print(log.read()[-2000:])
            raise
        finally:
            stop_server(camp)
            log.close()
            for camp_file in camp_files:
                camp_file.unlink(missing_ok=True)
                for parent in (camp_file.parent, camp_file.parent.parent):
                    try:
                        parent.rmdir()
                    except OSError:
                        pass
        assert rust_files[0].read_bytes() == DATA
        assert rust_proxy[:-2] == camp_proxy[:-2], (rust_proxy[:-2], camp_proxy[:-2])
        for (rust_name, rust_observations), (camp_name, camp_observations) in zip(rust_proxy[-2], camp_proxy[-2], strict=True):
            assert rust_name == camp_name
            for rust_observation, camp_observation in zip(rust_observations, camp_observations, strict=True):
                assert rust_observation == camp_observation, (rust_name, rust_observation, camp_observation)
        assert rust_proxy[-1] == camp_proxy[-1], ("missing MIME download", rust_proxy[-1], camp_proxy[-1])
        assert rust_boundaries == camp_boundaries, (rust_boundaries, camp_boundaries)
        print(f"PASS matched direct-upload metadata boundaries, {args.large_mib} MiB plus one byte upload, concurrent blob IDs, authenticated writes, checksum rejection, redirect and proxy downloads, and missing-MIME disk error body")


if __name__ == "__main__":
    main()
