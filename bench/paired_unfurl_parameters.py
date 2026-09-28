"""Compare Open Graph preview request parameters without fetching an external URL."""

import hashlib
import http.client
import pathlib
import subprocess
import tempfile

from direct_lookup import free_port, start_server, stop_server
from paired_direct_lookup import login_campfire, seed_campfire, seed_rustfire, wait_for_server


SOURCE = pathlib.Path("/tmp/once-campfire-reference")
RUBY = pathlib.Path("/tmp/rustfire-baseline/local/bin/ruby")
BUNDLE = pathlib.Path("/tmp/rustfire-baseline/bundle")
REVISION = "91d294f4a09f9bbe37f9548959bfcb43645678fb"

# None of these values is a fetchable public URL. Successful parameter parsing
# therefore reaches Campfire's ordinary 204 no-preview response.
CASES = (
    ("missing", "", b"", "application/x-www-form-urlencoded", "text/html"),
    ("query only", "url=not-a-url", b"", "application/x-www-form-urlencoded", "text/html"),
    ("query blank overrides body", "url=", b"url=not-a-url", "application/x-www-form-urlencoded", "text/html"),
    ("query array overrides body", "url%5B%5D=not-a-url", b"url=not-a-url", "application/x-www-form-urlencoded", "text/html"),
    ("query scalar then array", "url=not-a-url&url%5B%5D=x", b"", "application/x-www-form-urlencoded", "text/html"),
    ("query array then scalar", "url%5B%5D=x&url=not-a-url", b"", "application/x-www-form-urlencoded", "text/html"),
    ("query duplicate last blank", "url=not-a-url&url=", b"", "application/x-www-form-urlencoded", "text/html"),
    ("body array", "", b"url%5B%5D=not-a-url", "application/x-www-form-urlencoded", "text/html"),
    ("body scalar then array", "", b"url=not-a-url&url%5B%5D=x", "application/x-www-form-urlencoded", "text/html"),
    ("body duplicate last blank", "", b"url=not-a-url&url=", "application/x-www-form-urlencoded", "text/html"),
    ("JSON scalar", "", b'{"url":"not-a-url"}', "application/json", "application/json"),
    ("JSON blank", "", b'{"url":""}', "application/json", "application/json"),
    ("JSON array", "", b'{"url":["not-a-url"]}', "application/json", "application/json"),
    ("JSON object", "", b'{"url":{"value":"not-a-url"}}', "application/json", "application/json"),
    ("JSON query only", "url=not-a-url", b"{}", "application/json", "application/json"),
    ("JSON query array", "url%5B%5D=not-a-url", b'{"url":"not-a-url"}', "application/json", "application/json"),
    ("JSON null", "", b'{"url":null}', "application/json", "application/json"),
    ("plain body", "", b"url=not-a-url", "text/plain", "text/html"),
    ("query only with JSON Accept", "url=not-a-url", b"", "application/x-www-form-urlencoded", "application/json"),
    ("query collision with JSON Accept", "url=not-a-url&url%5B%5D=x", b"", "application/x-www-form-urlencoded", "application/json"),
    ("query hash", "url%5Bvalue%5D=not-a-url", b"", "application/x-www-form-urlencoded", "text/html"),
    ("query empty array", "url%5B%5D=", b"", "application/x-www-form-urlencoded", "text/html"),
    ("body empty array", "", b"url%5B%5D=", "application/x-www-form-urlencoded", "text/html"),
    ("JSON empty array", "", b'{"url":[]}', "application/json", "application/json"),
    ("JSON empty object", "", b'{"url":{}}', "application/json", "application/json"),
    ("JSON number", "", b'{"url":42}', "application/json", "application/json"),
    ("JSON true", "", b'{"url":true}', "application/json", "application/json"),
    ("JSON false", "", b'{"url":false}', "application/json", "application/json"),
    ("invalid JSON with query", "url=not-a-url", b'{oops', "application/json", "application/json"),
    ("body collision with query", "url=not-a-url", b"url=not-a-url&url%5B%5D=x", "application/x-www-form-urlencoded", "text/html"),
    ("plain body with query", "url=not-a-url", b"url=ignored", "text/plain", "text/html"),
)


def request(port, cookie, csrf, case):
    _, query, body, content_type, accept = case
    path = "/unfurl_link" + ("?" + query if query else "")
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
    try:
        connection.request("POST", path, body=body, headers={
            "Cookie": cookie,
            "X-CSRF-Token": csrf,
            "Content-Type": content_type,
            "Accept": accept,
        })
        response = connection.getresponse()
        payload = response.read()
        return (response.status, response.getheader("Content-Type", ""),
                len(payload), hashlib.sha256(payload).hexdigest())
    finally:
        connection.close()


def main():
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=SOURCE, text=True).strip() == REVISION
    with tempfile.TemporaryDirectory(prefix="paired-unfurl-parameters-") as scratch:
        temp = pathlib.Path(scratch)
        rust_db, camp_db = temp / "rust.sqlite3", temp / "camp.sqlite3"
        rust_port, camp_port = free_port(), free_port()
        seed_rustfire(rust_db, rust_port, [])
        camp_env = seed_campfire(SOURCE, RUBY, BUNDLE, SOURCE / "storage/db/production.sqlite3", camp_db, [], camp_port, temp)
        camp_env["WEB_CONCURRENCY"] = "1"
        rust = start_server(rust_db, rust_port, {})
        try:
            rust_results = [request(rust_port, "session_token=benchmark-session", "benchmark-csrf", case) for case in CASES]
        finally:
            stop_server(rust)
        with open(temp / "puma.log", "w+") as log:
            camp = subprocess.Popen([str(RUBY), str(RUBY.parent / "bundle"), "exec", "puma", "-C", "config/puma.rb"],
                                    cwd=SOURCE, env=camp_env, stdout=log, stderr=log)
            try:
                wait_for_server(camp_port, camp)
                cookie, csrf = login_campfire(camp_port)
                camp_results = [request(camp_port, cookie, csrf, case) for case in CASES]
            except Exception:
                log.flush()
                log.seek(0)
                print(log.read()[-3000:])
                raise
            finally:
                stop_server(camp)
    differences = [(case[0], rust, camp) for case, rust, camp in zip(CASES, rust_results, camp_results) if rust != camp]
    for label, rust, camp in differences:
        print(f"{label}: Rustfire={rust} Campfire={camp}")
    assert not differences, f"{len(differences)} Open Graph parameter cases differ"
    print(f"PASS {len(CASES)} paired Open Graph parameter cases")


if __name__ == "__main__":
    main()
