"""Compare dynamic-route security response headers with pinned Campfire."""

import http.client
import pathlib
import sqlite3
import subprocess
import tempfile

from direct_lookup import free_port, start_server, stop_server
from paired_banned_content import BUNDLE, REPOSITORY, REVISION, RUBY, isolated_campfire, start_redis
from paired_direct_lookup import login_campfire, seed_campfire, seed_rustfire, wait_for_server


HEADERS = (
    "content-security-policy",
    "cross-origin-opener-policy",
    "cross-origin-resource-policy",
    "permissions-policy",
    "referrer-policy",
    "strict-transport-security",
    "x-content-type-options",
    "x-frame-options",
    "x-permitted-cross-domain-policies",
    "x-xss-protection",
)
CASES = (
    ("anonymous sign-in", "/session/new", "anonymous", None),
    ("authenticated room", "/rooms/1", "admin", None),
    ("authenticated profile", "/users/me/profile", "admin", None),
    ("anonymous room redirect", "/rooms/1", "anonymous", None),
    ("member forbidden bot page", "/account/bots/new", "member", None),
    ("missing bot edit", "/account/bots/999/edit", "admin", None),
    ("autocomplete", "/autocompletable/users?query=Test", "admin", "application/json"),
    ("room messages", "/rooms/1/messages", "admin", "text/html"),
    ("manifest format error", "/webmanifest", "anonymous", None),
    ("manifest JSON", "/webmanifest", "anonymous", "application/json"),
    ("service worker format error", "/service-worker", "anonymous", None),
    ("service worker JavaScript", "/service-worker", "anonymous", "text/javascript"),
    ("health", "/up", "anonymous", None),
    ("static asset", "/assets/arrow-left-abe40556.svg", "anonymous", None),
    ("missing route", "/not-a-real-route", "anonymous", None),
)


def request(port, path, cookie, accept):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        headers = {"Cookie": cookie} if cookie else {}
        if accept:
            headers["Accept"] = accept
        connection.request("GET", path, headers=headers)
        response = connection.getresponse()
        response.read()
        return response.status, {name: response.getheader(name) for name in HEADERS}
    finally:
        connection.close()


def main():
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPOSITORY, text=True).strip() == REVISION
    with tempfile.TemporaryDirectory(prefix="paired-security-headers-") as scratch:
        temp = pathlib.Path(scratch)
        rust_db, camp_db = temp / "rust.sqlite3", temp / "camp.sqlite3"
        rust_port, camp_port, redis_port = free_port(), free_port(), free_port()
        seed_rustfire(rust_db, rust_port, [])
        camp_env = seed_campfire(REPOSITORY, RUBY, BUNDLE, REPOSITORY / "storage/db/production.sqlite3", camp_db, [], camp_port, temp)
        checkout = isolated_campfire(temp, redis_port)
        camp_env["REDIS_URL"] = f"redis://127.0.0.1:{redis_port}"
        with sqlite3.connect(rust_db) as rust:
            rust.execute("INSERT INTO sessions(user_id,token,csrf_token,created_at,last_active_at) VALUES(2,'benchmark-member-session','benchmark-member-csrf','2026-01-01T00:00:00Z','2026-01-01T00:00:00Z')")
        with sqlite3.connect(camp_db) as camp:
            camp.execute("UPDATE users SET email_address='member@example.invalid',password_digest=(SELECT password_digest FROM users WHERE id=1) WHERE id=2")
        redis, redis_log = start_redis(temp, redis_port)
        try:
            rust = start_server(rust_db, rust_port)
            try:
                rust_cookies = {"anonymous": "", "admin": "session_token=benchmark-session", "member": "session_token=benchmark-member-session"}
                rust_result = {label: request(rust_port, path, rust_cookies[role], accept) for label, path, role, accept in CASES}
            finally:
                stop_server(rust)
            with open(temp / "puma.log", "w+") as puma_log:
                camp = subprocess.Popen([str(RUBY), str(RUBY.parent / "bundle"), "exec", "puma", "-C", "config/puma.rb"], cwd=checkout, env=camp_env, stdout=puma_log, stderr=puma_log)
                try:
                    wait_for_server(camp_port, camp)
                    cookie, _ = login_campfire(camp_port)
                    member_cookie, _ = login_campfire(camp_port, email="member@example.invalid")
                    camp_cookies = {"anonymous": "", "admin": cookie, "member": member_cookie}
                    camp_result = {label: request(camp_port, path, camp_cookies[role], accept) for label, path, role, accept in CASES}
                finally:
                    stop_server(camp)
        finally:
            redis.terminate()
            redis.wait(timeout=10)
            redis_log.close()
    mismatches = {}
    for label, _, _, _ in CASES:
        rust_status, rust_headers = rust_result[label]
        camp_status, camp_headers = camp_result[label]
        differences = {name: (rust_headers[name], camp_headers[name]) for name in HEADERS if rust_headers[name] != camp_headers[name]}
        if rust_status != camp_status or differences:
            mismatches[label] = {"status": (rust_status, camp_status), "headers": differences}
    if mismatches:
        for label, details in mismatches.items():
            print(f"{label}: {details}")
        raise AssertionError(f"{len(mismatches)} security-header cases differ")
    print(f"PASS {len(CASES)} dynamic-route security-header cases match Campfire")


if __name__ == "__main__":
    main()
