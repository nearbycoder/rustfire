"""Compare search page sections and checked read throughput with pinned Campfire.

Run after ``cargo build --release``. The fixture uses the same search messages
and recent query in both apps; generated CSRF values and origins are normalized.
Optional concurrent reads require at least 100 messages and validate every
response's status, content type, and rendered-message count.
"""

import argparse
import json
import pathlib
import re
import shutil
import sqlite3
import subprocess
import tempfile
import uuid

from direct_lookup import free_port, start_server, stop_server
from paired_direct_lookup import login_campfire, seed_campfire, seed_rustfire, wait_for_server
from paired_room_shell import Section, get_room, post_message
from paired_search import seed_messages


REPOSITORY = pathlib.Path("/tmp/once-campfire-reference")
RUBY = pathlib.Path("/tmp/rustfire-baseline/local/bin/ruby")
BUNDLE = pathlib.Path("/tmp/rustfire-baseline/bundle")
REVISION = "91d294f4a09f9bbe37f9548959bfcb43645678fb"


def section(page, target):
    parser = Section(target, normalize_times=True)
    parser.feed(page.decode())
    tokens = []
    for token in parser.tokens:
        if token[0] == "start":
            attrs = []
            for key, value in token[2]:
                if key == "action":
                    value = value.removeprefix("<origin>")
                if key == "src":
                    value = re.sub(r"/users/[^/]+/avatar", "/users/<signed-avatar>/avatar", value)
                attrs.append((key, value))
            attrs = tuple(attrs)
            tokens.append((token[0], token[1], attrs))
        else:
            tokens.append(token)
    assert tokens, target
    return tokens


def compare(source, target, label):
    for part in ("nav", "sidebar", "message-area", "footer"):
        expected, actual = section(source, part), section(target, part)
        for index, (left, right) in enumerate(zip(expected, actual)):
            assert left == right, (label, part, index, left, right)
        assert len(expected) == len(actual), (label, part, len(expected), len(actual))
        print(f"{label} {part}: {len(actual)} matching parsed tokens")
    assert 'class="sidebar searches admin"' in target.decode()


def measure_search(binary, port, cookie, clients, seconds):
    result = subprocess.run([
        str(binary), "--base", f"http://127.0.0.1:{port}",
        "--path", "/searches?q=benchmark", "--cookie", cookie,
        "--accept", "text/html", "--expected-status", "200",
        "--expected-content-type", "text/html", "--expected-message-count", "100",
        "--invalid-sample", str(binary.parent / f"invalid-{port}-{clients}.html"),
        "--clients", str(clients), "--seconds", str(seconds),
    ], capture_output=True, text=True)
    assert result.returncode == 0, (result.returncode, result.stdout[-1000:], result.stderr[-2000:])
    report = json.loads(result.stdout)
    assert report["errors"] == 0 and report["successes"] > 0, report
    return report


def browser_search(port):
    session = f"search-scroll-{uuid.uuid4().hex[:8]}"
    def command(*arguments):
        result = subprocess.run(["agent-browser", "--session", session, *arguments], capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, (arguments, result.stdout, result.stderr)
        return result.stdout
    try:
        command("open", f"http://127.0.0.1:{port}/session/new")
        assert 'button "Go"' in command("snapshot", "-i")
        command("fill", 'input[name="email_address"]', "benchmark@example.invalid")
        command("fill", 'input[name="password"]', "benchmark-password")
        command("click", 'button[name="log_in"]')
        command("wait", "--url", "**/rooms/1")
        command("open", f"http://127.0.0.1:{port}/searches?q=benchmark")
        command("wait", "#search-results .message")
        command("wait", "--load", "networkidle")
        assert 'searchbox "search"' in command("snapshot", "-i")
        return json.loads(command("eval", """(() => {
          const results = document.querySelector('#search-results');
          const links = [...results.querySelectorAll('[data-reply-target="body"] a')];
          return {count: results.querySelectorAll('.message').length,
                  top: results.scrollTop, height: results.scrollHeight,
                  viewport: results.clientHeight,
                  externalTarget: links.find(link => link.href.startsWith('https://example.com/'))?.target,
                  internalTarget: links.find(link => link.href.startsWith(location.origin))?.target};
        })()"""))
    finally:
        subprocess.run(["agent-browser", "--session", session, "close"], capture_output=True, timeout=15)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--messages", type=int, default=3)
    parser.add_argument("--read-clients", type=int, nargs="*", default=[])
    parser.add_argument("--seconds", type=float, default=5.0)
    parser.add_argument("--campfire-workers", type=int, default=22)
    parser.add_argument("--rustfire-first", action="store_true")
    parser.add_argument("--browser", action="store_true", help="compare search-result scroll position in local Chromium")
    args = parser.parse_args()
    if args.messages < 1 or args.seconds <= 0 or args.campfire_workers < 1 or any(client < 1 for client in args.read_clients):
        parser.error("messages, seconds, worker count, and client counts must be positive")
    if args.read_clients and args.messages < 100:
        parser.error("concurrent search reads require at least 100 seeded messages")
    if args.browser and (args.messages < 100 or not shutil.which("agent-browser")):
        parser.error("browser search checks require at least 100 messages and the agent-browser CLI")
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPOSITORY, text=True).strip() == REVISION
    with tempfile.TemporaryDirectory(prefix="paired-search-shell-") as directory:
        temp = pathlib.Path(directory)
        rust_db, camp_db = temp / "rust.sqlite3", temp / "camp.sqlite3"
        rust_port, camp_port = free_port(), free_port()
        seed_rustfire(rust_db, rust_port, [])
        env = seed_campfire(REPOSITORY, RUBY, BUNDLE, REPOSITORY / "storage/db/production.sqlite3", camp_db, [], camp_port, temp)
        seed_messages(rust_db, camp_db, args.messages)
        if args.browser:
            with sqlite3.connect(camp_db) as camp, sqlite3.connect(rust_db) as rust:
                digest = camp.execute("SELECT password_digest FROM users WHERE id=1").fetchone()[0]
                rust.execute("UPDATE users SET email_address='benchmark@example.invalid',password_digest=? WHERE id=1", [digest])
        for database in (rust_db, camp_db):
            with sqlite3.connect(database) as db:
                db.execute("INSERT INTO searches(user_id,query,created_at,updated_at) VALUES(1,'benchmark','2026-01-02 00:00:00','2026-01-02 00:00:00')")
        rust = start_server(rust_db, rust_port, {"RUSTFIRE_CAMPFIRE_SECRET_KEY_BASE": env["SECRET_KEY_BASE"]})
        env["WEB_CONCURRENCY"] = str(args.campfire_workers if args.read_clients else 1)
        with open(temp / "puma.log", "w+") as log:
            camp = subprocess.Popen([str(RUBY), str(RUBY.parent / "bundle"), "exec", "puma", "-C", "config/puma.rb"], cwd=REPOSITORY, env=env, stdout=log, stderr=log)
            try:
                wait_for_server(camp_port, camp)
                camp_cookie, camp_csrf = login_campfire(camp_port)
                for label, path in (("empty", "/searches"), ("no matches", "/searches?q=unmatched"), ("results", "/searches?q=benchmark")):
                    source = get_room(camp_port, camp_cookie, path)
                    target = get_room(rust_port, "session_token=benchmark-session", path)
                    compare(source, target, label)
                if args.browser:
                    rich = '<div>benchmark links <a href="https://example.com/page">external</a> <a href="/rooms/1">internal</a></div>'
                    post_message(rust_port, "session_token=benchmark-session", "benchmark-csrf", rich, "search-link-fixture")
                    post_message(camp_port, camp_cookie, camp_csrf, rich, "search-link-fixture")
                    rust_view = browser_search(rust_port)
                    camp_view = browser_search(camp_port)
                    for name, view in (("rustfire", rust_view), ("campfire", camp_view)):
                        assert view["count"] == min(args.messages, 100) and view["height"] > view["viewport"], (name, view)
                        assert view["height"] - view["viewport"] - view["top"] <= 2, (name, view)
                        assert (view["externalTarget"], view["internalTarget"]) == ("_blank", "_top"), (name, view)
                    print(f"search browser scroll: Rustfire={rust_view}, Campfire={camp_view}")
                if args.read_clients:
                    binary = temp / "checked_get"
                    subprocess.run(["go", "build", "-o", str(binary), "bench/checked_get.go"], check=True)
                    targets = [
                        ("campfire", camp_port, camp_cookie),
                        ("rustfire", rust_port, "session_token=benchmark-session"),
                    ]
                    if args.rustfire_first:
                        targets.reverse()
                    for clients in args.read_clients:
                        for name, port, cookie in targets:
                            report = measure_search(binary, port, cookie, clients, args.seconds)
                            print(f"{name} clients={clients} rps={report['rps']:.1f} p95_ms={report['p95_ms']:.2f} errors={report['errors']}", flush=True)
            except Exception:
                log.flush()
                log.seek(0)
                print(log.read()[-2000:])
                raise
            finally:
                stop_server(camp)
                stop_server(rust)
    print("PASS paired search page sections")


if __name__ == "__main__":
    main()
