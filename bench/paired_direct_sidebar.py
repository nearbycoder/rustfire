"""Compare Campfire and Rustfire room sidebar, settings, and creation forms on matching fixtures.

Requires a release Rustfire build and the pinned Campfire checkout/bundle.
Uses disposable SQLite databases and an isolated Redis server.
"""

import argparse
import json
import pathlib
import re
import sqlite3
import subprocess
import tempfile
import time
import urllib.request
import urllib.error
import urllib.parse
from html.parser import HTMLParser

from direct_lookup import ROOT, free_port, start_server, stop_server
from paired_banned_content import REPOSITORY, RUBY, BUNDLE, REVISION, isolated_campfire, start_redis
from paired_direct_lookup import login_campfire, seed_campfire, seed_rustfire, wait_for_server


SECRET = "paired-direct-sidebar-secret"


def direct_link(page, room_id):
    match = re.search(rf'<a\b[^>]*\bid="list_rooms_direct_{room_id}"[^>]*>.*?</a>', page, re.S)
    if not match:
        raise AssertionError(f"Direct room {room_id} link missing from sidebar")
    return match.group(0)


def request(port, path, cookie, csrf=None, user_ids=()):
    body = None if csrf is None else urllib.parse.urlencode([("user_ids[]", value) for value in user_ids] + [("authenticity_token", csrf)]).encode()
    headers = {"Cookie": cookie}
    if csrf is not None:
        headers.update({"Content-Type": "application/x-www-form-urlencoded", "X-CSRF-Token": csrf})
    with urllib.request.urlopen(urllib.request.Request(f"http://127.0.0.1:{port}{path}", headers=headers, data=body), timeout=15) as response:
        return response.status, response.url, response.read().decode()


def delete_room_form(port, path, cookie, csrf):
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, request, file, code, message, headers, new_url):
            return None

    body = urllib.parse.urlencode({"_method": "delete", "authenticity_token": csrf}).encode()
    headers = {"Cookie": cookie, "Content-Type": "application/x-www-form-urlencoded", "X-CSRF-Token": csrf}
    request = urllib.request.Request(f"http://127.0.0.1:{port}{path}", headers=headers, data=body)
    try:
        with urllib.request.build_opener(NoRedirect).open(request, timeout=15) as response:
            return response.status, urllib.parse.urlparse(response.headers.get("Location", "")).path
    except urllib.error.HTTPError as error:
        return error.code, urllib.parse.urlparse(error.headers.get("Location", "")).path


def create_room_from_form(port, kind, cookie, csrf, name, user_ids=()):
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, request, file, code, message, headers, new_url):
            return None

    body = urllib.parse.urlencode([("room[name]", name), ("authenticity_token", csrf)] + [("user_ids[]", user_id) for user_id in user_ids]).encode()
    headers = {"Cookie": cookie, "Content-Type": "application/x-www-form-urlencoded", "X-CSRF-Token": csrf}
    request = urllib.request.Request(f"http://127.0.0.1:{port}/rooms/{kind}", headers=headers, data=body)
    try:
        with urllib.request.build_opener(NoRedirect).open(request, timeout=15) as response:
            return response.status, urllib.parse.urlparse(response.headers.get("Location", "")).path
    except urllib.error.HTTPError as error:
        return error.code, urllib.parse.urlparse(error.headers.get("Location", "")).path


def update_room_from_form(port, kind, room_id, cookie, csrf, name, user_ids=()):
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, request, file, code, message, headers, new_url):
            return None

    body = urllib.parse.urlencode([("_method", "patch"), ("room[name]", name), ("authenticity_token", csrf)] + [("user_ids[]", user_id) for user_id in user_ids]).encode()
    headers = {"Cookie": cookie, "Content-Type": "application/x-www-form-urlencoded", "X-CSRF-Token": csrf}
    request = urllib.request.Request(f"http://127.0.0.1:{port}/rooms/{kind}/{room_id}", headers=headers, data=body)
    try:
        with urllib.request.build_opener(NoRedirect).open(request, timeout=15) as response:
            return response.status, urllib.parse.urlparse(response.headers.get("Location", "")).path
    except urllib.error.HTTPError as error:
        return error.code, urllib.parse.urlparse(error.headers.get("Location", "")).path


def run(port, cookie, csrf, capture_dir, database):
    process = subprocess.Popen(
        ["node", "bench/capture_direct_sidebar.mjs", "--base", f"http://127.0.0.1:{port}", "--cookie", cookie, "--count", "4", "--output", str(capture_dir)],
        cwd=ROOT, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    try:
        ready = process.stdout.readline().strip()
        if ready != "READY":
            output, error = process.communicate(timeout=10)
            raise AssertionError(f"Direct-room capture did not start: {ready}\n{output}\n{error}")
        results = []
        placeholder_counts = []
        _, _, initial_sidebar = request(port, "/users/me/sidebar", cookie)
        _, _, new_ping_page = request(port, "/rooms/directs/new", cookie)
        new_room_pages = [request(port, f"/rooms/{kind}/new", cookie)[2] for kind in ("opens", "closeds")]
        placeholder_counts.append(count_placeholders(initial_sidebar))
        placeholder_users = [placeholder_ids(initial_sidebar)]
        placeholder_html = [placeholder_fragments(initial_sidebar)]
        sidebar_pages = [initial_sidebar]
        for members in ((2,), (2, 3), (2, 3, 4), (2, 3, 4, 5, 6)):
            status, url, _ = request(port, "/rooms/directs", cookie, csrf, members)
            assert status == 200, (status, url)
            room_id = int(url.rstrip("/").rsplit("/", 1)[1])
            assert room_id == len(results) + 2, (room_id, results)
            _, _, sidebar = request(port, "/users/me/sidebar", cookie)
            results.append(direct_link(sidebar, room_id))
            placeholder_counts.append(count_placeholders(sidebar))
            placeholder_users.append(placeholder_ids(sidebar))
            placeholder_html.append(placeholder_fragments(sidebar))
            sidebar_pages.append(sidebar)
        assert process.stdout.readline().strip() == "CREATES_READY", "Direct-room prepends were not captured"
        streams = [(capture_dir / f"{room_id}.html").read_text() for room_id in (2, 3, 4, 5)]
        edit_pages = [request(port, f"/rooms/directs/{room_id}/edit", cookie)[2] for room_id in (2, 3, 4, 5)]
        delete_result = delete_room_form(port, "/rooms/directs/2", cookie, csrf)
        output, error = process.communicate(timeout=35)
        assert process.returncode == 0, (output, error)
        received = json.loads(output.strip().splitlines()[-1])
        assert received == {"received": 4, "unexpected": 0, "ids": [2, 3, 4, 5], "deleted": True}, received
        delete_stream = (capture_dir / "delete.html").read_text()
        _, _, deleted_sidebar = request(port, "/users/me/sidebar", cookie)
        assert 'id="list_rooms_direct_2"' not in deleted_sidebar
        created_rooms = [create_room_from_form(port, "opens", cookie, csrf, "New public room"), create_room_from_form(port, "closeds", cookie, csrf, "New private room", (1, 42))]
        created_state = created_room_state(database)
        room_edit_paths = ("/rooms/opens/6/edit", "/rooms/closeds/6/edit", "/rooms/closeds/7/edit", "/rooms/opens/7/edit")
        room_edit_pages = [request(port, path, cookie)[2] for path in room_edit_paths]
        updates = subprocess.Popen(
            ["node", "bench/capture_room_updates.mjs", "--base", f"http://127.0.0.1:{port}", "--cookie", cookie, "--count", "2"],
            cwd=ROOT, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        try:
            ready = updates.stdout.readline().strip()
            if ready != "READY":
                output, error = updates.communicate(timeout=10)
                raise AssertionError(f"Shared-room update capture did not start: {ready}\n{output}\n{error}")
            renamed_rooms = [update_room_from_form(port, "opens", 6, cookie, csrf, "Renamed public room"), update_room_from_form(port, "closeds", 7, cookie, csrf, "Renamed private room", (1, 42))]
            updated_rooms = [update_room_from_form(port, "closeds", 6, cookie, csrf, "Restricted public room", (1, 42)), update_room_from_form(port, "opens", 7, cookie, csrf, "Open former private room")]
            output, error = updates.communicate(timeout=35)
            assert updates.returncode == 0, (output, error)
            update_events = json.loads(output.strip().splitlines()[-1])
        finally:
            if updates.poll() is None:
                updates.kill()
                updates.communicate()
        assert renamed_rooms == [(302, "/rooms/6"), (302, "/rooms/7")], renamed_rooms
        updated_state = updated_room_state(database)
        shared_delete = delete_room_form(port, "/rooms/6", cookie, csrf)
        with sqlite3.connect(database) as db:
            shared_delete_state = (db.execute("SELECT COUNT(*) FROM rooms WHERE id=6").fetchone()[0], db.execute("SELECT COUNT(*) FROM memberships WHERE room_id=6").fetchone()[0])
        return results, streams, placeholder_counts, placeholder_users, placeholder_html, sidebar_pages, new_ping_page, edit_pages, delete_result, delete_stream, deleted_sidebar, new_room_pages, created_rooms, created_state, room_edit_pages, update_events, updated_rooms, updated_state, shared_delete, shared_delete_state
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()


def normalized(html):
    epoch = re.search(r'data-sorted-list-number="(\d+)"', html)
    assert epoch and abs(int(epoch.group(1)) - int(time.time() * 1000)) < 30_000, "Direct room sort time is not a current epoch millisecond value"
    return re.sub(r'data-sorted-list-number="\d+"', 'data-sorted-list-number="<epoch-ms>"', html)


def count_placeholders(page):
    return len(re.findall(r'class="direct borderless fill-transparent unpad"', page))


def placeholder_ids(page):
    return [int(value) for value in re.findall(r'/rooms/directs\?user_ids%5B%5D=(\d+)', page)]


def placeholder_fragments(page):
    return re.findall(r'<form class="button_to" method="post" action="/rooms/directs\?user_ids%5B%5D=\d+".*?</form>', page, re.S)


def normalized_placeholders(groups):
    return [[re.sub(r'(name="authenticity_token" value=")[^"]+', r'\1<csrf>', fragment) for fragment in group] for group in groups]


def remaining_direct_rooms(database):
    with sqlite3.connect(database) as db:
        rooms = [row[0] for row in db.execute("SELECT id FROM rooms WHERE type='Rooms::Direct' ORDER BY id")]
        deleted_memberships = db.execute("SELECT COUNT(*) FROM memberships WHERE room_id=2").fetchone()[0]
    return rooms, deleted_memberships


def created_room_state(database):
    with sqlite3.connect(database) as db:
        return [(name, kind, [row[0] for row in db.execute("SELECT user_id FROM memberships WHERE room_id=? ORDER BY user_id", (rid,))]) for rid, name, kind in db.execute("SELECT id,name,type FROM rooms WHERE name IN ('New public room','New private room') ORDER BY id").fetchall()]


def updated_room_state(database):
    with sqlite3.connect(database) as db:
        return [(rid, name, kind, [row[0] for row in db.execute("SELECT user_id FROM memberships WHERE room_id=? ORDER BY user_id", (rid,))]) for rid, name, kind in db.execute("SELECT id,name,type FROM rooms WHERE id IN (6,7) ORDER BY id").fetchall()]


class SidebarTree(HTMLParser):
    def __init__(self, frame_id=None, target_class=None):
        super().__init__()
        self.frame_id = frame_id
        self.target_class = target_class
        self.active = False
        self.depth = 0
        self.events = []

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if not self.active:
            matches_frame = self.frame_id is not None and tag == "turbo-frame" and values.get("id") == self.frame_id
            matches_class = self.target_class is not None and self.target_class in values.get("class", "").split()
            if not (matches_frame or matches_class):
                return
            self.active = True
            self.depth = 1
        elif tag not in ("img", "input", "link", "meta", "br", "hr", "source"):
            self.depth += 1
        if values.get("name") == "authenticity_token":
            values["value"] = "<csrf>"
        if "data-sorted-list-number" in values:
            values["data-sorted-list-number"] = "<epoch-ms>"
        if values.get("action", "").startswith("http://127.0.0.1:"):
            values["action"] = re.sub(r"^http://127\.0\.0\.1:\d+", "", values["action"])
        self.events.append(("start", tag, tuple(sorted(values.items()))))

    def handle_endtag(self, tag):
        if self.active and tag not in ("img", "input", "link", "meta", "br", "hr", "source"):
            self.events.append(("end", tag))
            self.depth -= 1
            if self.depth == 0:
                self.active = False

    def handle_data(self, value):
        if self.active and value.strip():
            self.events.append(("text", " ".join(value.split())))


def frame_tree(page, frame_id):
    parser = SidebarTree(frame_id)
    parser.feed(page)
    assert parser.events and parser.depth == 0, f"{frame_id} frame missing or unclosed"
    return parser.events


def panel_tree(page):
    parser = SidebarTree(target_class="panel")
    parser.feed(page)
    assert parser.events and parser.depth == 0, "Direct settings panel missing or unclosed"
    return parser.events


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sample-dir", type=pathlib.Path)
    args = parser.parse_args()
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPOSITORY, text=True).strip()
    assert revision == REVISION, revision
    with tempfile.TemporaryDirectory(prefix="paired-direct-sidebar-") as temporary:
        temp = pathlib.Path(temporary)
        rust_db, camp_db = temp / "rust.sqlite3", temp / "camp.sqlite3"
        rust_port, camp_port, redis_port = free_port(), free_port(), free_port()
        checkout = isolated_campfire(temp, redis_port)
        seed_rustfire(rust_db, rust_port, [])
        env = seed_campfire(checkout, RUBY, BUNDLE, REPOSITORY / "storage/db/production.sqlite3", camp_db, [], camp_port, temp)
        env["REDIS_URL"] = f"redis://127.0.0.1:{redis_port}"
        env["SECRET_KEY_BASE"] = SECRET
        with sqlite3.connect(camp_db) as db:
            db.execute("UPDATE users SET name='User '||id,created_at='2026-01-01 00:00:00.000000',updated_at='2026-01-01 00:00:00.000000' WHERE id IN (1,2)")
            db.execute("UPDATE rooms SET name='Campfire' WHERE id=1")

        rust = start_server(rust_db, rust_port, {"RUSTFIRE_CAMPFIRE_SECRET_KEY_BASE": SECRET})
        try:
            rust_links, rust_streams, rust_placeholders, rust_placeholder_users, rust_placeholder_html, rust_sidebars, rust_new_ping, rust_edit_pages, rust_delete, rust_delete_stream, rust_deleted_sidebar, rust_new_room_pages, rust_created_rooms, rust_created_state, rust_room_edit_pages, rust_update_events, rust_updated_rooms, rust_updated_state, rust_shared_delete, rust_shared_delete_state = run(rust_port, "session_token=benchmark-session", "benchmark-csrf", temp / "rust-streams", rust_db)
        finally:
            stop_server(rust)

        redis, redis_log = start_redis(temp, redis_port)
        log = open(temp / "puma.log", "w+")
        camp = None
        try:
            camp = subprocess.Popen([str(RUBY), str(RUBY.parent / "bundle"), "exec", "puma", "-C", "config/puma.rb"], cwd=checkout, env=env, stdout=log, stderr=log)
            wait_for_server(camp_port, camp)
            cookie, csrf = login_campfire(camp_port)
            camp_links, camp_streams, camp_placeholders, camp_placeholder_users, camp_placeholder_html, camp_sidebars, camp_new_ping, camp_edit_pages, camp_delete, camp_delete_stream, camp_deleted_sidebar, camp_new_room_pages, camp_created_rooms, camp_created_state, camp_room_edit_pages, camp_update_events, camp_updated_rooms, camp_updated_state, camp_shared_delete, camp_shared_delete_state = run(camp_port, cookie, csrf, temp / "camp-streams", camp_db)
        finally:
            if camp is not None:
                stop_server(camp)
            redis.terminate()
            redis.wait(timeout=10)
            redis_log.close()
            log.close()

        if args.sample_dir:
            args.sample_dir.mkdir(parents=True, exist_ok=True)
            for index, (rust_link, camp_link) in enumerate(zip(rust_links, camp_links), 2):
                (args.sample_dir / f"rustfire-{index}.html").write_text(rust_link)
                (args.sample_dir / f"campfire-{index}.html").write_text(camp_link)
                (args.sample_dir / f"rustfire-stream-{index}.html").write_text(rust_streams[index - 2])
                (args.sample_dir / f"campfire-stream-{index}.html").write_text(camp_streams[index - 2])
            for index, (rust_sidebar, camp_sidebar) in enumerate(zip(rust_sidebars, camp_sidebars)):
                (args.sample_dir / f"rustfire-sidebar-{index}.html").write_text(rust_sidebar)
                (args.sample_dir / f"campfire-sidebar-{index}.html").write_text(camp_sidebar)
            (args.sample_dir / "rustfire-new-ping.html").write_text(rust_new_ping)
            (args.sample_dir / "campfire-new-ping.html").write_text(camp_new_ping)
            for room_id, (rust_edit, camp_edit) in enumerate(zip(rust_edit_pages, camp_edit_pages), 2):
                (args.sample_dir / f"rustfire-edit-ping-{room_id}.html").write_text(rust_edit)
                (args.sample_dir / f"campfire-edit-ping-{room_id}.html").write_text(camp_edit)
            (args.sample_dir / "rustfire-delete-ping.html").write_text(rust_delete_stream)
            (args.sample_dir / "campfire-delete-ping.html").write_text(camp_delete_stream)
            (args.sample_dir / "rustfire-deleted-sidebar.html").write_text(rust_deleted_sidebar)
            (args.sample_dir / "campfire-deleted-sidebar.html").write_text(camp_deleted_sidebar)
            for kind, rust_page, camp_page in zip(("opens", "closeds"), rust_new_room_pages, camp_new_room_pages):
                (args.sample_dir / f"rustfire-new-{kind}.html").write_text(rust_page)
                (args.sample_dir / f"campfire-new-{kind}.html").write_text(camp_page)
            for label, rust_page, camp_page in zip(("open-6", "closed-6", "closed-7", "open-7"), rust_room_edit_pages, camp_room_edit_pages):
                (args.sample_dir / f"rustfire-edit-{label}.html").write_text(rust_page)
                (args.sample_dir / f"campfire-edit-{label}.html").write_text(camp_page)
            for stream in ("global", "user"):
                for index, event in enumerate(rust_update_events.get(stream, []), 1):
                    (args.sample_dir / f"rustfire-update-{stream}-{index}.html").write_text(event)
                for index, event in enumerate(camp_update_events.get(stream, []), 1):
                    (args.sample_dir / f"campfire-update-{stream}-{index}.html").write_text(event)
        for index, (rust_link, camp_link) in enumerate(zip(rust_links, camp_links), 2):
            assert normalized(rust_link) == normalized(camp_link), f"Direct room {index} markup differs; use --sample-dir to inspect"
            assert normalized(rust_streams[index - 2]) == normalized(camp_streams[index - 2]), f"Direct room {index} Turbo event differs; use --sample-dir to inspect"
        assert rust_placeholders == camp_placeholders == [19, 17, 16, 15, 13], (rust_placeholders, camp_placeholders)
        assert rust_placeholder_users == camp_placeholder_users, (rust_placeholder_users, camp_placeholder_users)
        assert [len(group) for group in rust_placeholder_html] == rust_placeholders
        assert [len(group) for group in camp_placeholder_html] == camp_placeholders
        assert normalized_placeholders(rust_placeholder_html) == normalized_placeholders(camp_placeholder_html), "Direct placeholder markup differs; use --sample-dir to inspect"
        for index, (rust_sidebar, camp_sidebar) in enumerate(zip(rust_sidebars, camp_sidebars)):
            assert frame_tree(rust_sidebar, "user_sidebar") == frame_tree(camp_sidebar, "user_sidebar"), f"Sidebar frame differs after {index} direct rooms; use --sample-dir to inspect"
        assert frame_tree(rust_new_ping, "direct_rooms_control") == frame_tree(camp_new_ping, "direct_rooms_control"), "New-ping frame differs; use --sample-dir to inspect"
        for room_id, (rust_edit, camp_edit) in enumerate(zip(rust_edit_pages, camp_edit_pages), 2):
            assert panel_tree(rust_edit) == panel_tree(camp_edit), f"Direct settings panel differs for room {room_id}; use --sample-dir to inspect"
        assert rust_delete == camp_delete == (302, "/"), (rust_delete, camp_delete)
        assert rust_delete_stream == camp_delete_stream, (rust_delete_stream, camp_delete_stream)
        assert frame_tree(rust_deleted_sidebar, "user_sidebar") == frame_tree(camp_deleted_sidebar, "user_sidebar"), "Sidebar differs after direct-room deletion; use --sample-dir to inspect"
        assert remaining_direct_rooms(rust_db) == remaining_direct_rooms(camp_db) == ([3, 4, 5], 0)
        with sqlite3.connect(rust_db) as db:
            assert db.execute("SELECT COUNT(*) FROM direct_room_sets WHERE room_id=2").fetchone()[0] == 0
        for kind, rust_page, camp_page in zip(("opens", "closeds"), rust_new_room_pages, camp_new_room_pages):
            assert panel_tree(rust_page) == panel_tree(camp_page), f"New {kind} room panel differs; use --sample-dir to inspect"
        for label, rust_page, camp_page in zip(("open-6", "closed-6", "closed-7", "open-7"), rust_room_edit_pages, camp_room_edit_pages):
            assert panel_tree(rust_page) == panel_tree(camp_page), f"Edit room panels differ for {label}; use --sample-dir to inspect"
        assert rust_created_rooms == camp_created_rooms == [(302, "/rooms/6"), (302, "/rooms/7")], (rust_created_rooms, camp_created_rooms)
        expected_created = [("New public room", "Rooms::Open", list(range(1, 52))), ("New private room", "Rooms::Closed", [1, 42])]
        assert rust_created_state == camp_created_state == expected_created
        assert rust_update_events == camp_update_events, (rust_update_events, camp_update_events)
        assert set(rust_update_events) == {"global", "user"}, rust_update_events
        assert [len(rust_update_events[stream]) for stream in ("global", "user")] == [2, 2], rust_update_events
        assert rust_update_events["global"][0].startswith('<turbo-stream action="replace" target="list_rooms_open_6"><template>')
        assert rust_update_events["user"][0].startswith('<turbo-stream action="replace" target="list_rooms_closed_7"><template>')
        assert rust_update_events["user"][1].startswith('<turbo-stream action="replace" target="list_rooms_closed_6"><template>')
        assert rust_update_events["global"][1].startswith('<turbo-stream action="replace" target="list_rooms_open_7"><template>')
        assert rust_updated_rooms == camp_updated_rooms == [(302, "/rooms/6"), (302, "/rooms/7")], (rust_updated_rooms, camp_updated_rooms)
        expected_updated = [(6, "Restricted public room", "Rooms::Closed", [1, 42]), (7, "Open former private room", "Rooms::Open", list(range(1, 52)))]
        assert rust_updated_state == camp_updated_state == expected_updated, (rust_updated_state, camp_updated_state)
        assert rust_shared_delete == camp_shared_delete == (302, "/"), (rust_shared_delete, camp_shared_delete)
        assert rust_shared_delete_state == camp_shared_delete_state == (0, 0), (rust_shared_delete_state, camp_shared_delete_state)
        print("PASS paired direct links, create/delete/update Turbo events, 80 shortcut forms, six sidebar frames, new-ping frame, four direct settings panels, delete form, two new-room panels, four edit-room panel pairs, and open/private room create/convert/delete flows; only room epoch milliseconds, form CSRF tokens, and origins normalized")


if __name__ == "__main__":
    main()
