"""Compare clicked image and QR lightboxes with pinned Campfire in Chromium."""

import json
import pathlib
import shutil
import sqlite3
import subprocess
import tempfile
import uuid

from direct_lookup import free_port, start_server, stop_server
from paired_banned_content import BUNDLE, REPOSITORY, REVISION, RUBY, start_redis
from paired_bot_admin import cleanup_campfire_uploads
from paired_direct_lookup import login_campfire, seed_campfire, seed_rustfire, wait_for_server
from paired_reply_browser import browser
from paired_room_shell import post_file_message


def inspect_lightbox(session, selector):
    browser(session, "click", selector)
    opened = json.loads(browser(session, "eval", """(() => {
      const link = document.querySelector(%s);
      const dialog = document.querySelector('dialog[data-lightbox-target="dialog"]');
      const image = dialog?.querySelector('[data-lightbox-target="zoomedImage"]');
      const download = dialog?.querySelector('[data-lightbox-target="download"]');
      const share = dialog?.querySelector('[data-lightbox-target="share"]');
      return {open:dialog?.open, imageSrcMatches:image?.src===link?.href,
        downloadMatches:download?.href===new URL(link?.dataset.lightboxUrlValue, location.href).href,
        shareMatches:share?.dataset.webShareFilesValue===link?.dataset.lightboxUrlValue,
        shareHidden:share?.hidden, hasAdHocDialog:!!document.querySelector('.image-lightbox')};
    })()""" % json.dumps(selector)))
    browser(session, "click", 'dialog.lightbox form[method="dialog"] button')
    closed = json.loads(browser(session, "eval", """(async () => {
      await new Promise(resolve => setTimeout(resolve, 100));
      const dialog = document.querySelector('dialog[data-lightbox-target="dialog"]');
      return {closed:!dialog?.open,
        imageReset:dialog?.querySelector('[data-lightbox-target="zoomedImage"]')?.getAttribute('src')==='',
        downloadReset:dialog?.querySelector('[data-lightbox-target="download"]')?.getAttribute('href')==='',
        shareReset:dialog?.querySelector('[data-lightbox-target="share"]')?.dataset.webShareFilesValue===''};
    })()"""))
    result = {**opened, **closed}
    assert all(value for key, value in result.items() if key not in ("hasAdHocDialog", "shareHidden")) and not result["hasAdHocDialog"], result
    return result


def check_app(session, port):
    base = f"http://127.0.0.1:{port}"
    browser(session, "open", f"{base}/session/new")
    browser(session, "fill", 'input[name="email_address"]', "benchmark@example.invalid")
    browser(session, "fill", 'input[name="password"]', "benchmark-password")
    browser(session, "click", 'button[name="log_in"]')
    browser(session, "wait", "--url", "**/rooms/1")
    browser(session, "wait", "#system_welcome a[data-action~='lightbox#open']")
    browser(session, "wait", "--load", "networkidle")
    result = {"invite": inspect_lightbox(session, "#system_welcome a[data-action~='lightbox#open']")}
    result["attachment"] = inspect_lightbox(session, "#message_lightbox-image a[data-action~='lightbox#open']")
    browser(session, "open", f"{base}/users/1/profile")
    browser(session, "wait", "a[data-action~='lightbox#open']")
    browser(session, "wait", "--load", "networkidle")
    result["profile"] = inspect_lightbox(session, "a[data-action~='lightbox#open']")
    return result


def main():
    assert shutil.which("agent-browser"), "agent-browser CLI is required"
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPOSITORY, text=True).strip() == REVISION
    sessions = [f"lightbox-rust-{uuid.uuid4().hex[:8]}", f"lightbox-camp-{uuid.uuid4().hex[:8]}"]
    with tempfile.TemporaryDirectory(prefix="paired-lightbox-browser-") as scratch:
        temp = pathlib.Path(scratch)
        rust_db, camp_db = temp / "rust.sqlite3", temp / "camp.sqlite3"
        rust_port, camp_port, redis_port = free_port(), free_port(), free_port()
        seed_rustfire(rust_db, rust_port, [])
        environment = seed_campfire(REPOSITORY, RUBY, BUNDLE, REPOSITORY / "storage/db/production.sqlite3", camp_db, [], camp_port, temp)
        environment["REDIS_URL"] = f"redis://127.0.0.1:{redis_port}"
        with sqlite3.connect(camp_db) as camp:
            password, name, updated_at = camp.execute("SELECT password_digest,name,updated_at FROM users WHERE id=1").fetchone()
            account = camp.execute("SELECT name,updated_at FROM accounts WHERE id=1").fetchone()
            room_name = camp.execute("SELECT name FROM rooms WHERE id=1").fetchone()[0]
        with sqlite3.connect(rust_db) as rust:
            rust.execute("UPDATE users SET email_address='benchmark@example.invalid',password_digest=?,name=?,updated_at=? WHERE id=1", (password, name, updated_at))
            rust.execute("UPDATE accounts SET name=?,updated_at=? WHERE id=1", account)
            rust.execute("UPDATE rooms SET name=? WHERE id=1", [room_name])
        redis, redis_log = start_redis(temp, redis_port)
        try:
            rust = start_server(rust_db, rust_port, {"RUSTFIRE_CAMPFIRE_SECRET_KEY_BASE": environment["SECRET_KEY_BASE"], "RUSTFIRE_UPLOAD_DIR": str(temp / "uploads")})
            try:
                with open(temp / "puma.log", "w+") as log:
                    camp = subprocess.Popen(
                        [str(RUBY), str(RUBY.parent / "bundle"), "exec", "puma", "-C", "config/puma.rb"],
                        cwd=REPOSITORY, env=environment, stdout=log, stderr=log,
                    )
                    try:
                        wait_for_server(camp_port, camp)
                        camp_cookie, camp_csrf = login_campfire(camp_port)
                        jpeg = (REPOSITORY / "test/fixtures/files/moon.jpg").read_bytes()
                        for port, cookie, csrf in ((rust_port, "session_token=benchmark-session", "benchmark-csrf"), (camp_port, camp_cookie, camp_csrf)):
                            post_file_message(port, cookie, csrf, "moon.jpg", "image/jpeg", jpeg, "lightbox-image")
                        rust_result = check_app(sessions[0], rust_port)
                        camp_result = check_app(sessions[1], camp_port)
                        assert rust_result == camp_result, (rust_result, camp_result)
                    finally:
                        for session in sessions:
                            subprocess.run(["agent-browser", "--session", session, "close"], capture_output=True, timeout=15)
                        stop_server(camp)
                        cleanup_campfire_uploads(REPOSITORY / "storage/db/production.sqlite3", camp_db, REPOSITORY)
            finally:
                stop_server(rust)
        finally:
            redis.terminate()
            redis.wait(timeout=10)
            redis_log.close()
    print("PASS invite QR, image attachment, and profile QR lightboxes match Campfire in Chromium")


if __name__ == "__main__":
    main()
