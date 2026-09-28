"""Compare device-transfer redirects after an anonymous protected-page visit."""

import http.cookiejar
import pathlib
import re
import subprocess
import tempfile
import urllib.error
import urllib.parse
import urllib.request

from direct_lookup import free_port, start_server, stop_server
from paired_banned_content import BUNDLE, REPOSITORY, REVISION, RUBY, start_redis
from paired_direct_lookup import login_campfire, seed_campfire, seed_rustfire, wait_for_server
from paired_profile import transfer_path


PATHS = ("/users/me/profile?from=transfer", "/rooms/1?focus=messages")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, destination):
        return None


def request(opener, base, path, method="GET", data=None, headers=None):
    request = urllib.request.Request(base + path, data=data, method=method, headers=headers or {})
    try:
        response = opener.open(request)
    except urllib.error.HTTPError as error:
        response = error
    with response:
        location = urllib.parse.urlsplit(response.headers.get("Location", ""))
        destination = location.path + ("?" + location.query if location.query else "") if location.path else None
        return response.status, destination, response.read()


def transfer_link(port, cookie):
    connection = urllib.request.build_opener(NoRedirect())
    status, _, body = request(connection, f"http://127.0.0.1:{port}", "/users/me/profile", headers={"Cookie": cookie})
    assert status == 200, status
    return transfer_path(body)


def run(port, link, path):
    base = f"http://127.0.0.1:{port}"
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()), NoRedirect())
    first = request(opener, base, path)
    assert first[:2] == (302, "/session/new"), first[:2]
    status, _, show = request(opener, base, link)
    assert status == 200, status
    match = re.search(rb'<meta name=["\']csrf-token["\'] content=["\']([^"\']+)', show)
    assert match, "Missing transfer-page CSRF token"
    result = request(opener, base, link, "PUT", b"", {"X-CSRF-Token": match.group(1).decode()})
    assert request(opener, base, "/users/me/profile")[0] == 200, "Transfer did not establish a session"
    status, _, second_show = request(opener, base, link)
    assert status == 200, status
    second_match = re.search(rb'<meta name=["\']csrf-token["\'] content=["\']([^"\']+)', second_show)
    assert second_match, "Missing second transfer-page CSRF token"
    repeated = request(opener, base, link, "PUT", b"", {"X-CSRF-Token": second_match.group(1).decode()})
    return (result[0], result[1]), (repeated[0], repeated[1])


def main():
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPOSITORY, text=True).strip() == REVISION
    with tempfile.TemporaryDirectory(prefix="paired-transfer-return-") as scratch:
        temp = pathlib.Path(scratch)
        rust_db, camp_db = temp / "rust.sqlite3", temp / "camp.sqlite3"
        rust_port, camp_port, redis_port = free_port(), free_port(), free_port()
        seed_rustfire(rust_db, rust_port, [])
        environment = seed_campfire(REPOSITORY, RUBY, BUNDLE, REPOSITORY / "storage/db/production.sqlite3", camp_db, [], camp_port, temp)
        environment["REDIS_URL"] = f"redis://127.0.0.1:{redis_port}"
        redis, redis_log = start_redis(temp, redis_port)
        try:
            rust = start_server(rust_db, rust_port, {"RUSTFIRE_CAMPFIRE_SECRET_KEY_BASE": environment["SECRET_KEY_BASE"]})
            with (temp / "puma.log").open("w+") as log:
                camp = subprocess.Popen(
                    [str(RUBY), str(RUBY.parent / "bundle"), "exec", "puma", "-C", "config/puma.rb"],
                    cwd=REPOSITORY, env=environment, stdout=log, stderr=log,
                )
                try:
                    wait_for_server(camp_port, camp)
                    camp_cookie, _ = login_campfire(camp_port)
                    links = {
                        "Campfire": transfer_link(camp_port, camp_cookie),
                        "Rustfire": transfer_link(rust_port, "session_token=benchmark-session"),
                    }
                    results = [
                        (path, run(rust_port, links["Rustfire"], path), run(camp_port, links["Campfire"], path))
                        for path in PATHS
                    ]
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
    for path, rust, camp in results:
        if rust != camp:
            print(f"{path}: Rustfire={rust}, Campfire={camp}")
    assert all(rust == camp and rust == ((302, path), (302, "/")) for path, rust, camp in results)
    print(f"PASS {len(results)} device-transfer return destinations match Campfire")


if __name__ == "__main__":
    main()
