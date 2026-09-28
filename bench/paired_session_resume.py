"""Compare authenticated session activity refresh with pinned Campfire.

Run after ``cargo build --release --locked`` with the pinned Ruby bundle.
The probe uses disposable databases and never changes a real account.
"""

from datetime import datetime, timedelta, timezone
import http.cookiejar
import json
import pathlib
import re
import sqlite3
import subprocess
import tempfile
import urllib.parse
import urllib.request

from direct_lookup import free_port, start_server, stop_server
from paired_bot_admin import request
from paired_direct_upload import PAYLOAD, path_from_url, raw_request
from paired_direct_lookup import seed_campfire, seed_rustfire, wait_for_server
from paired_profile import transfer_path


REPOSITORY = pathlib.Path("/tmp/once-campfire-reference")
RUBY = pathlib.Path("/tmp/rustfire-baseline/local/bin/ruby")
BUNDLE = pathlib.Path("/tmp/rustfire-baseline/bundle")
REVISION = "91d294f4a09f9bbe37f9548959bfcb43645678fb"
OLD_IP = "203.0.113.7"
NEW_IP = "8.8.8.8"
OLD_AGENT = "Before refresh"
NEW_AGENT = "Paired session resume"
LOGIN_AGENT = "Paired session creation"
LOGIN_IP = "8.8.4.4"


def metadata(database, session_id):
    with sqlite3.connect(database) as db:
        return db.execute(
            "SELECT user_agent,ip_address,last_active_at,updated_at FROM sessions WHERE id=?",
            (session_id,),
        ).fetchone()


def timestamp(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def sign_in(port, database):
    with sqlite3.connect(database) as db:
        prior = db.execute("SELECT COALESCE(MAX(id),0) FROM sessions WHERE user_id=1").fetchone()[0]
    cookies = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cookies))
    origin = f"http://127.0.0.1:{port}"
    with opener.open(origin + "/session/new") as response:
        body = response.read()
        assert response.status == 200, response.status
    csrf = re.search(rb'<meta name=[\'\"]csrf-token[\'\"] content=[\'\"]([^\'\"]+)', body)
    assert csrf, "sign-in CSRF token missing"
    fields = urllib.parse.urlencode({
        "email_address": "benchmark@example.invalid", "password": "benchmark-password",
        "authenticity_token": csrf.group(1).decode(),
    }).encode()
    request = urllib.request.Request(origin + "/session", data=fields, headers={
        "Content-Type": "application/x-www-form-urlencoded", "User-Agent": LOGIN_AGENT,
        "X-Forwarded-For": LOGIN_IP, "X-CSRF-Token": csrf.group(1).decode(),
    })
    before = datetime.now(timezone.utc)
    with opener.open(request) as response:
        response.read()
        assert response.status == 200 and urllib.parse.urlsplit(response.url).path in ("/", "/rooms/1"), (response.status, response.url)
    after = datetime.now(timezone.utc)
    with sqlite3.connect(database) as db:
        rows = db.execute(
            "SELECT id,user_agent,ip_address,created_at,updated_at,last_active_at FROM sessions WHERE user_id=1 AND id>?",
            (prior,),
        ).fetchall()
    assert len(rows) == 1, rows
    session_id, agent, ip, *times = rows[0]
    assert (agent, ip) == (LOGIN_AGENT, LOGIN_IP), rows[0]
    for value in times:
        assert before - timedelta(seconds=2) <= timestamp(value) <= after + timedelta(seconds=2), rows[0]
    cookie = "; ".join(f"{item.name}={item.value}" for item in cookies)
    return session_id, cookie


def set_metadata(database, session_id, age):
    activity = (datetime.now(timezone.utc) - age).isoformat()
    updated = "2025-01-01T00:00:00Z"
    with sqlite3.connect(database) as db:
        db.execute(
            "UPDATE sessions SET user_agent=?,ip_address=?,last_active_at=?,updated_at=? WHERE id=?",
            (OLD_AGENT, OLD_IP, activity, updated, session_id),
        )
    return OLD_AGENT, OLD_IP, activity, updated


def page(port, cookie):
    status, _, body = request(
        port, "GET", "/account/edit", cookie, "", extra_headers={
            "User-Agent": NEW_AGENT,
            "X-Forwarded-For": NEW_IP,
            "X-Rustfire-Peer-Ip": "1.1.1.1",  # Untrusted client input must not win.
        },
    )
    assert status == 200, (status, body[:300])


def exercise(port, database, session_id, cookie):
    original = set_metadata(database, session_id, timedelta(minutes=5))
    page(port, cookie)
    assert metadata(database, session_id) == original, ("fresh", metadata(database, session_id))

    original = set_metadata(database, session_id, timedelta(hours=2))
    before = datetime.now(timezone.utc)
    page(port, cookie)
    after = datetime.now(timezone.utc)
    refreshed = metadata(database, session_id)
    assert refreshed[:2] == (NEW_AGENT, NEW_IP), refreshed
    assert before - timedelta(seconds=2) <= timestamp(refreshed[2]) <= after + timedelta(seconds=2), refreshed
    assert before - timedelta(seconds=2) <= timestamp(refreshed[3]) <= after + timedelta(seconds=2), refreshed
    assert refreshed[2] != original[2] and refreshed[3] != original[3]

    page(port, cookie)
    assert metadata(database, session_id) == refreshed, ("second request", metadata(database, session_id))
    return "fresh unchanged; stale agent, IP, and times refreshed; immediate repeat unchanged"


def active_storage_without_refresh(port, database, session_id, cookie):
    status, _, page = request(port, "GET", "/account/edit", cookie, "")
    assert status == 200, status
    csrf = re.search(rb'<meta name=[\'\"]csrf-token[\'\"] content=[\'\"]([^\'\"]+)', page)
    assert csrf, "signed-in CSRF token missing"
    original = set_metadata(database, session_id, timedelta(hours=2))
    status, _, body = request(
        port, "POST", "/rails/active_storage/direct_uploads", cookie, csrf.group(1).decode(),
        PAYLOAD, "application/json", {"User-Agent": NEW_AGENT, "X-Forwarded-For": NEW_IP},
    )
    assert status == 200, (status, body[:200])
    assert metadata(database, session_id) == original, ("direct upload metadata", metadata(database, session_id))
    upload_path = path_from_url(json.loads(body)["direct_upload"]["url"])
    status, _, _ = raw_request(port, "PUT", upload_path, b"incorrect", {
        "Cookie": cookie, "Content-Type": "text/plain", "User-Agent": NEW_AGENT,
        "X-Forwarded-For": NEW_IP,
    })
    assert status == 422, status
    assert metadata(database, session_id) == original, ("direct upload bytes", metadata(database, session_id))
    return "direct upload POST and PUT authenticate without refreshing stale session"


def rejected_csrf_activity(port, database, session_id, cookie):
    original = set_metadata(database, session_id, timedelta(hours=2))
    before = datetime.now(timezone.utc)
    status, _, _ = request(
        port, "POST", "/rooms/1/messages", cookie, "incorrect-csrf",
        b"message%5Bbody%5D=Rejected", "application/x-www-form-urlencoded",
        {"User-Agent": NEW_AGENT, "X-Forwarded-For": NEW_IP},
    )
    after = datetime.now(timezone.utc)
    assert status == 422, status
    saved = metadata(database, session_id)
    if saved != original:
        assert saved[:2] == (NEW_AGENT, NEW_IP), saved
        assert before - timedelta(seconds=2) <= timestamp(saved[2]) <= after + timedelta(seconds=2), saved
        assert before - timedelta(seconds=2) <= timestamp(saved[3]) <= after + timedelta(seconds=2), saved
    return saved != original


def signed_in_relogin(port, database, session_id, cookie):
    status, _, page = request(port, "GET", "/account/edit", cookie, "")
    assert status == 200, status
    csrf = re.search(rb'<meta name=[\'\"]csrf-token[\'\"] content=[\'\"]([^\'\"]+)', page)
    assert csrf, "signed-in CSRF token missing"
    original = set_metadata(database, session_id, timedelta(hours=2))
    with sqlite3.connect(database) as db:
        before_count = db.execute("SELECT COUNT(*) FROM sessions WHERE user_id=1").fetchone()[0]
    fields = urllib.parse.urlencode({
        "email_address": "benchmark@example.invalid", "password": "benchmark-password",
        "authenticity_token": csrf.group(1).decode(),
    }).encode()
    status, location, _ = request(
        port, "POST", "/session", cookie, csrf.group(1).decode(), fields,
        "application/x-www-form-urlencoded", {"User-Agent": NEW_AGENT, "X-Forwarded-For": NEW_IP},
    )
    assert status == 302, (status, location)
    with sqlite3.connect(database) as db:
        after_count = db.execute("SELECT COUNT(*) FROM sessions WHERE user_id=1").fetchone()[0]
    assert after_count == before_count + 1, (before_count, after_count)
    return metadata(database, session_id) != original


def public_route_activity(port, database, session_id, cookie):
    status, _, page = request(port, "GET", "/account/edit", cookie, "")
    assert status == 200, status
    csrf = re.search(rb'<meta name=[\'\"]csrf-token[\'\"] content=[\'\"]([^\'\"]+)', page)
    assert csrf, "signed-in CSRF token missing"
    profile_status, _, profile = request(port, "GET", "/users/me/profile", cookie, "")
    assert profile_status == 200, profile_status
    valid_transfer = transfer_path(profile)
    with sqlite3.connect(database) as db:
        join_code = db.execute("SELECT join_code FROM accounts WHERE id=1").fetchone()[0]
    cases = (
        ("repeat setup", "POST", "/first_run", csrf.group(1).decode(), b"", {}),
        ("bad transfer", "POST", "/session/transfers/invalid", csrf.group(1).decode(), b"", {}),
        ("put override transfer", "POST", "/session/transfers/invalid", csrf.group(1).decode(), b"_method=put", {}),
        ("patch override transfer", "POST", "/session/transfers/invalid", csrf.group(1).decode(), b"_method=patch", {}),
        ("header override transfer", "POST", "/session/transfers/invalid", csrf.group(1).decode(), b"", {"X-HTTP-Method-Override": "PUT"}),
        ("duplicate override invalid first", "POST", "/session/transfers/invalid", csrf.group(1).decode(), b"_method=delete&_method=put", {}),
        ("duplicate override valid first", "POST", "/session/transfers/invalid", csrf.group(1).decode(), b"_method=put&_method=delete", {}),
        ("valid transfer override", "POST", valid_transfer, csrf.group(1).decode(), b"_method=put", {}),
        ("bad login CSRF", "POST", "/session", "incorrect-csrf",
         b"email_address=benchmark%40example.invalid&password=benchmark-password", {}),
        ("signed-in join", "GET", f"/join/{join_code}", "", b"", {}),
    )
    observations = {}
    for name, method, path, token, body, additional_headers in cases:
        original = set_metadata(database, session_id, timedelta(hours=2))
        with sqlite3.connect(database) as db:
            before_count = db.execute("SELECT COUNT(*) FROM sessions WHERE user_id=1").fetchone()[0]
        status, location, _ = request(
            port, method, path, cookie, token, body,
            "application/x-www-form-urlencoded" if method == "POST" else None,
            {"User-Agent": NEW_AGENT, "X-Forwarded-For": NEW_IP, **additional_headers},
        )
        with sqlite3.connect(database) as db:
            after_count = db.execute("SELECT COUNT(*) FROM sessions WHERE user_id=1").fetchone()[0]
        observations[name] = status, urllib.parse.urlsplit(location).path if location else None, metadata(database, session_id) != original, after_count - before_count
    return observations


def main():
    revision = subprocess.check_output(("git", "rev-parse", "HEAD"), cwd=REPOSITORY, text=True).strip()
    assert revision == REVISION, revision
    with tempfile.TemporaryDirectory(prefix="paired-session-resume-") as directory:
        temp = pathlib.Path(directory)
        rust_db, camp_db = temp / "rustfire.sqlite3", temp / "campfire.sqlite3"
        rust_port, camp_port = free_port(), free_port()
        seed_rustfire(rust_db, rust_port, [])
        env = seed_campfire(REPOSITORY, RUBY, BUNDLE, REPOSITORY / "storage/db/production.sqlite3",
                            camp_db, [], camp_port, temp)
        with sqlite3.connect(camp_db) as db:
            digest = db.execute("SELECT password_digest FROM users WHERE id=1").fetchone()[0]
        with sqlite3.connect(rust_db) as db:
            db.execute("UPDATE users SET email_address='benchmark@example.invalid',password_digest=? WHERE id=1", (digest,))
        rust_process = start_server(rust_db, rust_port, {"RUSTFIRE_TRUSTED_PROXY_IPS": "127.0.0.1"})
        try:
            rust_session, rust_cookie = sign_in(rust_port, rust_db)
            rust_result = exercise(rust_port, rust_db, rust_session, rust_cookie)
            rust_storage = active_storage_without_refresh(rust_port, rust_db, rust_session, rust_cookie)
            rust_rejected = rejected_csrf_activity(rust_port, rust_db, rust_session, rust_cookie)
            rust_relogin = signed_in_relogin(rust_port, rust_db, rust_session, rust_cookie)
            rust_public = public_route_activity(rust_port, rust_db, rust_session, rust_cookie)
        finally:
            stop_server(rust_process)
        with (temp / "puma.log").open("w+") as log:
            camp_process = subprocess.Popen(
                (str(RUBY), str(RUBY.parent / "bundle"), "exec", "puma", "-C", "config/puma.rb"),
                cwd=REPOSITORY, env=env, stdout=log, stderr=log,
            )
            try:
                wait_for_server(camp_port, camp_process)
                camp_session, cookie = sign_in(camp_port, camp_db)
                camp_result = exercise(camp_port, camp_db, camp_session, cookie)
                camp_storage = active_storage_without_refresh(camp_port, camp_db, camp_session, cookie)
                camp_rejected = rejected_csrf_activity(camp_port, camp_db, camp_session, cookie)
                camp_relogin = signed_in_relogin(camp_port, camp_db, camp_session, cookie)
                camp_public = public_route_activity(camp_port, camp_db, camp_session, cookie)
            except Exception:
                log.flush()
                log.seek(0)
                print(log.read()[-3000:])
                raise
            finally:
                stop_server(camp_process)
    assert rust_result == camp_result and rust_storage == camp_storage and rust_rejected == camp_rejected and rust_relogin == camp_relogin and rust_public == camp_public, (rust_rejected, camp_rejected, rust_relogin, camp_relogin, rust_public, camp_public)
    print("PASS paired session creation and resume:", rust_result, ";", rust_storage,
          "; rejected CSRF refreshed session:", rust_rejected,
          "; signed-in relogin refreshed old session:", rust_relogin,
          "; public-route activity:", rust_public)


if __name__ == "__main__":
    main()
