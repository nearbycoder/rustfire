"""Compare GET status and media type across common Rails formats and roles.

Run after cargo build --release. Both applications use disposable databases,
and Campfire runs from the pinned source in an isolated checkout and Redis.
"""

import argparse
import http.client
import pathlib
import re
import sqlite3
import subprocess
import tempfile
import urllib.parse

from direct_lookup import free_port, start_server, stop_server
from paired_banned_content import BUNDLE, REPOSITORY, REVISION, RUBY, isolated_campfire, start_redis
from paired_direct_lookup import login_campfire, seed_campfire, seed_rustfire, wait_for_server
from paired_turbo_fanout import seed_boost_message


PATHS = (
    "/", "/first_run", "/session/new", "/account/edit", "/account/users",
    "/account/bots", "/account/bots/new", "/account/logo",
    "/account/custom_styles/edit", "/users/1", "/users/1/profile",
    "/users/me/profile", "/users/1/sidebar", "/users/me/sidebar",
    "/users/1/push_subscriptions", "/users/me/push_subscriptions",
    "/autocompletable/users", "/rooms", "/rooms/1", "/rooms/1/messages",
    "/rooms/1/messages/1", "/rooms/1/messages/1/edit", "/rooms/1/refresh",
    "/rooms/1/involvement", "/rooms/1/@1",
    "/rooms/opens", "/rooms/opens/1", "/messages/1/boosts",
    "/messages/1/boosts/new", "/searches",
)
SUFFIXES = ("", ".html", ".json", ".turbo_stream")
ACCEPTS = ("text/html", "application/json", "text/vnd.turbo-stream.html", "*/*")
MIXED_PATHS = (
    "/session/new", "/account/users", "/autocompletable/users", "/rooms/1",
    "/rooms/1/refresh", "/rooms/1/messages/1", "/account/logo", "/searches",
)


def inventory_paths(checkout, env):
    """Expand the pinned Rails GET routes into one representative URL each."""
    output = subprocess.check_output(
        [str(RUBY), str(RUBY.parent / "bundle"), "exec", "bin/rails", "routes"],
        cwd=checkout, env=env, text=True,
    )
    paths = []
    for line in output.splitlines():
        match = re.search(r"\bGET\s+(\S+)\s+\S+#\S+", line)
        if not match:
            continue
        path = match.group(1).replace("(.:format)", "")
        if path.startswith("/rails/"):
            continue
        for name, value in (
            (":user_id", "me"), (":room_id", "1"),
            (":message_id", "1"), (":join_code", "invalid"),
            (":bot_key", "invalid"), (":id", "1"),
        ):
            path = path.replace(name, value)
        if path not in paths:
            paths.append(path)
    return paths


def request(port, path, cookie, accept, compare_406_bodies, compare_error_bodies, compare_redirects):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
    try:
        connection.request("GET", path, headers={"Cookie": cookie, "Accept": accept})
        response = connection.getresponse()
        body = response.read()
        content_type = (response.getheader("Content-Type") or "").split(";", 1)[0]
        result = (response.status, content_type)
        if compare_406_bodies or compare_error_bodies:
            statuses = (403, 406) if compare_error_bodies else (406,)
            result += (body if response.status in statuses else None,)
        if compare_redirects:
            location = response.getheader("Location")
            target = urllib.parse.urlsplit(location) if location else None
            result += ((target.path + ("?" + target.query if target.query else "")) if target else None,)
        return result
    finally:
        connection.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--all-accepts", action="store_true", help="compare HTML, JSON, Turbo, and wildcard Accept headers")
    parser.add_argument("--query-formats", action="store_true", help="request ?format=html, json, and turbo_stream instead of path suffixes")
    parser.add_argument("--mixed-formats", action="store_true", help="combine format suffixes and conflicting ?format= values on selected paths")
    parser.add_argument("--route-inventory", action="store_true", help="check every application GET route from the pinned Rails route table")
    parser.add_argument("--exclude-source-500s", action="store_true", help="report but exclude routes that fail inside pinned Campfire")
    parser.add_argument("--compare-406-bodies", action="store_true", help="also compare Not Acceptable response bodies")
    parser.add_argument("--compare-error-bodies", action="store_true", help="also compare forbidden and Not Acceptable response bodies")
    parser.add_argument("--compare-redirects", action="store_true", help="also compare redirect path and query")
    parser.add_argument("--role", choices=("admin", "member", "anonymous"), default="admin", help="request as an admin, member, or signed-out visitor")
    args = parser.parse_args()
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPOSITORY, text=True).strip() == REVISION
    with tempfile.TemporaryDirectory(prefix="paired-get-formats-") as scratch:
        temp = pathlib.Path(scratch)
        rust_db, camp_db = temp / "rust.sqlite3", temp / "camp.sqlite3"
        rust_port, camp_port, redis_port = free_port(), free_port(), free_port()
        seed_rustfire(rust_db, rust_port, [])
        camp_env = seed_campfire(REPOSITORY, RUBY, BUNDLE, REPOSITORY / "storage/db/production.sqlite3", camp_db, [], camp_port, temp)
        seed_boost_message(rust_db, camp_db)
        with sqlite3.connect(rust_db) as rust:
            rust.execute("INSERT INTO sessions(user_id,token,csrf_token,created_at,last_active_at) VALUES(2,'benchmark-member-session','benchmark-member-csrf','2026-01-01T00:00:00Z','2026-01-01T00:00:00Z')")
        with sqlite3.connect(camp_db) as camp:
            camp.execute("UPDATE users SET email_address='member@example.invalid',password_digest=(SELECT password_digest FROM users WHERE id=1) WHERE id=2")
        checkout = isolated_campfire(temp, redis_port)
        camp_env["REDIS_URL"] = f"redis://127.0.0.1:{redis_port}"
        redis, redis_log = start_redis(temp, redis_port)
        # Pinned Campfire routes /rooms/:id/settings to a missing controller and
        # returns 500 for all four formats, so it is not a usable parity case.
        base_paths = inventory_paths(checkout, camp_env) if args.route_inventory else PATHS
        if args.mixed_formats:
            paths = [f"{path}.{suffix}?format={query}" for path in MIXED_PATHS
                     for suffix in ("html", "json", "turbo_stream") for query in ("html", "json", "turbo_stream")]
        elif args.query_formats:
            paths = [f"{path}?format={format_name}" for path in base_paths for format_name in ("html", "json", "turbo_stream")]
        else:
            paths = [path + suffix for path in base_paths for suffix in (SUFFIXES if path != "/" else ("",))]
        accepts = ACCEPTS if args.all_accepts else ("text/html",)
        cases = [(path, accept) for path in paths for accept in accepts]
        try:
            rust = start_server(rust_db, rust_port)
            try:
                rust_cookie = {"admin": "session_token=benchmark-session", "member": "session_token=benchmark-member-session", "anonymous": ""}[args.role]
                rust_responses = {case: request(rust_port, case[0], rust_cookie, case[1], args.compare_406_bodies, args.compare_error_bodies, args.compare_redirects) for case in cases}
            finally:
                stop_server(rust)
            with open(temp / "puma.log", "w+") as log:
                camp = subprocess.Popen([str(RUBY), str(RUBY.parent / "bundle"), "exec", "puma", "-C", "config/puma.rb"], cwd=checkout, env=camp_env, stdout=log, stderr=log)
                try:
                    wait_for_server(camp_port, camp)
                    cookie, _ = login_campfire(camp_port)
                    camp_cookie = login_campfire(camp_port, email="member@example.invalid")[0] if args.role == "member" else cookie if args.role == "admin" else ""
                    camp_responses = {case: request(camp_port, case[0], camp_cookie, case[1], args.compare_406_bodies, args.compare_error_bodies, args.compare_redirects) for case in cases}
                finally:
                    stop_server(camp)
        finally:
            redis.terminate()
            redis.wait(timeout=10)
            redis_log.close()
    excluded = [case for case in cases if args.exclude_source_500s and camp_responses[case][0] == 500]
    for path, accept in excluded:
        print(f"Excluded pinned-source 500: {path} Accept={accept}")
    checked = [case for case in cases if case not in excluded]
    mismatches = [(case, rust_responses[case], camp_responses[case]) for case in checked if rust_responses[case] != camp_responses[case]]
    for (path, accept), rust_response, camp_response in mismatches:
        print(f"{path} Accept={accept}: Rustfire {rust_response}, Campfire {camp_response}")
    print(f"Matched {len(checked) - len(mismatches)}/{len(checked)} {args.role} GET status/media-type cases ({len(excluded)} source 500s excluded)")
    if mismatches:
        raise AssertionError(f"{len(mismatches)} GET format cases differ")


if __name__ == "__main__":
    main()
