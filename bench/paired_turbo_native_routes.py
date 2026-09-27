"""Compare Hotwire Native navigation routes against pinned Campfire."""

import hashlib
import http.client
import pathlib
import subprocess
import tempfile

from direct_lookup import free_port, start_server, stop_server
from paired_banned_content import BUNDLE, REPOSITORY, REVISION, RUBY, isolated_campfire, start_redis
from paired_direct_lookup import login_campfire, seed_campfire, seed_rustfire, wait_for_server


PATHS = (
    "/recede_historical_location",
    "/resume_historical_location",
    "/refresh_historical_location",
)
ACCEPTS = ("text/html", "application/json", "text/vnd.turbo-stream.html", "*/*")
HEADERS = ("Vary", "Referrer-Policy", "X-Content-Type-Options", "X-Frame-Options",
           "X-Permitted-Cross-Domain-Policies", "X-XSS-Protection", "X-Version", "X-Rev", "Cache-Control")
CASES = tuple(
    ("GET", authenticated, path + suffix, accept)
    for authenticated in (True, False)
    for path in PATHS
    for suffix in ("", ".json", ".turbo_stream", ".xml", ".bogus", "?format=json")
    for accept in ACCEPTS
) + tuple(("HEAD", authenticated, path, "text/html")
          for authenticated in (True, False) for path in PATHS)


def request(port, cookie, method, path, accept):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
    try:
        headers = {"Accept": accept}
        if cookie:
            headers["Cookie"] = cookie
        connection.request(method, path, headers=headers)
        response = connection.getresponse()
        body = response.read()
        return (
            response.status,
            response.getheader("Content-Type"),
            response.getheader("Location"),
            len(body),
            hashlib.sha256(body).hexdigest(),
            tuple(response.getheader(name) for name in HEADERS),
        )
    finally:
        connection.close()


def collect(port, cookie):
    return {case: request(port, cookie if case[1] else "", case[0], case[2], case[3]) for case in CASES}


def main():
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPOSITORY, text=True).strip() == REVISION
    with tempfile.TemporaryDirectory(prefix="paired-turbo-native-") as scratch:
        temp = pathlib.Path(scratch)
        rust_db, camp_db = temp / "rust.sqlite3", temp / "camp.sqlite3"
        rust_port, camp_port, redis_port = free_port(), free_port(), free_port()
        seed_rustfire(rust_db, rust_port, [])
        environment = seed_campfire(REPOSITORY, RUBY, BUNDLE, REPOSITORY / "storage/db/production.sqlite3", camp_db, [], camp_port, temp)
        checkout = isolated_campfire(temp, redis_port)
        environment["REDIS_URL"] = f"redis://127.0.0.1:{redis_port}"
        redis, redis_log = start_redis(temp, redis_port)
        try:
            rust = start_server(rust_db, rust_port)
            try:
                rust_responses = collect(rust_port, "session_token=benchmark-session")
            finally:
                stop_server(rust)
            with open(temp / "puma.log", "w+") as log:
                camp = subprocess.Popen([str(RUBY), str(RUBY.parent / "bundle"), "exec", "puma", "-C", "config/puma.rb"],
                                        cwd=checkout, env=environment, stdout=log, stderr=log)
                try:
                    wait_for_server(camp_port, camp)
                    cookie, _ = login_campfire(camp_port)
                    camp_responses = collect(camp_port, cookie)
                finally:
                    stop_server(camp)
        finally:
            redis.terminate()
            redis.wait(timeout=10)
            redis_log.close()
    mismatches = [(case, rust_responses[case], camp_responses[case])
                  for case in CASES if rust_responses[case] != camp_responses[case]]
    for case, rust, camp in mismatches[:20]:
        print(f"{case}: Rustfire {rust}, Campfire {camp}")
    print(f"Matched {len(CASES) - len(mismatches)}/{len(CASES)} Hotwire Native GET/HEAD responses")
    if mismatches:
        raise AssertionError(f"{len(mismatches)} Hotwire Native GET/HEAD responses differ")


if __name__ == "__main__":
    main()
