"""Compare device-transfer expiry, tampering, replay, and inactive-user behavior."""

import hashlib
import pathlib
import re
import sqlite3
import subprocess
import tempfile
import urllib.error
import urllib.parse
import urllib.request

from direct_lookup import free_port, start_server, stop_server
from paired_banned_content import BUNDLE, REPOSITORY, REVISION, RUBY, isolated_campfire, start_redis
from paired_direct_lookup import login_campfire, seed_campfire, seed_rustfire, wait_for_server
from paired_join import browser
from paired_profile import transfer_path
from paired_session import token as csrf_token


def request(opener, port, path, method="GET", csrf=None, accept="text/html"):
    headers = {"Accept": accept}
    body = None
    if method != "GET":
        body = urllib.parse.urlencode({"authenticity_token": csrf}).encode()
        headers.update({"Content-Type": "application/x-www-form-urlencoded", "X-CSRF-Token": csrf})
    call = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=body, headers=headers, method=method)
    try:
        response = opener.open(call)
    except urllib.error.HTTPError as error:
        response = error
    with response:
        body = response.read()
        location = urllib.parse.urlsplit(response.headers.get("Location", "")).path or None
        media = (response.headers.get("Content-Type") or "").split(";", 1)[0]
        return response.status, media, location, body


def transfer(port, path, accept="text/html"):
    opener = browser()
    status, _, _, page = request(opener, port, path)
    assert status == 200 and b"auto-submit" in page, (status, page[:200])
    csrf = csrf_token(page.decode())
    status, media, location, body = request(opener, port, path, "PUT", csrf, accept)
    return status, media, location, (len(body), hashlib.sha256(body).hexdigest())


def sessions(database):
    with sqlite3.connect(database) as db:
        return db.execute("SELECT COUNT(*) FROM sessions WHERE user_id=1").fetchone()[0]


def set_status(database, status):
    with sqlite3.connect(database) as db:
        db.execute("UPDATE users SET status=? WHERE id=1", [status])


def main():
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPOSITORY, text=True).strip() == REVISION
    with tempfile.TemporaryDirectory(prefix="paired-transfer-edges-") as scratch:
        temp = pathlib.Path(scratch)
        rust_db, camp_db = temp / "rust.sqlite3", temp / "camp.sqlite3"
        rust_port, camp_port, redis_port = free_port(), free_port(), free_port()
        seed_rustfire(rust_db, rust_port, [])
        camp_env = seed_campfire(REPOSITORY, RUBY, BUNDLE, REPOSITORY / "storage/db/production.sqlite3", camp_db, [], camp_port, temp)
        checkout = isolated_campfire(temp, redis_port)
        camp_env["REDIS_URL"] = f"redis://127.0.0.1:{redis_port}"
        redis, redis_log = start_redis(temp, redis_port)
        try:
            rust = start_server(rust_db, rust_port, {"RUSTFIRE_CAMPFIRE_SECRET_KEY_BASE": camp_env["SECRET_KEY_BASE"]})
            with open(temp / "puma.log", "w+") as log:
                camp = subprocess.Popen([str(RUBY), str(RUBY.parent / "bundle"), "exec", "puma", "-C", "config/puma.rb"],
                                        cwd=checkout, env=camp_env, stdout=log, stderr=log)
                try:
                    wait_for_server(camp_port, camp)
                    cookie, _ = login_campfire(camp_port)
                    headers = {"Accept": "text/html", "Cookie": cookie}
                    with urllib.request.urlopen(urllib.request.Request(f"http://127.0.0.1:{camp_port}/users/me/profile", headers=headers)) as response:
                        assert response.status == 200
                        profile = response.read()
                    valid = transfer_path(profile)
                    expired = subprocess.run(
                        [str(RUBY), str(RUBY.parent / "bundle"), "exec", "rails", "runner",
                         'puts User.find(1).signed_id(purpose: :transfer, expires_at: 1.day.ago)'],
                        cwd=checkout, env=camp_env, text=True, capture_output=True, check=True,
                    ).stdout.strip()
                    assert re.fullmatch(r"[A-Za-z0-9_-]+--[0-9a-f]{64}", expired), expired
                    altered = valid[:-1] + ("0" if valid[-1] != "0" else "1")
                    cases = (("malformed", "/session/transfers/invalid", 0, "text/html"),
                             ("malformed json", "/session/transfers/invalid", 0, "application/json"),
                             ("malformed turbo", "/session/transfers/invalid", 0, "text/vnd.turbo-stream.html"),
                             ("malformed wildcard", "/session/transfers/invalid", 0, "*/*"),
                             ("altered", altered, 0, "text/html"),
                             ("expired", "/session/transfers/" + expired, 0, "text/html"),
                             ("valid", valid, 1, "text/html"),
                             ("replay", valid, 1, "text/html"),
                             ("banned", valid, 0, "text/html"),
                             ("deactivated", valid, 0, "text/html"))
                    results = []
                    for label, path, expected_sessions, accept in cases:
                        if label == "banned":
                            set_status(rust_db, 2)
                            set_status(camp_db, 2)
                        elif label == "deactivated":
                            set_status(rust_db, 1)
                            set_status(camp_db, 1)
                        before_rust, before_camp = sessions(rust_db), sessions(camp_db)
                        rust_result = transfer(rust_port, path, accept)
                        camp_result = transfer(camp_port, path, accept)
                        changes = sessions(rust_db) - before_rust, sessions(camp_db) - before_camp
                        results.append((label, rust_result, camp_result, changes, expected_sessions))
                except Exception:
                    log.flush()
                    log.seek(0)
                    print(log.read()[-2500:])
                    raise
                finally:
                    stop_server(camp)
                    stop_server(rust)
        finally:
            redis.terminate()
            redis.wait(timeout=10)
            redis_log.close()
    mismatches = [(label, rust, camp, changes) for label, rust, camp, changes, expected in results
                  if rust != camp or changes != (expected, expected)]
    for label, rust, camp, changes in mismatches:
        print(f"{label}: Rustfire={rust} Campfire={camp} sessions={changes}")
    assert not mismatches, f"{len(mismatches)} transfer cases differ"
    print(f"PASS {len(cases)} paired transfer edge cases match Campfire")


if __name__ == "__main__":
    main()
