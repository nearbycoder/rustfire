"""Compare invalid-CSRF responses on matched disposable Campfire fixtures."""

import argparse
import hashlib
import http.client
import pathlib
import sqlite3
import subprocess
import tempfile
import urllib.parse

from direct_lookup import free_port, start_server, stop_server
from paired_banned_content import BUNDLE, REPOSITORY, REVISION, RUBY, isolated_campfire, start_redis
from paired_direct_lookup import login_campfire, seed_campfire, seed_rustfire, wait_for_server
from paired_turbo_fanout import seed_boost_message


CASES = (
    ("POST", "/account"),
    ("POST", "/account/bots"),
    ("PATCH", "/account/bots/1/key"),
    ("PATCH", "/account/users/2"),
    ("POST", "/account/join_code"),
    ("PATCH", "/account/custom_styles"),
    ("POST", "/rooms/1/messages"),
    ("PATCH", "/rooms/1/messages/1"),
    ("POST", "/rooms/opens"),
    ("POST", "/messages/1/boosts"),
    ("POST", "/searches"),
    ("POST", "/unfurl_link"),
    ("PATCH", "/users/me/profile"),
    ("POST", "/users/me/push_subscriptions"),
    ("PATCH", "/rooms/1/involvement"),
)
ACCEPTS = ("text/html", "application/json", "text/vnd.turbo-stream.html", "*/*")
OVERRIDE_CASES = (
    ("POST", "/account", "_method=patch&authenticity_token=invalid"),
    ("POST", "/account.1", "_method=put&authenticity_token=invalid"),
    ("POST", "/account.1", "authenticity_token=invalid"),
)
TOKEN_CHANNEL_CASES = (
    ("form-only", "valid", None),
    ("header-only", None, "valid"),
    ("valid-header-invalid-form", "invalid", "valid"),
    ("valid-form-invalid-header", "valid", "invalid"),
    ("invalid-both", "invalid", "invalid"),
    ("missing-both", None, None),
    ("invalid-header-only", None, "invalid"),
    ("valid-form-first-invalid-last", "valid-first-invalid-last", None),
    ("invalid-form-first-valid-last", "invalid-first-valid-last", None),
)
MULTIPART_CASES = (
    "valid-token-field",
    "invalid-token-valid-decoy",
    "missing-token-body-decoy",
    "valid-first-invalid-last",
    "invalid-first-valid-last",
    "invalid-token-file",
)


def request(port, cookie, method, path, accept, body, compare_bodies, extra_headers=None):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
    try:
        headers = {
            "Accept": accept,
            "Content-Type": "application/x-www-form-urlencoded",
        }
        if cookie:
            headers["Cookie"] = cookie
        if extra_headers:
            headers.update(extra_headers)
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        body = response.read()
        location = response.getheader("Location")
        media = (response.getheader("Content-Type") or "").split(";", 1)[0]
        result = (response.status, media, urllib.parse.urlsplit(location).path if location else None)
        if compare_bodies:
            result += (len(body), hashlib.sha256(body).hexdigest())
        return result
    finally:
        connection.close()


def token_channel_requests(port, cookie, csrf):
    results = {}
    for label, form_token, header_token in TOKEN_CHANNEL_CASES:
        fields = [("q", f"csrf-{label}")]
        if form_token == "valid-first-invalid-last":
            fields.extend((("authenticity_token", csrf), ("authenticity_token", "invalid")))
        elif form_token == "invalid-first-valid-last":
            fields.extend((("authenticity_token", "invalid"), ("authenticity_token", csrf)))
        elif form_token:
            fields.append(("authenticity_token", csrf if form_token == "valid" else "invalid"))
        headers = {"X-CSRF-Token": csrf if header_token == "valid" else "invalid"} if header_token else None
        body = urllib.parse.urlencode(fields)
        results[label] = request(port, cookie, "POST", "/searches", "text/html", body, True, headers)
    return results


def saved_searches(database):
    with sqlite3.connect(database) as db:
        return sorted(row[0] for row in db.execute("SELECT query FROM searches WHERE query LIKE 'csrf %'"))


def multipart_token_requests(port, cookie, csrf):
    boundary = "rustfire-csrf-boundary"
    results = {}
    for label in MULTIPART_CASES:
        fields = [("message[body]", f"multipart csrf {label}"),
                  ("message[client_message_id]", f"csrf-{label}")]
        if label == "valid-token-field":
            fields.append(("authenticity_token", csrf))
        elif label == "invalid-token-valid-decoy":
            fields.extend((("authenticity_token", "invalid"), ("csrf_decoy", csrf)))
        elif label == "missing-token-body-decoy":
            fields[0] = ("message[body]", fields[0][1] + f" {csrf}")
        elif label == "valid-first-invalid-last":
            fields.extend((("authenticity_token", csrf), ("authenticity_token", "invalid")))
        elif label == "invalid-first-valid-last":
            fields.extend((("authenticity_token", "invalid"), ("authenticity_token", csrf)))
        elif label == "invalid-token-file":
            fields.append(("authenticity_token", "invalid"))
        body = b"".join(
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n".encode()
            for name, value in fields
        )
        if label == "invalid-token-file":
            body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"message[attachment]\"; filename=\"invalid.bin\"\r\n"
                     "Content-Type: application/octet-stream\r\n\r\n").encode() + b"R" * (8 * 1024 * 1024) + b"\r\n"
        body += f"--{boundary}--\r\n".encode()
        results[label] = request(port, cookie, "POST", "/rooms/1/messages", "text/html", body, True,
                                 {"Content-Type": f"multipart/form-data; boundary={boundary}"})
    return results


def saved_multipart_messages(database):
    with sqlite3.connect(database) as db:
        return sorted(row[0] for row in db.execute("SELECT client_message_id FROM messages WHERE client_message_id LIKE 'csrf-%'"))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--all-accepts", action="store_true", help="compare HTML, JSON, Turbo, and wildcard Accept headers")
    parser.add_argument("--compare-bodies", action="store_true", help="also compare response body length and SHA-256")
    parser.add_argument("--include-anonymous", action="store_true", help="also compare requests without a session cookie")
    parser.add_argument("--token-channels", action="store_true", help="compare form and X-CSRF-Token header precedence on saved searches")
    parser.add_argument("--multipart-token-fields", action="store_true", help="compare multipart authenticity-token field parsing")
    args = parser.parse_args()
    accepts = ACCEPTS if args.all_accepts else ("text/html",)
    authenticated_cases = [(method, path, accept, "authenticity_token=invalid") for method, path in CASES for accept in accepts]
    authenticated_cases += [(method, path, accept, body) for method, path, body in OVERRIDE_CASES for accept in accepts]
    cases = [(True, *case) for case in authenticated_cases]
    if args.include_anonymous:
        cases += [(False, *case) for case in authenticated_cases]
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPOSITORY, text=True).strip() == REVISION
    with tempfile.TemporaryDirectory(prefix="paired-csrf-precedence-") as scratch:
        temp = pathlib.Path(scratch)
        rust_db, camp_db = temp / "rust.sqlite3", temp / "camp.sqlite3"
        rust_uploads = temp / "rust-uploads"
        rust_port, camp_port, redis_port = free_port(), free_port(), free_port()
        seed_rustfire(rust_db, rust_port, [])
        camp_env = seed_campfire(REPOSITORY, RUBY, BUNDLE, REPOSITORY / "storage/db/production.sqlite3", camp_db, [], camp_port, temp)
        seed_boost_message(rust_db, camp_db)
        checkout = isolated_campfire(temp, redis_port)
        camp_env["REDIS_URL"] = f"redis://127.0.0.1:{redis_port}"
        redis, redis_log = start_redis(temp, redis_port)
        try:
            rust = start_server(rust_db, rust_port, {"RUSTFIRE_UPLOAD_DIR": str(rust_uploads)})
            try:
                rust_responses = {case: request(rust_port, "session_token=benchmark-session" if case[0] else "", *case[1:], args.compare_bodies) for case in cases}
                if args.token_channels:
                    rust_channels = token_channel_requests(rust_port, "session_token=benchmark-session", "benchmark-csrf")
                if args.multipart_token_fields:
                    rust_multipart = multipart_token_requests(rust_port, "session_token=benchmark-session", "benchmark-csrf")
            finally:
                stop_server(rust)
            with open(temp / "puma.log", "w+") as log:
                camp = subprocess.Popen([str(RUBY), str(RUBY.parent / "bundle"), "exec", "puma", "-C", "config/puma.rb"], cwd=checkout, env=camp_env, stdout=log, stderr=log)
                try:
                    wait_for_server(camp_port, camp)
                    cookie, camp_csrf = login_campfire(camp_port)
                    camp_responses = {case: request(camp_port, cookie if case[0] else "", *case[1:], args.compare_bodies) for case in cases}
                    if args.token_channels:
                        camp_channels = token_channel_requests(camp_port, cookie, camp_csrf)
                    if args.multipart_token_fields:
                        camp_multipart = multipart_token_requests(camp_port, cookie, camp_csrf)
                finally:
                    stop_server(camp)
        finally:
            redis.terminate()
            redis.wait(timeout=10)
            redis_log.close()
        if args.token_channels:
            rust_searches, camp_searches = saved_searches(rust_db), saved_searches(camp_db)
        if args.multipart_token_fields:
            rust_messages, camp_messages = saved_multipart_messages(rust_db), saved_multipart_messages(camp_db)
            staged_uploads = list(rust_uploads.glob("message-upload-*"))
    mismatches = [(case, rust_responses[case], camp_responses[case])
                  for case in cases if rust_responses[case] != camp_responses[case]]
    for (authenticated, method, path, accept, body), rust, camp in mismatches:
        print(f"{'authenticated' if authenticated else 'anonymous'} {method} {path} Accept={accept} body={body}: Rustfire {rust}, Campfire {camp}")
    print(f"Matched {len(cases) - len(mismatches)}/{len(cases)} invalid-CSRF response cases")
    if args.token_channels:
        channel_mismatches = [(case[0], rust_channels[case[0]], camp_channels[case[0]])
                              for case in TOKEN_CHANNEL_CASES if rust_channels[case[0]] != camp_channels[case[0]]]
        for label, result in rust_channels.items():
            print(f"{label}: {result[:3]}")
        for label, rust, camp in channel_mismatches:
            print(f"{label}: Rustfire {rust}, Campfire {camp}")
        print(f"Matched {len(TOKEN_CHANNEL_CASES) - len(channel_mismatches)}/{len(TOKEN_CHANNEL_CASES)} CSRF token-channel responses")
        print(f"Saved searches: Rustfire {rust_searches}, Campfire {camp_searches}")
        expected_searches = sorted(f"csrf {label.replace('-', ' ')}" for label, form_token, header_token
                                   in TOKEN_CHANNEL_CASES if form_token in ("valid", "invalid-first-valid-last") or header_token == "valid")
        if rust_searches != expected_searches or camp_searches != expected_searches:
            raise AssertionError(f"Expected only accepted token channels to save searches: {expected_searches}")
        if channel_mismatches or rust_searches != camp_searches:
            raise AssertionError("CSRF token-channel responses or saved searches differ")
    if args.multipart_token_fields:
        multipart_mismatches = [(label, rust_multipart[label], camp_multipart[label])
                                for label in MULTIPART_CASES if rust_multipart[label] != camp_multipart[label]]
        for label, rust, camp in multipart_mismatches:
            print(f"{label}: Rustfire {rust}, Campfire {camp}")
        print(f"Matched {len(MULTIPART_CASES) - len(multipart_mismatches)}/{len(MULTIPART_CASES)} multipart CSRF responses")
        print(f"Saved multipart messages: Rustfire {rust_messages}, Campfire {camp_messages}")
        expected_messages = ["csrf-invalid-first-valid-last", "csrf-valid-token-field"]
        if rust_messages != expected_messages or camp_messages != expected_messages:
            raise AssertionError(f"Expected only accepted multipart tokens to save messages: {expected_messages}")
        if staged_uploads:
            raise AssertionError(f"Rejected multipart upload left staged files: {staged_uploads}")
        if multipart_mismatches or rust_messages != camp_messages:
            raise AssertionError("Multipart CSRF responses or saved messages differ")
    if mismatches:
        raise AssertionError(f"{len(mismatches)} invalid-CSRF cases differ")


if __name__ == "__main__":
    main()
