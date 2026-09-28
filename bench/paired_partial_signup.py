"""Compare Campfire and Rustfire first-run and invitation submissions with omitted fields."""

import pathlib
import re
import shutil
import sqlite3
import subprocess
import tempfile
import urllib.parse

from direct_lookup import free_port, start_server, stop_server
from paired_banned_content import REDIS_CLI, start_redis
from paired_direct_lookup import campfire_env, wait_for_server
from paired_first_run import BUNDLE, REVISION, RUBY, SOURCE, fetch, fresh_campfire_database
from paired_join import browser, csrf_token, fetch as join_fetch


CASES = {
    "name_only": {"user[name]": "Admin"},
    "no_name": {"user[email_address]": "admin@example.invalid", "user[password]": "password123"},
    "no_password": {"user[name]": "Admin", "user[email_address]": "admin@example.invalid"},
    "no_email": {"user[name]": "Admin", "user[password]": "password123"},
    "all_blank": {"user[name]": "", "user[email_address]": "", "user[password]": ""},
    "scalar_user": {"user": "unexpected"},
    "array_user": {"user[]": "unexpected"},
    "password_72_bytes": {"user[name]": "Admin", "user[email_address]": "admin@example.invalid", "user[password]": "p" * 72},
    "password_73_bytes": {"user[name]": "Admin", "user[email_address]": "admin@example.invalid", "user[password]": "p" * 73},
    "password_80_utf8_bytes": {"user[name]": "Admin", "user[email_address]": "admin@example.invalid", "user[password]": "é" * 40},
}

QUERY_CASES = {
    "query_complete_user": (
        {"user[name]": "Body", "user[email_address]": "body@example.invalid", "user[password]": "body-password"},
        {"user[name]": "Query", "user[email_address]": "query@example.invalid", "user[password]": "query-password"},
    ),
    "query_name_only": (
        {"user[name]": "Body", "user[email_address]": "body@example.invalid", "user[password]": "body-password"},
        {"user[name]": "Query"},
    ),
    "query_scalar_user": (
        {"user[name]": "Body", "user[email_address]": "body@example.invalid", "user[password]": "body-password"},
        {"user": "unexpected"},
    ),
    "query_unknown_user": (
        {"user[name]": "Body", "user[email_address]": "body@example.invalid", "user[password]": "body-password"},
        {"user[unknown]": "ignored"},
    ),
}


def saved_state(database):
    with sqlite3.connect(database) as db:
        state = (
            db.execute("SELECT count(*) FROM accounts").fetchone()[0],
            db.execute("SELECT name,email_address,password_digest IS NULL,role FROM users").fetchall(),
            db.execute("SELECT name,type FROM rooms").fetchall(),
            db.execute("SELECT count(*) FROM memberships").fetchone()[0],
            db.execute("SELECT count(*) FROM sessions").fetchone()[0],
        )
        assert not db.execute("PRAGMA foreign_key_check").fetchall()
    return state


def submit(opener, port, path, fields, query=None):
    status, _, page = fetch(opener, port, path)
    assert status == 200
    token = re.search(r'name=[\'\"]authenticity_token[\'\"] value=[\'\"]([^\'\"]+)', page)
    assert token
    body = urllib.parse.urlencode({"authenticity_token": token.group(1), **fields}).encode()
    post_path = path + ("?" + urllib.parse.urlencode(query) if query else "")
    return fetch(opener, port, post_path, body, "application/x-www-form-urlencoded")


def check(port, database, submitted, mode, query=None):
    if mode == "first_run":
        response = submit(browser(), port, "/first_run", submitted, query)
    else:
        assert submit(browser(), port, "/first_run", {
            "user[name]": "Owner", "user[email_address]": "owner@example.invalid", "user[password]": "owner-password",
        })[:2] == (302, "/")
        with sqlite3.connect(database) as db:
            join_code = db.execute("SELECT join_code FROM accounts").fetchone()[0]
        response = submit(browser(), port, f"/join/{join_code}", submitted, query)
    login_results = []
    password = submitted.get("user[password]")
    if password and len(password.encode()) >= 72 and response[:2] == (302, "/"):
        alternate = password[:-1] + ("è" if password[-1] == "é" else "q")
        for candidate in (password, alternate):
            opener = browser()
            sign_in_page = join_fetch(opener, port, "/session/new")[2]
            status, location, _, _ = join_fetch(opener, port, "/session",
                                                 {"email_address": submitted["user[email_address]"], "password": candidate},
                                                 csrf_token(sign_in_page))
            login_results.append((status, location))
    return response, saved_state(database), login_results


def main():
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=SOURCE, text=True).strip() == REVISION
    with tempfile.TemporaryDirectory(prefix="paired-partial-signup-") as scratch:
        temp = pathlib.Path(scratch)
        source_root = temp / "source"
        shutil.copytree(SOURCE, source_root, ignore=shutil.ignore_patterns(".git", "storage", "tmp", "log"))
        (source_root / "storage/files").mkdir(parents=True)
        (source_root / "tmp").mkdir()
        redis_port = free_port()
        redis, redis_log = start_redis(temp, redis_port)
        try:
            cases = [(label, fields, None) for label, fields in CASES.items()]
            cases += [(label, fields, query) for label, (fields, query) in QUERY_CASES.items()]
            for mode, label, submitted, query in ((mode, label, fields, query) for mode in ("first_run", "join") for label, fields, query in cases):
                subprocess.run([str(REDIS_CLI), "-p", str(redis_port), "FLUSHDB"],
                               check=True, capture_output=True)
                rust_db, camp_db = temp / f"{mode}-{label}-rust.sqlite3", temp / f"{mode}-{label}-camp.sqlite3"
                rust_port, camp_port = free_port(), free_port()
                fresh_campfire_database(camp_db)
                rust = start_server(rust_db, rust_port)
                try:
                    rust_result = check(rust_port, rust_db, submitted, mode, query)
                finally:
                    stop_server(rust)
                environment = campfire_env(source_root, RUBY, BUNDLE, camp_db, camp_port, temp)
                environment["REDIS_URL"] = f"redis://127.0.0.1:{redis_port}"
                environment["WEB_CONCURRENCY"] = "1"
                with open(temp / f"{mode}-{label}.log", "w+") as log:
                    camp = subprocess.Popen([str(RUBY), str(RUBY.parent / "bundle"), "exec", "puma", "-C", "config/puma.rb"],
                        cwd=source_root, env=environment, stdout=log, stderr=log)
                    try:
                        wait_for_server(camp_port, camp)
                        camp_result = check(camp_port, camp_db, submitted, mode, query)
                    except Exception:
                        log.flush()
                        log.seek(0)
                        print(log.read()[-3000:])
                        raise
                    finally:
                        stop_server(camp)
                assert rust_result == camp_result, (mode, label, rust_result, camp_result)
                status, location, response_body = rust_result[0]
                login = f"; exact/changed-suffix login {rust_result[2]}" if rust_result[2] else ""
                print(f"{mode}/{label}: {status} {location or '-'}; {len(response_body)} response chars; saved {rust_result[1][0]} account(s), {len(rust_result[1][1])} user(s){login}")
        finally:
            redis.terminate()
            redis.wait(timeout=10)
            redis_log.close()
    print(f"PASS {len(CASES) + len(QUERY_CASES)} paired first-run and invitation parameter cases per route")


if __name__ == "__main__":
    main()
