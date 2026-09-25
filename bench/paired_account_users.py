"""Compare account user administration with the pinned Campfire checkout.

Run after ``cargo build --release`` with the pinned Ruby bundle and Redis.
"""

import argparse
import base64
import concurrent.futures
import hashlib
import html
from html.parser import HTMLParser
import http.client
import pathlib
import re
import sqlite3
import statistics
import subprocess
import tempfile
import threading
import time
import urllib.parse

from direct_lookup import free_port, p95, start_server, stop_server
from paired_bot_admin import request
from paired_direct_lookup import login_campfire, seed_campfire, seed_rustfire, wait_for_server


def role_and_status(database):
    with sqlite3.connect(database) as db:
        return db.execute("SELECT role,status FROM users WHERE id=2").fetchone()


def membership_state(database):
    with sqlite3.connect(database) as db:
        return db.execute("SELECT room_id FROM memberships WHERE user_id=2 ORDER BY room_id").fetchall()


def seed_large_fixture(database, users):
    if users <= 51:
        return
    stamp = "2026-01-01T00:00:00Z"
    with sqlite3.connect(database) as db:
        db.execute("UPDATE users SET name=printf('User %04d',id),updated_at=?1 WHERE id<=51", [stamp])
        db.executemany(
            "INSERT INTO users(id,name,role,status,created_at,updated_at) VALUES(?1,?2,0,0,?3,?3)",
            ((uid, f"User {uid:04d}", stamp) for uid in range(52, users + 1)),
        )


def page_summary(body):
    html = body.decode()
    ids = tuple(int(uid) for uid in re.findall(r"href=['\"]/users/(\d+)['\"]", html))
    next_frame = re.search(r"src=['\"]/account/users\.turbo_stream\?page=(\d+)['\"]", html)
    return ids, html.count('action="replace" target="next_page_container"'), html.count('action="append" target="account_users"'), int(next_frame.group(1)) if next_frame else None


class Markup(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.events = []

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == "input" and values.get("name") == "authenticity_token":
            values["value"] = "CSRF"
        self.events.append(("start", tag, tuple(sorted(values.items()))))

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        self.events.append(("end", tag))

    def handle_data(self, data):
        if data.strip():
            self.events.append(("text", data.strip()))


def markup_events(body):
    parser = Markup()
    parser.feed(body.decode())
    return parser.events


def settings_summary(page):
    panel = page.split('view-transition-name: account-settings', 1)[1].split('<turbo-frame id=', 1)[0]
    forms = re.findall(r'<form\b([^>]*)>(.*?)</form>', panel, re.S)
    account_forms = []
    for attributes, content in forms:
        action = re.search(r'action=[\'"]([^\'"]+)', attributes)
        if action and action.group(1) == '/account.1':
            method = re.search(r'name=[\'"]_method[\'"] value=[\'"]([^\'"]+)', content)
            account_forms.append((method.group(1) if method else None,
                                  tuple(re.findall(r'name=[\'"](account\[[^\'"]+)', content))))
    invite = re.search(r'id=[\'"]invite_url[\'"][^>]*value=[\'"]([^\'"]+)', panel)
    qr = re.search(r'href=[\'"](/qr_code/[^\'"]+)', panel)
    copy = re.search(r'data-copy-to-clipboard-content-value=[\'"]([^\'"]+)', panel)
    assert invite and qr and copy, 'Invite controls are missing'
    invite_url = html.unescape(invite.group(1))
    decoded_qr = base64.urlsafe_b64decode(qr.group(1).rsplit('/', 1)[1] + '===').decode()
    return {
        'account_forms': tuple(account_forms),
        'invite_matches_qr': decoded_qr == invite_url,
        'invite_matches_copy': html.unescape(copy.group(1)) == invite_url,
        'invite_path': urllib.parse.urlsplit(invite_url).path.startswith('/join/'),
        'regenerate': 'action="/account/join_code"' in panel or "action='/account/join_code'" in panel,
        'room_switch': 'class="switch__input"' in panel or "class='switch__input'" in panel,
    }


def measure_settings_page(port, cookie, clients, seconds):
    ready = threading.Barrier(clients + 1, timeout=30)
    start = threading.Event()
    clock = {}

    def worker():
        connection = http.client.HTTPConnection('127.0.0.1', port, timeout=15)
        samples, errors, sizes = [], 0, set()
        try:
            for _ in range(2):
                connection.request('GET', '/account/edit', headers={'Cookie': cookie})
                response = connection.getresponse()
                body = response.read()
                if response.status != 200 or not settings_summary(body.decode())['invite_matches_qr']:
                    raise AssertionError('Settings page warmup failed')
            ready.wait()
            start.wait()
            while time.perf_counter() < clock['deadline']:
                begun = time.perf_counter()
                try:
                    connection.request('GET', '/account/edit', headers={'Cookie': cookie})
                    response = connection.getresponse()
                    body = response.read()
                    if response.status == 200 and b'id="invite_url"' in body and b'id="account_users"' in body:
                        samples.append((time.perf_counter() - begun) * 1000)
                        sizes.add(len(body))
                    else:
                        errors += 1
                except (OSError, ValueError):
                    errors += 1
                    connection.close()
                    connection = http.client.HTTPConnection('127.0.0.1', port, timeout=15)
        finally:
            connection.close()
        return samples, errors, sizes, time.perf_counter()

    with concurrent.futures.ThreadPoolExecutor(max_workers=clients) as executor:
        futures = [executor.submit(worker) for _ in range(clients)]
        ready.wait()
        clock['begun'] = time.perf_counter()
        clock['deadline'] = clock['begun'] + seconds
        start.set()
        workers = [future.result() for future in futures]
    samples = [sample for worker_samples, _, _, _ in workers for sample in worker_samples]
    errors = sum(worker_errors for _, worker_errors, _, _ in workers)
    sizes = set().union(*(worker_sizes for _, _, worker_sizes, _ in workers))
    elapsed = max(ended for _, _, _, ended in workers) - clock['begun']
    assert samples and errors == 0, (len(samples), errors)
    return len(samples) / elapsed, statistics.median(samples), p95(samples), len(samples), errors, sorted(sizes)


def measure_page(port, cookie, clients, seconds, expected_summary, expected_markup):
    ready = threading.Barrier(clients + 1, timeout=30)
    start = threading.Event()
    clock = {}
    ids = expected_summary[0]
    first = f'href="/users/{ids[0]}"'.encode()
    last = f'href="/users/{ids[-1]}"'.encode()

    def worker():
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
        samples = []
        errors = 0
        sizes = set()
        try:
            for _ in range(2):
                connection.request("GET", "/account/users.turbo_stream?page=2", headers={"Cookie": cookie, "Accept": "text/vnd.turbo-stream.html"})
                response = connection.getresponse()
                body = response.read()
                if response.status != 200 or page_summary(body) != expected_summary or markup_events(body) != expected_markup:
                    raise AssertionError("Page warmup differs from the paired markup")
            ready.wait()
            start.wait()
            while time.perf_counter() < clock["deadline"]:
                begun = time.perf_counter()
                try:
                    connection.request("GET", "/account/users.turbo_stream?page=2", headers={"Cookie": cookie, "Accept": "text/vnd.turbo-stream.html"})
                    response = connection.getresponse()
                    body = response.read()
                    if response.status == 200 and body.count(b'<li class=') == len(ids) and first in body and last in body:
                        samples.append((time.perf_counter() - begun) * 1000)
                        sizes.add(len(body))
                    else:
                        errors += 1
                except (OSError, ValueError):
                    errors += 1
                    connection.close()
                    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
        finally:
            connection.close()
        return samples, errors, sizes, time.perf_counter()

    with concurrent.futures.ThreadPoolExecutor(max_workers=clients) as executor:
        futures = [executor.submit(worker) for _ in range(clients)]
        ready.wait()
        clock["begun"] = time.perf_counter()
        clock["deadline"] = clock["begun"] + seconds
        start.set()
        workers = [future.result() for future in futures]
    samples = [sample for worker_samples, _, _, _ in workers for sample in worker_samples]
    errors = sum(worker_errors for _, worker_errors, _, _ in workers)
    sizes = set().union(*(worker_sizes for _, _, worker_sizes, _ in workers))
    elapsed = max(ended for _, _, _, ended in workers) - clock["begun"]
    assert samples and errors == 0, (len(samples), errors)
    return len(samples) / elapsed, statistics.median(samples), p95(samples), len(samples), errors, sorted(sizes)


def workflow(port, cookie, csrf, database, users, clients, seconds):
    result = {}
    performance = {}
    status, _, body = request(port, "GET", "/account/edit", cookie, csrf)
    assert status == 200
    page = body.decode()
    result['settings_controls'] = settings_summary(page)
    frame = re.search(r"<turbo-frame\b[^>]*\bid=['\"]account_users['\"][^>]*>(.*?)</turbo-frame>", page, re.S)
    assert frame, "Account user frame is absent"
    frame = frame.group(1)
    with sqlite3.connect(database) as db:
        admin = html.escape(db.execute("SELECT name FROM users WHERE id=1").fetchone()[0])
        member = html.escape(db.execute("SELECT name FROM users WHERE id=2").fetchone()[0])
    divider = re.search(r"<hr\s+class=['\"]separator full-width['\"]", frame)
    assert divider and frame.index(f"<strong>{admin}</strong>") < divider.start() < frame.index(f"<strong>{member}</strong>")
    result["roster_grouping"] = True
    if users > 500:
        result["initial_roster"] = page_summary(body)
        initial_frame = re.search(r"<turbo-frame\s+id=['\"]account_users['\"]>(.*?)</turbo-frame>", page, re.S)
        assert initial_frame
        result["initial_markup"] = markup_events(initial_frame.group(1).encode())
        for number in (1, 2, 3, 4):
            status, _, payload = request(port, "GET", f"/account/users.turbo_stream?page={number}", cookie, csrf)
            assert status == 200, (number, status, payload[:300])
            result[f"page_{number}"] = page_summary(payload)
            result[f"page_{number}_markup"] = markup_events(payload)
        for count in clients:
            performance[count] = measure_page(port, cookie, count, seconds, result["page_2"], result["page_2_markup"])
            performance[f'settings_{count}'] = measure_settings_page(port, cookie, count, seconds)

    for key, role in (("promote", "administrator"), ("invalid_role", "invalid")):
        body = urllib.parse.urlencode({"user[role]": role}).encode()
        status, location, payload = request(port, "PUT", "/account/users/2", cookie, csrf, body, "application/x-www-form-urlencoded")
        assert status == 302, (key, status, payload[:300])
        result[key] = status, urllib.parse.urlsplit(location).path, role_and_status(database)

    status, location, payload = request(port, "DELETE", "/account/users/2", cookie, csrf)
    assert status == 302, (status, payload[:300])
    result["deactivate"] = status, urllib.parse.urlsplit(location).path, role_and_status(database), membership_state(database)
    status, _, _ = request(port, "DELETE", "/account/users/2", cookie, csrf)
    result["repeat_delete"] = status
    return result, performance


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--campfire-repo", type=pathlib.Path, default=pathlib.Path("/tmp/once-campfire-reference"))
    parser.add_argument("--ruby", type=pathlib.Path, default=pathlib.Path("/tmp/rustfire-baseline/local/bin/ruby"))
    parser.add_argument("--bundle-path", type=pathlib.Path, default=pathlib.Path("/tmp/rustfire-baseline/bundle"))
    parser.add_argument("--users", type=int, default=51, help="use 1100 or more to probe account user pages")
    parser.add_argument("--clients", type=int, nargs="*", default=[], help="concurrent clients for page-2 reads")
    parser.add_argument("--seconds", type=float, default=3.0, help="duration of each concurrent trial")
    parser.add_argument("--campfire-workers", type=int, default=1, help="Puma workers; packaged default here is 22")
    args = parser.parse_args()
    if args.users < 51:
        parser.error("users must be at least 51")
    if any(count < 1 for count in args.clients) or args.seconds <= 0 or args.campfire_workers < 1:
        parser.error("client counts, duration, and Puma workers must be positive")
    if args.clients and args.users <= 500:
        parser.error("concurrent page-2 reads require more than 500 users")
    repository, ruby, bundle_path = args.campfire_repo.resolve(), args.ruby.resolve(), args.bundle_path.resolve()
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repository, text=True).strip()
    assert revision == "91d294f4a09f9bbe37f9548959bfcb43645678fb", revision
    with tempfile.TemporaryDirectory(prefix="paired-account-users-") as temporary:
        temp = pathlib.Path(temporary)
        rust_db, camp_db = temp / "rustfire.sqlite3", temp / "campfire.sqlite3"
        rust_port, camp_port = free_port(), free_port()
        direct_rooms = [(2,)]
        seed_rustfire(rust_db, rust_port, direct_rooms)
        env = seed_campfire(repository, ruby, bundle_path, repository / "storage/db/production.sqlite3", camp_db, direct_rooms, camp_port, temp)
        seed_large_fixture(rust_db, args.users)
        seed_large_fixture(camp_db, args.users)
        rust_process = start_server(rust_db, rust_port, {"RUSTFIRE_CAMPFIRE_SECRET_KEY_BASE": env["SECRET_KEY_BASE"]})
        try:
            rust, rust_performance = workflow(rust_port, "session_token=benchmark-session", "benchmark-csrf", rust_db, args.users, args.clients, args.seconds)
        finally:
            stop_server(rust_process)
        log = open(temp / "puma.log", "w+")
        env["WEB_CONCURRENCY"] = str(args.campfire_workers)
        camp_process = subprocess.Popen([str(ruby), str(ruby.parent / "bundle"), "exec", "puma", "-C", "config/puma.rb"], cwd=repository, env=env, stdout=log, stderr=log)
        try:
            wait_for_server(camp_port, camp_process)
            cookie, csrf = login_campfire(camp_port)
            camp, camp_performance = workflow(camp_port, cookie, csrf, camp_db, args.users, args.clients, args.seconds)
        except Exception:
            log.flush()
            log.seek(0)
            print(log.read()[-3000:])
            raise
        finally:
            stop_server(camp_process)
            log.close()
        for key in rust:
            if rust[key] != camp[key]:
                if key.endswith("_markup"):
                    mismatch = next((index for index, pair in enumerate(zip(rust[key], camp[key])) if pair[0] != pair[1]), min(len(rust[key]), len(camp[key])))
                    raise AssertionError((key, mismatch, rust[key][mismatch:mismatch + 3], camp[key][mismatch:mismatch + 3]))
                raise AssertionError((key, rust[key], camp[key]))
            if key.endswith("_markup"):
                digest = hashlib.sha256(repr(rust[key]).encode()).hexdigest()
                print(f"{key}: events={len(rust[key])} sha256={digest} equal=True")
            elif key.startswith("page_") or key == "initial_roster":
                ids, replaced, appended, next_page = rust[key]
                print(f"{key}: users={len(ids)} first={ids[0] if ids else None} last={ids[-1] if ids else None} replace={replaced} append={appended} next={next_page}")
            else:
                print(f"{key}: rustfire={rust[key]!r} campfire={camp[key]!r}")
        for count in args.clients:
            rust_rate, rust_median, rust_p95, rust_success, rust_errors, rust_sizes = rust_performance[count]
            camp_rate, camp_median, camp_p95, camp_success, camp_errors, camp_sizes = camp_performance[count]
            print(f"clients={count} seconds={args.seconds:g} campfire_workers={args.campfire_workers} rustfire_rps={rust_rate:.1f} rustfire_median_ms={rust_median:.3f} rustfire_p95_ms={rust_p95:.3f} rustfire_successes={rust_success} rustfire_errors={rust_errors} rustfire_bytes={rust_sizes} campfire_rps={camp_rate:.1f} campfire_median_ms={camp_median:.3f} campfire_p95_ms={camp_p95:.3f} campfire_successes={camp_success} campfire_errors={camp_errors} campfire_bytes={camp_sizes}")
            rust_rate, rust_median, rust_p95, rust_success, rust_errors, rust_sizes = rust_performance[f'settings_{count}']
            camp_rate, camp_median, camp_p95, camp_success, camp_errors, camp_sizes = camp_performance[f'settings_{count}']
            print(f"account_edit clients={count} seconds={args.seconds:g} campfire_workers={args.campfire_workers} rustfire_rps={rust_rate:.1f} rustfire_median_ms={rust_median:.3f} rustfire_p95_ms={rust_p95:.3f} rustfire_successes={rust_success} rustfire_errors={rust_errors} rustfire_bytes={rust_sizes} campfire_rps={camp_rate:.1f} campfire_median_ms={camp_median:.3f} campfire_p95_ms={camp_p95:.3f} campfire_successes={camp_success} campfire_errors={camp_errors} campfire_bytes={camp_sizes}")


if __name__ == "__main__":
    main()
