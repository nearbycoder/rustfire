"""Compare account custom-style editing with the pinned Campfire checkout.

Run after cargo build --release with the pinned Ruby bundle and Redis.
"""

import html
import pathlib
import re
import sqlite3
import subprocess
import tempfile
import urllib.parse

from direct_lookup import free_port, start_server, stop_server
from paired_bot_admin import request
from paired_direct_lookup import login_campfire, seed_campfire, seed_rustfire, wait_for_server


EDIT = "/account/custom_styles/edit"
UPDATE = "/account/custom_styles"
FIRST = ":root { --color-text: red; }"
SECOND = "body { color: navy; }"


def attribute(tag, name):
    found = re.search(rf"(?:^|\s){re.escape(name)}=['\"]([^'\"]*)['\"]", tag)
    return html.unescape(found.group(1)) if found else None


def editor_summary(page):
    page = page.decode()
    panel = re.search(r"<section\b[^>]*view-transition-name: custom-styles[^>]*>(.*?)</section>", page, re.S)
    assert panel, "Custom styles panel missing"
    content = panel.group(1)
    form = re.search(r"<form\b(.*?)>(?=\s*<input)", content, re.S)
    assert form, "Custom styles form missing"
    form_tag = form.group(1)
    form_body = content[form.end():content.index("</form>", form.end())]
    textarea = re.search(r"<textarea\b([^>]*)>(.*?)</textarea>", form_body, re.S)
    assert textarea, "Custom CSS textarea missing"
    field_tag, field_value = textarea.groups()
    method = re.search(r"<input\b[^>]*name=['\"]_method['\"][^>]*>", form_body)
    assert method, "PATCH method override missing"
    warning = "Add custom CSS styles." in content and "Use Caution: you could break things." in content
    save = "Save changes" in content
    back = next((attribute(tag, "href") for tag, body in re.findall(r"<a\b([^>]*)>(.*?)</a>", page, re.S)
                 if "Go Back" in body), None)
    icon = re.search(r"<img\b[^>]*src=['\"]([^'\"]+alert[^'\"]*\.svg)['\"]", content)
    assert icon, "Warning icon missing"
    return {
        "form_action": urllib.parse.urlsplit(attribute(form_tag, "action")).path,
        "form_method": attribute(form_tag, "method"),
        "method_override": attribute(method.group(0), "value"),
        "field_name": attribute(field_tag, "name"),
        "rows": attribute(field_tag, "rows"),
        "placeholder": attribute(field_tag, "placeholder"),
        "autocomplete": attribute(field_tag, "autocomplete"),
        "spellcheck": attribute(field_tag, "spellcheck"),
        "autocorrect": attribute(field_tag, "autocorrect"),
        "autocapitalize": attribute(field_tag, "autocapitalize"),
        "value": html.unescape(field_value),
        "heading": "Custom CSS" in content,
        "warning": warning,
        "save": save,
        "translations": content.count("<dt>"),
        "back": urllib.parse.urlsplit(back).path if back else None,
        "icon": icon.group(1),
    }


def saved_styles(database):
    with sqlite3.connect(database) as db:
        return db.execute("SELECT custom_styles,updated_at FROM accounts LIMIT 1").fetchone()


def assert_inline_style(page, css):
    page = page.decode()
    styles = re.findall(r'<style\b[^>]*data-turbo-track=[\'"]reload[\'"][^>]*>(.*?)</style>', page, re.S)
    assert css in styles, "Saved CSS missing from inline style tag"
    assert "/account/custom_styles.css" not in page, "Unexpected separate custom CSS link"


def workflow(port, cookie, csrf, database, campfire):
    status, _, page = request(port, "GET", EDIT, cookie, csrf)
    assert status == 200, ("editor", status)
    initial = editor_summary(page)
    icon_path = urllib.parse.urlsplit(initial.pop("icon")).path
    icon_status, _, icon = request(port, "GET", icon_path, cookie, csrf)
    assert icon_status == 200 and b"<svg" in icon
    assert initial["form_action"] == UPDATE and initial["form_method"] == "post", initial
    assert initial["method_override"] == "patch"
    assert initial["field_name"] == "account[custom_styles]"
    assert initial["rows"] == "16" and initial["placeholder"] == "Add CSS styles…"
    assert all(initial[key] in ("off", "false") for key in ("autocomplete", "spellcheck", "autocorrect", "autocapitalize"))
    assert initial["heading"] and initial["warning"] and initial["save"] and initial["back"] == "/account/edit"
    assert initial["translations"] == 7
    before = saved_styles(database)
    rejected = []
    for label, method in (("plain-post", None), ("delete-override", "delete")):
        values = {"account[custom_styles]": "rejected"}
        if method:
            values["_method"] = method
        body = urllib.parse.urlencode(values).encode()
        status, _, _ = request(port, "POST", UPDATE, cookie, csrf, body, "application/x-www-form-urlencoded")
        rejected.append((label, status, saved_styles(database) == before))
    assert rejected == [("plain-post", 404, True), ("delete-override", 404, True)], rejected
    results = []
    for method, css, override in (("PATCH", FIRST, None), ("PUT", SECOND, None), ("POST", FIRST, "patch"), ("POST", SECOND, "put")):
        values = {"account[custom_styles]": css}
        if override:
            values["_method"] = override
        body = urllib.parse.urlencode(values).encode()
        status, location, response = request(port, method, UPDATE, cookie, csrf, body, "application/x-www-form-urlencoded")
        assert status == 302, ("update", method, status, response[:200])
        assert urllib.parse.urlsplit(location).path == EDIT
        saved = saved_styles(database)
        assert saved[0] == css
        if not campfire:
            with sqlite3.connect(database) as db:
                assert db.execute("SELECT css FROM account_custom_styles WHERE id=1").fetchone() == (css,)
        results.append((method, override, status, urllib.parse.urlsplit(location).path))
    assert saved_styles(database)[1] != before[1], "Account update timestamp was not touched"
    status, _, page = request(port, "GET", EDIT, cookie, csrf)
    assert status == 200
    final = editor_summary(page)
    final.pop("icon")
    assert final["value"].lstrip("\n") == SECOND
    assert_inline_style(page, SECOND)
    anonymous_status, _, anonymous_page = request(port, "GET", "/session/new", "", "")
    assert anonymous_status == 200, ("anonymous sign in", anonymous_status)
    assert_inline_style(anonymous_page, SECOND)
    assert request(port, "GET", "/account/custom_styles.css", "", "")[0] == 404
    with sqlite3.connect(database) as db:
        db.execute("UPDATE users SET role=0 WHERE id=1")
    assert request(port, "GET", EDIT, cookie, csrf)[0] == 403
    denied = request(port, "PATCH", UPDATE, cookie, csrf,
                     urllib.parse.urlencode({"account[custom_styles]": "denied"}).encode(),
                     "application/x-www-form-urlencoded")
    assert denied[0] == 403
    assert saved_styles(database)[0] == SECOND
    return initial, final, rejected, results, icon


def main():
    repo = pathlib.Path("/tmp/once-campfire-reference")
    ruby = pathlib.Path("/tmp/rustfire-baseline/local/bin/ruby")
    bundle = pathlib.Path("/tmp/rustfire-baseline/bundle")
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    assert revision == "91d294f4a09f9bbe37f9548959bfcb43645678fb", revision
    with tempfile.TemporaryDirectory(prefix="paired-custom-styles-") as scratch:
        temp = pathlib.Path(scratch)
        rust_db, camp_db = temp / "rust.sqlite3", temp / "camp.sqlite3"
        rust_port, camp_port = free_port(), free_port()
        seed_rustfire(rust_db, rust_port, [])
        camp_env = seed_campfire(repo, ruby, bundle, repo / "storage/db/production.sqlite3", camp_db, [], camp_port, temp)
        rust = start_server(rust_db, rust_port)
        try:
            rust_result = workflow(rust_port, "session_token=benchmark-session", "benchmark-csrf", rust_db, False)
        finally:
            stop_server(rust)
        log = open(temp / "puma.log", "w+")
        camp = subprocess.Popen([str(ruby), str(ruby.parent / "bundle"), "exec", "puma", "-C", "config/puma.rb"],
                                cwd=repo, env=camp_env, stdout=log, stderr=log)
        try:
            wait_for_server(camp_port, camp)
            cookie, csrf = login_campfire(camp_port)
            camp_result = workflow(camp_port, cookie, csrf, camp_db, True)
        except Exception:
            log.flush()
            log.seek(0)
            print(log.read()[-2000:])
            raise
        finally:
            stop_server(camp)
            log.close()
        assert rust_result == camp_result, (rust_result[:3], camp_result[:3])
        print("PASS custom CSS form, rejected POST methods, PATCH/PUT and browser-form persistence, inline styles, account touch, and member authorization")


if __name__ == "__main__":
    main()
