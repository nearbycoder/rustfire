"""Compare parsed user-profile panels with pinned Campfire on matched fixtures.

Run after ``cargo build --release``. This checks active, banned, deactivated,
and bot profile views, plus the browser's form-based unban request.
"""

from html.parser import HTMLParser
import pathlib
import re
import sqlite3
import subprocess
import tempfile
import urllib.parse

from direct_lookup import free_port, start_server, stop_server
from paired_bot_admin import request
from paired_direct_lookup import login_campfire, seed_campfire, seed_rustfire, wait_for_server
from paired_room_shell import post_account_logo, section


REPOSITORY = pathlib.Path("/tmp/once-campfire-reference")
RUBY = pathlib.Path("/tmp/rustfire-baseline/local/bin/ruby")
BUNDLE = pathlib.Path("/tmp/rustfire-baseline/bundle")
REVISION = "91d294f4a09f9bbe37f9548959bfcb43645678fb"
STAMP = "2026-01-01T00:00:00Z"
VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}


class ProfilePanel(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.depth = 0
        self.tokens = []

    def handle_starttag(self, tag, attrs):
        if tag == "section" and ("class", "panel txt-align-center") in attrs:
            self.depth = 1
        elif self.depth and tag not in VOID:
            self.depth += 1
        if self.depth:
            values = []
            for key, value in attrs:
                if key == "src" and tag == "img" and value and "/avatar" in value:
                    value = "<signed-avatar>"
                elif key == "value" and ("name", "authenticity_token") in attrs:
                    value = "<csrf>"
                elif key in {"value", "data-copy-to-clipboard-content-value", "data-web-share-url-value"} and value and "/session/transfers/" in value:
                    value = "<transfer-url>"
                elif key in {"href", "data-lightbox-url-value"} and value and value.startswith("/qr_code/"):
                    value = "<qr-url>"
                values.append((key, value))
            self.tokens.append(("start", tag, tuple(sorted(values))))

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        if self.depth:
            self.tokens.append(("end", tag))
            if tag not in VOID:
                self.depth -= 1

    def handle_data(self, data):
        if self.depth and data.strip():
            self.tokens.append(("text", " ".join(data.split())))


class ProfileAvatar(HTMLParser):
    def __init__(self):
        super().__init__()
        self.path = None

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == "img" and values.get("alt") == "Profile avatar":
            self.path = values.get("src")


def avatar_path(page):
    parser = ProfileAvatar()
    parser.feed(page.decode())
    assert parser.path and parser.path.startswith("/users/") and "/avatar" in parser.path
    return parser.path


def seed_profile(database):
    with sqlite3.connect(database) as db:
        db.execute(
            "UPDATE users SET name='Profile Admin',email_address='benchmark@example.invalid',"
            "bio='Admin bio',updated_at=? WHERE id=1", (STAMP,)
        )
        db.execute(
            "UPDATE users SET name='Profile Member',email_address='profile@example.test',"
            "bio='Profile bio',updated_at=? WHERE id=2", (STAMP,)
        )


def profile(port, cookie, csrf, user_id=2):
    status, _, body = request(port, "GET", f"/users/{user_id}", cookie, csrf)
    assert status == 200, (status, body[:200])
    panel = ProfilePanel()
    panel.feed(body.decode())
    assert panel.tokens and panel.tokens[0][:2] == ("start", "section")
    return body, panel.tokens


def compare_profile(label, rust_page, camp_page):
    rust_body, rust_panel = rust_page
    camp_body, camp_panel = camp_page
    if rust_panel != camp_panel:
        for index, (left, right) in enumerate(zip(rust_panel, camp_panel)):
            if left != right:
                raise AssertionError(f"{label} panel token {index}: rustfire={left!r} campfire={right!r}")
        raise AssertionError(f"{label}: different panel token counts {len(rust_panel)} != {len(camp_panel)}")
    print(f"{label} panel: {len(rust_panel)} matching parsed tokens")
    for part in ("nav", "footer", "sidebar"):
        expected = section(camp_body, part)
        actual = section(rust_body, part)
        for index, (left, right) in enumerate(zip(expected, actual)):
            assert left == right, (label, part, index, left, right)
        assert len(expected) == len(actual), (label, part, len(expected), len(actual))
        print(f"{label} {part}: {len(actual)} matching parsed tokens")
    rust_classes = re.search(rb'<body class="([^"]*)"', rust_body).group(1)
    camp_classes = re.search(rb'<body class="([^"]*)"', camp_body).group(1)
    assert rust_classes == camp_classes, (label, rust_classes, camp_classes)


def set_state(database, role, status):
    with sqlite3.connect(database) as db:
        db.execute("UPDATE users SET role=?,status=? WHERE id=2", (role, status))


def main():
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPOSITORY, text=True).strip()
    assert revision == REVISION, revision
    with tempfile.TemporaryDirectory(prefix="paired-profiles-") as temporary:
        temp = pathlib.Path(temporary)
        rust_db, camp_db = temp / "rust.sqlite3", temp / "camp.sqlite3"
        rust_port, camp_port = free_port(), free_port()
        seed_rustfire(rust_db, rust_port, [])
        env = seed_campfire(REPOSITORY, RUBY, BUNDLE, REPOSITORY / "storage/db/production.sqlite3", camp_db, [], camp_port, temp)
        seed_profile(rust_db)
        seed_profile(camp_db)
        with sqlite3.connect(camp_db) as db:
            db.execute("UPDATE users SET password_digest=(SELECT password_digest FROM users WHERE id=1) WHERE id=2")
        with sqlite3.connect(rust_db) as db:
            db.execute("INSERT INTO sessions(user_id,token,csrf_token,created_at,last_active_at) VALUES(2,'member-session','member-csrf',?1,?1)", [STAMP])
        rust_process = start_server(rust_db, rust_port)
        log = open(temp / "puma.log", "w+")
        camp_process = subprocess.Popen(
            [str(RUBY), str(RUBY.parent / "bundle"), "exec", "puma", "-C", "config/puma.rb"],
            cwd=REPOSITORY, env=env, stdout=log, stderr=log,
        )
        try:
            wait_for_server(camp_port, camp_process)
            camp_cookie, camp_csrf = login_campfire(camp_port)
            for path in ("/users/me/avatar", "/users/1/avatar", "/users/not-a-valid-token/avatar"):
                for rust_cookie, source_cookie in (("session_token=benchmark-session", camp_cookie), ("", "")):
                    actual_status, actual_location, _ = request(rust_port, "GET", path, rust_cookie, "")
                    expected_status, expected_location, _ = request(camp_port, "GET", path, source_cookie, "")
                    actual = (actual_status, urllib.parse.urlsplit(actual_location).path if actual_location else None)
                    expected = (expected_status, urllib.parse.urlsplit(expected_location).path if expected_location else None)
                    assert actual == expected, (path, bool(rust_cookie), actual, expected)
            rust = profile(rust_port, "session_token=benchmark-session", "benchmark-csrf", 1)
            camp = profile(camp_port, camp_cookie, camp_csrf, 1)
            compare_profile("administrator's own profile", rust, camp)
            for label, role, status in (
                ("active member", 0, 0),
                ("banned member", 0, 2),
                ("deactivated member", 0, 1),
                ("active bot", 2, 0),
                ("deactivated bot", 2, 1),
            ):
                set_state(rust_db, role, status)
                set_state(camp_db, role, status)
                rust = profile(rust_port, "session_token=benchmark-session", "benchmark-csrf")
                camp = profile(camp_port, camp_cookie, camp_csrf)
                compare_profile(label, rust, camp)
                for port, cookie, csrf, page in (
                    (rust_port, "session_token=benchmark-session", "benchmark-csrf", rust[0]),
                    (camp_port, camp_cookie, camp_csrf, camp[0]),
                ):
                    avatar_status, _, _ = request(port, "GET", avatar_path(page), cookie, csrf)
                    assert avatar_status == 200, (label, port, avatar_status)
            set_state(rust_db, 0, 2)
            set_state(camp_db, 0, 2)
            data = urllib.parse.urlencode({"_method": "delete"}).encode()
            results = []
            for port, cookie, csrf, database in (
                (rust_port, "session_token=benchmark-session", "benchmark-csrf", rust_db),
                (camp_port, camp_cookie, camp_csrf, camp_db),
            ):
                status, location, _ = request(port, "POST", "/users/2/ban", cookie, csrf, data, "application/x-www-form-urlencoded")
                with sqlite3.connect(database) as db:
                    saved = db.execute("SELECT status FROM users WHERE id=2").fetchone()[0]
                results.append((status, urllib.parse.urlsplit(location).path, saved))
            assert results == [(302, "/users/2", 0)] * 2, results
            jpeg = (REPOSITORY / "test/fixtures/files/moon.jpg").read_bytes()
            post_account_logo(camp_port, camp_cookie, camp_csrf, jpeg)
            post_account_logo(rust_port, "session_token=benchmark-session", "benchmark-csrf", jpeg)
            compare_profile(
                "administrator with account logo",
                profile(rust_port, "session_token=benchmark-session", "benchmark-csrf", 1),
                profile(camp_port, camp_cookie, camp_csrf, 1),
            )
            member_cookie, member_csrf = login_campfire(camp_port, "profile@example.test", "benchmark-password")
            for label, user_id in (("member's own profile with logo", 2), ("member viewing administrator with logo", 1)):
                compare_profile(
                    label,
                    profile(rust_port, "session_token=member-session", "member-csrf", user_id),
                    profile(camp_port, member_cookie, member_csrf, user_id),
                )
            print("PASS paired avatar routes, profile panels, and form-based unban")
        except Exception:
            log.flush()
            log.seek(0)
            print(log.read()[-3000:])
            raise
        finally:
            stop_server(rust_process)
            stop_server(camp_process)
            log.close()


if __name__ == "__main__":
    main()
