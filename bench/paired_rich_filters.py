"""Compare Campfire's rendered rich-text sanitization with Rustfire."""

import argparse
import http.client
import pathlib
import sqlite3
import subprocess
import tempfile
import urllib.parse

from direct_lookup import free_port, start_server, stop_server
from paired_banned_content import BUNDLE, REPOSITORY, REVISION, RUBY, start_redis
from paired_direct_lookup import login_campfire, seed_campfire, seed_rustfire, wait_for_server
from paired_link_preview import Presentation


CASES = [
    ("unattached-image", "<div>Hello <img src='https://evil.example/image.svg'>World</div>"),
    ("table", "<div>Before<table><tr><td>Cell</td></tr></table>After</div>"),
    ("section", "<div>Before<section><strong>Hidden</strong></section>After</div>"),
    ("address", "<div>Before<address>Location</address>After</div>"),
    ("big", "<div>Before<big>Large</big>After</div>"),
    ("class", "<div><p class='para'>P</p><a class='link' href='/x'>X</a><code class='code'>C</code><strong class='bold'>B</strong></div>"),
    ("time", "<div><time datetime='2024-01-01' title='when'>Now</time></div>"),
    ("safe-attributes", "<div><span abbr='short' alt='alternative' cite='https://example.com/ref' datetime='2024-01-01' height='20' href='https://example.com/x' lang='en' name='marker' src='https://example.com/y' title='title' width='20' xml:lang='en'>X</span></div>"),
    ("extra-attributes", "<div><a href='/x' hreflang='en'>X</a><ol start='4'><li>One</li></ol><hr size='3'></div>"),
    ("unsafe-span-uris", "<div><span href='javascript:alert(1)' src='data:text/html,pwned'>X</span></div>"),
    ("allowed-schemes", "<div><a href='afs:resource'>A</a><a href='sftp:resource'>B</a></div>"),
    ("rejected-schemes", "<div><a href='ftps:resource'>A</a><a href='magnet:resource'>B</a><a href='bitcoin:resource'>C</a><a href='geo:resource'>D</a></div>"),
    ("safe-data-uri", "<div><a href='data:image/png;base64,aGVsbG8='>image</a><span src='data:text/plain,hello'>text</span></div>"),
    ("malformed-data-media", "<div><a href='data:text/html bad,hello'>fallback text</a><span src='data:image/png'>missing comma</span></div>"),
    ("event-handler", "<div><a href='/x' onmouseover='alert(1)'>x</a> <span onclick='alert(2)'>y</span></div>"),
    ("data-link", "<div><a href='data:text/html,pwned'>x</a></div>"),
    ("formatting", "<div><a href='https://example.com'>example</a> <strong>bold</strong> <code>code</code><ul><li>one</li><li>two</li></ul></div>"),
]


def sweep_cases():
    tags = ["abbr", "acronym", "address", "big", "cite", "dfn", "h2", "h3", "h4", "h5", "h6", "ins", "kbd", "samp", "small", "sub", "sup", "time", "tt", "var", "pre", "del", "hr"]
    attributes = {
        "abbr": "short", "alt": "alternate", "cite": "https://example.com/ref",
        "datetime": "2024-01-01", "height": "20", "href": "https://example.com/x",
        "lang": "en", "name": "marker", "src": "https://example.com/x",
        "title": "title", "width": "20", "xml:lang": "en", "sgid": "abc",
        "content-type": "text/plain", "url": "https://example.com/x",
        "filename": "file.txt", "filesize": "12", "previewable": "true",
        "presentation": "inline", "caption": "caption", "content": "text",
    }
    schemes = [
        "afs", "aim", "callto", "ed2k", "fax", "ftp", "gopher", "http", "https", "irc",
        "line", "mailto", "modem", "news", "nntp", "rsync", "rtsp", "sftp", "sms",
        "ssh", "tag", "tel", "telnet", "urn", "webcal", "xmpp", "ftps", "magnet",
        "bitcoin", "geo", "im", "ircs", "mms", "mx", "openpgp4fpr", "sip", "smsto",
        "url", "wtai", "javascript", "vbscript", "file", "blob",
    ]
    data_urls = [
        "data:image/png;base64,aGVsbG8=", "data:image/jpeg;base64,aGVsbG8=",
        "data:text/plain,hello", "data:text/css,body%7B%7D",
        "data:image/svg+xml,%3Csvg%3E", "data:text/html,%3Cscript%3E",
        "data:IMAGE/PNG;base64,aGVsbG8=",
    ]
    obfuscated_urls = [
        "java&#x73;cript:alert(1)", "javascript&#58;alert(1)",
        "java&#9;script:alert(1)", "java%73cript:alert(1)",
        "data&#58;text/html,pwned", "data:;base64,aGVsbG8=",
        "data:image/svg+xml,%3Csvg%3E", "data:text/html,%3Cscript%3E",
    ]
    cases = [(f"sweep-tag-{tag}", f"<div>A<{tag}>B</{tag}>C</div>") for tag in tags]
    cases.extend((f"sweep-attr-{key.replace(':', '-')}", f'<div><span {key}="{value}">X</span></div>') for key, value in attributes.items())
    cases.extend([
        ("sweep-a-hreflang", "<div><a href='/x' hreflang='en'>X</a></div>"),
        ("sweep-ol-start", "<div><ol start='4'><li>One</li></ol></div>"),
        ("sweep-hr-size", "<div>Before<hr size='3'>After</div>"),
        ("sweep-span-unsafe-href", "<div><span href='javascript:alert(1)'>X</span></div>"),
    ])
    cases.extend((f"sweep-uri-{scheme}", f'<div><a href="{scheme}:value">X</a><span src="{scheme}:value">Y</span></div>') for scheme in schemes)
    cases.extend((f"sweep-data-{index}", f'<div><a href="{value}">X</a><span src="{value}">Y</span></div>') for index, value in enumerate(data_urls))
    cases.extend((f"sweep-obfuscated-{index}", f'<div><a href="{value}">X</a><span src="{value}">Y</span></div>') for index, value in enumerate(obfuscated_urls))
    return cases


def post(port, cookie, csrf, case):
    name, body = case
    client_id = f"paired-rich-filter-{name}"
    fields = urllib.parse.urlencode({
        "message[body]": body,
        "message[client_message_id]": client_id,
        "authenticity_token": csrf,
    })
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
    try:
        connection.request("POST", "/rooms/1/messages", fields, {
            "Cookie": cookie,
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "text/vnd.turbo-stream.html, text/html",
        })
        response = connection.getresponse()
        payload = response.read()
        assert response.status == 200, (name, response.status, payload[:300])
        presentation = Presentation(client_id)
        presentation.feed(payload.decode())
        assert presentation.structure, (name, payload[:300])
        return presentation.structure
    finally:
        connection.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sweep", action="store_true", help="include allowed-tag, attribute, and URL-scheme matrices")
    args = parser.parse_args()
    cases = CASES + (sweep_cases() if args.sweep else [])
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPOSITORY, text=True).strip() == REVISION
    with tempfile.TemporaryDirectory(prefix="paired-rich-filters-") as scratch:
        temp = pathlib.Path(scratch)
        rust_db, camp_db = temp / "rust.sqlite3", temp / "camp.sqlite3"
        rust_port, camp_port, redis_port = free_port(), free_port(), free_port()
        seed_rustfire(rust_db, rust_port, [])
        camp_env = seed_campfire(REPOSITORY, RUBY, BUNDLE, REPOSITORY / "storage/db/production.sqlite3", camp_db, [], camp_port, temp)
        camp_env["REDIS_URL"] = f"redis://127.0.0.1:{redis_port}"
        camp_env["WEB_CONCURRENCY"] = "1"
        with sqlite3.connect(camp_db) as camp, sqlite3.connect(rust_db) as rust:
            camp.execute("DELETE FROM sqlite_sequence WHERE name='messages'")
            rust.executemany("UPDATE users SET name=?2,updated_at=?3 WHERE id=?1", camp.execute("SELECT id,name,updated_at FROM users WHERE id IN(1,2)").fetchall())
            rust.execute("UPDATE rooms SET name=? WHERE id=1", [camp.execute("SELECT name FROM rooms WHERE id=1").fetchone()[0]])
        redis, redis_log = start_redis(temp, redis_port)
        try:
            rust = start_server(rust_db, rust_port, {"RUSTFIRE_CAMPFIRE_SECRET_KEY_BASE": camp_env["SECRET_KEY_BASE"]})
            try:
                rust_results = [post(rust_port, "session_token=benchmark-session", "benchmark-csrf", case) for case in cases]
            finally:
                stop_server(rust)
            with open(temp / "puma.log", "w+") as log:
                camp = subprocess.Popen([str(RUBY), str(RUBY.parent / "bundle"), "exec", "puma", "-C", "config/puma.rb"], cwd=REPOSITORY, env=camp_env, stdout=log, stderr=log)
                try:
                    wait_for_server(camp_port, camp)
                    cookie, csrf = login_campfire(camp_port)
                    camp_results = [post(camp_port, cookie, csrf, case) for case in cases]
                finally:
                    stop_server(camp)
        finally:
            redis.terminate()
            redis.wait(timeout=10)
            redis_log.close()
    mismatches = [
        (case[0], rust_result, camp_result)
        for case, rust_result, camp_result in zip(cases, rust_results, camp_results)
        if rust_result != camp_result
    ]
    assert not mismatches, mismatches
    print(f"PASS {len(cases)} paired rich-text sanitizer presentations")


if __name__ == "__main__":
    main()
