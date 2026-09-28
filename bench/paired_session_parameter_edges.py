"""Compare sign-in query/body parameter precedence with pinned Campfire."""

from html.parser import HTMLParser
import pathlib
import sqlite3
import subprocess
import tempfile
import urllib.parse

from direct_lookup import free_port, start_server, stop_server
from paired_banned_content import BUNDLE, REPOSITORY, REVISION, RUBY, isolated_campfire, start_redis
from paired_direct_lookup import seed_campfire, seed_rustfire, wait_for_server
from paired_join import browser
from paired_session import request, session_count, token


GOOD = {"email_address": "benchmark@example.invalid", "password": "benchmark-password"}
BAD = {"email_address": "wrong@example.invalid", "password": "wrong-password"}
CASES = (
    ("body valid", GOOD, {}),
    ("query valid", {}, GOOD),
    ("query overrides valid body", GOOD, BAD),
    ("query overrides invalid body", BAD, GOOD),
    ("query email only", GOOD, {"email_address": BAD["email_address"]}),
    ("query password only", GOOD, {"password": BAD["password"]}),
    ("empty query email", GOOD, {"email_address": ""}),
    ("empty query password", GOOD, {"password": ""}),
    ("last duplicate query email valid", BAD, (("email_address", BAD["email_address"]), ("email_address", GOOD["email_address"]), ("password", GOOD["password"]))),
    ("last duplicate query email invalid", GOOD, (("email_address", GOOD["email_address"]), ("email_address", BAD["email_address"]))),
)


class EmailField(HTMLParser):
    def __init__(self):
        super().__init__()
        self.email = None

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == "input" and values.get("name") == "email_address":
            self.email = values.get("value")


def login_result(port, database, body, query):
    opener = browser()
    status, _, page = request(opener, port, "/session/new")
    assert status == 200
    before = session_count(database)
    path = "/session" + ("?" + urllib.parse.urlencode(query) if query else "")
    status, location, response = request(opener, port, path, "POST", body, token(page))
    field = EmailField()
    field.feed(response)
    return status, location, field.email, session_count(database) - before


def main():
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPOSITORY, text=True).strip() == REVISION
    with tempfile.TemporaryDirectory(prefix="paired-session-parameters-") as scratch:
        temp = pathlib.Path(scratch)
        rust_db, camp_db = temp / "rust.sqlite3", temp / "camp.sqlite3"
        rust_port, camp_port, redis_port = free_port(), free_port(), free_port()
        seed_rustfire(rust_db, rust_port, [])
        camp_env = seed_campfire(REPOSITORY, RUBY, BUNDLE, REPOSITORY / "storage/db/production.sqlite3", camp_db, [], camp_port, temp)
        with sqlite3.connect(camp_db) as source, sqlite3.connect(rust_db) as target:
            digest = source.execute("SELECT password_digest FROM users WHERE id=1").fetchone()[0]
            target.execute("UPDATE users SET email_address=?,password_digest=? WHERE id=1", (GOOD["email_address"], digest))
        checkout = isolated_campfire(temp, redis_port)
        camp_env["REDIS_URL"] = f"redis://127.0.0.1:{redis_port}"
        redis, redis_log = start_redis(temp, redis_port)
        try:
            rust = start_server(rust_db, rust_port)
            with open(temp / "puma.log", "w+") as log:
                camp = subprocess.Popen([str(RUBY), str(RUBY.parent / "bundle"), "exec", "puma", "-C", "config/puma.rb"],
                                        cwd=checkout, env=camp_env, stdout=log, stderr=log)
                try:
                    wait_for_server(camp_port, camp)
                    results = []
                    for label, body, query in CASES:
                        actual = login_result(rust_port, rust_db, body, query)
                        expected = login_result(camp_port, camp_db, body, query)
                        results.append((label, actual, expected))
                except Exception:
                    log.flush()
                    log.seek(0)
                    print(log.read()[-2000:])
                    raise
                finally:
                    stop_server(camp)
                    stop_server(rust)
        finally:
            redis.terminate()
            redis.wait(timeout=10)
            redis_log.close()
    mismatches = [(label, actual, expected) for label, actual, expected in results if actual != expected]
    for label, actual, expected in mismatches:
        print(f"{label}: Rustfire={actual}, Campfire={expected}")
    assert not mismatches, f"{len(mismatches)} sign-in parameter cases differ"
    print(f"PASS {len(CASES)} paired sign-in parameter cases match Campfire")


if __name__ == "__main__":
    main()
