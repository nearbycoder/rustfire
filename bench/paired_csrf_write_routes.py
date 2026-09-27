"""Compare CSRF, routing, and missing-record precedence on write requests."""

import hashlib
import http.client
import pathlib
import re
import subprocess
import tempfile
import urllib.parse

from direct_lookup import free_port, start_server, stop_server
from paired_banned_content import BUNDLE, REPOSITORY, REVISION, RUBY, isolated_campfire, start_redis
from paired_direct_lookup import login_campfire, seed_campfire, seed_rustfire, wait_for_server


CASES = (
    ("account no token", "PATCH", "/account", "none"),
    ("account bad token", "PATCH", "/account", "bad"),
    ("account good token", "PATCH", "/account", "good"),
    ("missing room no token", "DELETE", "/rooms/999", "none"),
    ("missing room bad token", "DELETE", "/rooms/999", "bad"),
    ("missing room good token", "DELETE", "/rooms/999", "good"),
    ("missing message room no token", "POST", "/rooms/999/messages", "none"),
    ("missing message room good token", "POST", "/rooms/999/messages", "good"),
    ("missing ban no token", "POST", "/users/999/ban", "none"),
    ("missing ban good token", "POST", "/users/999/ban", "good"),
    ("missing route no token", "POST", "/absent-action", "none"),
    ("missing route good token", "POST", "/absent-action", "good"),
    ("missing action no token", "POST", "/account/bots/1/key", "none"),
    ("missing action good token", "POST", "/account/bots/1/key", "good"),
    ("bot key override no token", "POST", "/account/bots/1/key", "override"),
    ("health post no token", "POST", "/up", "none"),
    ("health post good token", "POST", "/up", "good"),
    ("account edit post no token", "POST", "/account/edit", "none"),
    ("room settings post no token", "POST", "/rooms/1/settings", "none"),
    ("room refresh post no token", "POST", "/rooms/1/refresh", "none"),
    ("message edit post no token", "POST", "/rooms/1/messages/1/edit", "none"),
    ("user show post no token", "POST", "/users/1", "none"),
    ("sidebar post no token", "POST", "/users/me/sidebar", "none"),
    ("bot new post no token", "POST", "/account/bots/new", "none"),
    ("boost new post no token", "POST", "/messages/1/boosts/new", "none"),
    ("account users post no token", "POST", "/account/users", "none"),
    ("rooms collection post no token", "POST", "/rooms", "none"),
    ("messages collection post no token", "POST", "/messages", "none"),
    ("message resource post no token", "POST", "/messages/1", "none"),
)


def request(port, cookie, csrf, method, path, token_kind):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
    try:
        headers = {"Cookie": cookie, "Accept": "text/html", "Content-Type": "application/x-www-form-urlencoded"}
        if token_kind in ("good", "bad"):
            headers["X-CSRF-Token"] = csrf if token_kind == "good" else "incorrect"
        body = b"_method=put" if token_kind == "override" else b""
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        body = response.read()
        location = response.getheader("Location")
        if response.status == 200:
            title = re.search(rb"<title>(.*?)</title>", body, re.S)
            body_class = re.search(rb'<body class="([^"]*)"', body)
            frame = re.search(rb'<turbo-frame id="composer-frame">.*?</turbo-frame>', body, re.S)
            nav = re.search(rb'<nav id="nav">(.*?)</nav>', body, re.S)
            sidebar = re.search(rb'<aside id="sidebar"[^>]*>(.*?)</aside>', body, re.S)
            assert all((title, body_class, frame, nav, sidebar)), "missing room-deleted document sections"
            normalized_frame = re.sub(rb'>\s+<', b'><', frame.group(0))
            document = (title.group(1).decode(), body_class.group(1).decode(),
                        hashlib.sha256(normalized_frame).hexdigest(),
                        not nav.group(1).strip(), not sidebar.group(1).strip())
            return response.status, response.getheader("Content-Type", "").split(";", 1)[0], None, document
        return (response.status, response.getheader("Content-Type", "").split(";", 1)[0],
                urllib.parse.urlsplit(location).path if location else None,
                len(body), hashlib.sha256(body).hexdigest())
    finally:
        connection.close()


def main():
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPOSITORY, text=True).strip() == REVISION
    with tempfile.TemporaryDirectory(prefix="paired-csrf-write-routes-") as scratch:
        temp = pathlib.Path(scratch)
        rust_db, camp_db = temp / "rust.sqlite3", temp / "camp.sqlite3"
        rust_port, camp_port, redis_port = free_port(), free_port(), free_port()
        seed_rustfire(rust_db, rust_port, [])
        camp_env = seed_campfire(REPOSITORY, RUBY, BUNDLE, REPOSITORY / "storage/db/production.sqlite3", camp_db, [], camp_port, temp)
        checkout = isolated_campfire(temp, redis_port)
        camp_env["REDIS_URL"] = f"redis://127.0.0.1:{redis_port}"
        redis, redis_log = start_redis(temp, redis_port)
        try:
            rust = start_server(rust_db, rust_port)
            try:
                rust_results = [request(rust_port, "session_token=benchmark-session", "benchmark-csrf", method, path, token_kind)
                                for _, method, path, token_kind in CASES]
            finally:
                stop_server(rust)
            with open(temp / "puma.log", "w+") as log:
                camp = subprocess.Popen([str(RUBY), str(RUBY.parent / "bundle"), "exec", "puma", "-C", "config/puma.rb"],
                                        cwd=checkout, env=camp_env, stdout=log, stderr=log)
                try:
                    wait_for_server(camp_port, camp)
                    cookie, csrf = login_campfire(camp_port)
                    camp_results = [request(camp_port, cookie, csrf, method, path, token_kind)
                                    for _, method, path, token_kind in CASES]
                except Exception:
                    log.flush()
                    log.seek(0)
                    print(log.read()[-2000:])
                    raise
                finally:
                    stop_server(camp)
        finally:
            redis.terminate()
            redis.wait(timeout=10)
            redis_log.close()
    mismatches = [(case[0], rust, camp) for case, rust, camp in zip(CASES, rust_results, camp_results) if rust != camp]
    for label, rust, camp in mismatches:
        print(f"{label}: Rustfire={rust} Campfire={camp}")
    assert not mismatches, f"{len(mismatches)} CSRF/routing cases differ"
    print(f"PASS {len(CASES)} paired CSRF/routing cases match Campfire")


if __name__ == "__main__":
    main()
