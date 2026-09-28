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
    ("nested-section", "<div>Before<section><p>Nested <em>text</em></p></section>After</div>"),
    ("script-text", "<div>Before<script>alert(1)</script>After</div>"),
    ("style-text", "<div>Before<style>.x{color:red}</style>After</div>"),
    ("iframe-text", "<div>Before<iframe src='https://example.com'>Fallback</iframe>After</div>"),
    ("svg-text", "<div>Before<svg><text>Vector</text></svg>After</div>"),
    ("image-alt", "<div>Before<img src='https://example.com/x.png' alt='Alternative'>After</div>"),
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
    ("two-paragraphs", "<div><p>First</p><p>Second</p></div>"),
    ("heading-paragraph", "<h2>Heading</h2><p>Following paragraph</p>"),
    ("blockquote-paragraph", "<blockquote><p>Quoted paragraph</p></blockquote><p>After</p>"),
    ("pre-linebreak", "<pre>First\nSecond</pre><div>After</div>"),
    ("nested-list", "<ul><li>One<ul><li>Nested</li></ul></li><li>Two</li></ul>"),
    ("ordered-list", "<ol><li>First</li><li>Second</li></ol>"),
    ("definition-list", "<dl><dt>Term</dt><dd>Definition</dd></dl><p>After</p>"),
    ("unknown-paragraph-wrapper", "<div>Before<section><p>Inside</p></section>After</div>"),
    ("linebreak-run", "<div>Before<br><br>After</div>"),
    ("mixed-inline", "<div>Left<strong>Bold</strong><em>Italic</em><code>Code</code>Right</div>"),
    ("leading-trailing-space", "<div>  Leading and trailing  </div>"),
    ("repeated-spaces", "<div>A   B&nbsp;&nbsp;C</div>"),
    ("encoded-entities", "<div>One &amp; two &lt; three &#169; &#x1F600;</div>"),
    ("leading-break", "<div><br>After</div>"),
    ("trailing-break", "<div>Before<br></div>"),
    ("empty-paragraph", "<p>Before</p><p></p><p>After</p>"),
    ("empty-div", "<div>Before</div><div></div><div>After</div>"),
    ("nested-blockquotes", "<blockquote><div>Outer<blockquote><div>Inner</div></blockquote>End</div></blockquote>"),
    ("mixed-nested-lists", "<ol><li>First<ul><li>Inner</li></ul></li><li>Second</li></ol>"),
    ("malformed-unclosed-div", "<div>Before<div>Inside"),
    ("malformed-unclosed-paragraph", "<p>Before<p>After"),
    ("malformed-table", "<div>Before<table><tr><td>Cell</table>After</div>"),
    ("comment-between", "<div>Before<!-- comment -->After</div>"),
    ("invisible-block", "<div>Before<div hidden>Hidden</div>After</div>"),
    ("soft-hyphen", "<div>One&shy;Two</div>"),
    ("unicode-lines", "<div>Line one\u2028Line two\u2029Line three</div>"),
    ("paste-inline-aliases", "<div>Before<b>Bold</b><i>Italic</i><u>Underline</u><s>Strike</s>After</div>"),
    ("paste-font", "<div>Before<font face='Arial' color='red'>Styled <b>text</b></font>After</div>"),
    ("paste-styled-span", "<div><span style='color: red; position: fixed' class='pasted' id='inserted'>Pasted</span></div>"),
    ("protocol-relative-link", "<div><a href='//example.com/path?x=1&amp;y=2'>Relative host</a></div>"),
    ("fragment-link", "<div><a href='#message_123'>Earlier message</a></div>"),
    ("nested-paste-list", "<blockquote><div>Quoted <b>start</b></div><ul><li>First</li><li>Second <i>end</i></li></ul></blockquote>"),
    ("paste-image-srcset", "<div>Before<img src='https://example.com/a.png' srcset='https://example.com/b.png 2x' width='20' height='30' alt='A'>After</div>"),
    ("paste-line-separators", "<div>First<br>Second\r\nThird\tFourth</div>"),
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
    pasted_widgets = ["audio", "button", "details", "form", "math", "noscript", "object", "picture", "ruby", "select", "svg", "template", "textarea", "title", "video"]
    cases.extend((f"sweep-pasted-widget-{tag}", f"<div>A<{tag}>B</{tag}>C</div>") for tag in pasted_widgets)
    cases.extend((f"sweep-attr-{key.replace(':', '-')}", f'<div><span {key}="{value}">X</span></div>') for key, value in attributes.items())
    cases.extend([
        ("sweep-a-hreflang", "<div><a href='/x' hreflang='en'>X</a></div>"),
        ("sweep-ol-start", "<div><ol start='4'><li>One</li></ol></div>"),
        ("sweep-hr-size", "<div>Before<hr size='3'>After</div>"),
        ("sweep-span-unsafe-href", "<div><span href='javascript:alert(1)'>X</span></div>"),
    ])
    pasted_link_attributes = {
        "aria-label": "Read more", "data-action": "click->malicious#run", "data-turbo-frame": "_top",
        "download": "file.txt", "id": "copied", "ping": "https://example.com/ping",
        "referrerpolicy": "no-referrer", "rel": "noopener", "target": "_blank",
    }
    cases.extend((f"sweep-link-attr-{name}", f'<div><a href="/x" {name}="{value}">X</a></div>') for name, value in pasted_link_attributes.items())
    cases.extend((f"sweep-uri-{scheme}", f'<div><a href="{scheme}:value">X</a><span src="{scheme}:value">Y</span></div>') for scheme in schemes)
    cases.extend((f"sweep-data-{index}", f'<div><a href="{value}">X</a><span src="{value}">Y</span></div>') for index, value in enumerate(data_urls))
    cases.extend((f"sweep-obfuscated-{index}", f'<div><a href="{value}">X</a><span src="{value}">Y</span></div>') for index, value in enumerate(obfuscated_urls))
    entities = [
        "&nbsp;", "&NewLine;", "&Tab;", "&ZeroWidthSpace;", "&shy;", "&apos;",
        "&notanentity;", "&#0;", "&#9;", "&#10;", "&#13;", "&#x7f;",
        "&#x85;", "&#x200b;", "&#x2028;", "&#x2029;", "&#x1f468;",
    ]
    cases.extend((f"sweep-entity-{index}", f"<div>Before{entity}After</div>") for index, entity in enumerate(entities))
    unicode_text = ["e\u0301", "👨‍👩‍👧‍👦", "🇺🇸", "a\u200bb", "a\u2060b", "a\ufeffb", "a\u202eb", "a\u00a0b"]
    cases.extend((f"sweep-unicode-{index}", f"<div>Before {value} After</div>") for index, value in enumerate(unicode_text))
    carriage_returns = ["&#xD;", "&#00013;", "&#x000D;", "\r", "\r\n", "&#13;&#13;", "A&#13;B", "&#13", "&#XD;", "&#xD"]
    cases.extend((f"sweep-carriage-return-{index}", f"<div>Before{value}After</div>") for index, value in enumerate(carriage_returns))
    cases.extend([
        ("sweep-cr-title-attribute", "<div><span title='Before&#13;After'>X</span></div>"),
        ("sweep-cr-link-attribute", "<div><a href='https://example.com/a&#13;b'>X</a></div>"),
        ("sweep-comment-fake-link", "<div><!-- <a title='fake' href='/fake'> --><a href='/real' title='Real'>X</a></div>"),
        ("sweep-script-fake-link", "<div><script>const x = \"<a title='fake' href='/fake'>\";</script><a href='/real' title='Real'>X</a></div>"),
        ("sweep-textarea-fake-link", "<div><textarea><a title='fake' href='/fake'></textarea><a href='/real' title='Real'>X</a></div>"),
    ])
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


def post_blank(port, cookie, csrf, name, body):
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
        presentation = Presentation(client_id)
        presentation.feed(payload.decode())
        return response.status, presentation.structure
    finally:
        connection.close()


def indexed_texts(database, cases):
    with sqlite3.connect(database) as db:
        return {
            name: db.execute(
                "SELECT s.body FROM message_search_index s JOIN messages m ON m.id=s.rowid WHERE m.client_message_id=?1",
                (f"paired-rich-filter-{name}",),
            ).fetchone()
            for name, _ in cases
        }


def stored_rich_source(database, name, campfire):
    with sqlite3.connect(database) as db:
        client_id = f"paired-rich-filter-{name}"
        if campfire:
            return db.execute(
                "SELECT body FROM action_text_rich_texts WHERE record_type='Message' AND name='body' AND record_id=(SELECT id FROM messages WHERE client_message_id=?)",
                (client_id,),
            ).fetchone()
        return db.execute("SELECT body_source FROM messages WHERE client_message_id=?", (client_id,)).fetchone()


def edit_message(port, cookie, csrf, database, name, body):
    with sqlite3.connect(database) as db:
        message_id = db.execute("SELECT id FROM messages WHERE client_message_id=?", (f"paired-rich-filter-{name}",)).fetchone()[0]
    fields = urllib.parse.urlencode({
        "_method": "patch",
        "message[body]": body,
        "authenticity_token": csrf,
    })
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
    try:
        connection.request("POST", f"/rooms/1/messages/{message_id}", fields, {
            "Cookie": cookie,
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "text/html",
        })
        response = connection.getresponse()
        response.read()
        return response.status, urllib.parse.urlsplit(response.getheader("Location") or "").path, indexed_texts(database, [(name, "")])[name]
    finally:
        connection.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sweep", action="store_true", help="include allowed-tag, attribute, and URL-scheme matrices")
    parser.add_argument("--source-audit", action="store_true", help="summarize saved ActionText source differences for all cases")
    args = parser.parse_args()
    cases = CASES + (sweep_cases() if args.sweep else [])
    blank_cases = [("spaces-only-div", "<div>  </div>"), ("break-only-div", "<div><br></div>"), ("empty-div", "<div></div>"), ("empty-raw", "")]
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
                rust_blank = {name: post_blank(rust_port, "session_token=benchmark-session", "benchmark-csrf", name, body) for name, body in blank_cases}
                rust_plain = indexed_texts(rust_db, cases)
                rust_sources = {name: stored_rich_source(rust_db, name, False) for name, _ in cases}
                rust_blank_plain = indexed_texts(rust_db, blank_cases)
                rust_edit = edit_message(rust_port, "session_token=benchmark-session", "benchmark-csrf", rust_db, "table", "<div>Edited<table><tr><td>Cell</td></tr></table>End</div>")
                rust_blank_edit = edit_message(rust_port, "session_token=benchmark-session", "benchmark-csrf", rust_db, "spaces-only-div", "<div></div>")
                rust_cr_edit = edit_message(rust_port, "session_token=benchmark-session", "benchmark-csrf", rust_db, "sweep-entity-10", "<div>Edited&#13;Again</div>") if args.sweep else None
                rust_cr_edit_source = stored_rich_source(rust_db, "sweep-entity-10", False) if args.sweep else None
            finally:
                stop_server(rust)
            with open(temp / "puma.log", "w+") as log:
                camp = subprocess.Popen([str(RUBY), str(RUBY.parent / "bundle"), "exec", "puma", "-C", "config/puma.rb"], cwd=REPOSITORY, env=camp_env, stdout=log, stderr=log)
                try:
                    wait_for_server(camp_port, camp)
                    cookie, csrf = login_campfire(camp_port)
                    camp_results = [post(camp_port, cookie, csrf, case) for case in cases]
                    camp_blank = {name: post_blank(camp_port, cookie, csrf, name, body) for name, body in blank_cases}
                    camp_plain = indexed_texts(camp_db, cases)
                    camp_sources = {name: stored_rich_source(camp_db, name, True) for name, _ in cases}
                    camp_blank_plain = indexed_texts(camp_db, blank_cases)
                    camp_edit = edit_message(camp_port, cookie, csrf, camp_db, "table", "<div>Edited<table><tr><td>Cell</td></tr></table>End</div>")
                    camp_blank_edit = edit_message(camp_port, cookie, csrf, camp_db, "spaces-only-div", "<div></div>")
                    camp_cr_edit = edit_message(camp_port, cookie, csrf, camp_db, "sweep-entity-10", "<div>Edited&#13;Again</div>") if args.sweep else None
                    camp_cr_edit_source = stored_rich_source(camp_db, "sweep-entity-10", True) if args.sweep else None
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
    plain_mismatches = {name: (rust_plain[name], camp_plain[name]) for name, _ in cases if rust_plain[name] != camp_plain[name]}
    assert not plain_mismatches, plain_mismatches
    source_differences = [(name, rust_sources[name], camp_sources[name]) for name, _ in cases if rust_sources[name] != camp_sources[name]]
    if args.source_audit:
        print(f"Saved ActionText source differs for {len(source_differences)}/{len(cases)} cases")
        for name, rust, camp in source_differences[:20]:
            print(f"{name}: Rustfire={rust!r}; Campfire={camp!r}")
    assert not source_differences, source_differences[:20]
    assert rust_blank == camp_blank, (rust_blank, camp_blank)
    assert all(status == 200 for status, _ in rust_blank.values()), rust_blank
    assert rust_blank_plain == camp_blank_plain, (rust_blank_plain, camp_blank_plain)
    assert rust_edit == camp_edit == (302, "/rooms/1/messages/2", ("EditedCellEnd",)), (rust_edit, camp_edit)
    assert rust_blank_edit == camp_blank_edit, (rust_blank_edit, camp_blank_edit)
    assert rust_blank_edit[0] == 302 and rust_blank_edit[2] == ("",), rust_blank_edit
    assert rust_cr_edit == camp_cr_edit, (rust_cr_edit, camp_cr_edit)
    assert rust_cr_edit_source == camp_cr_edit_source, (rust_cr_edit_source, camp_cr_edit_source)
    print(f"PASS {len(cases)} paired rich-text presentations, saved sources, and search-index bodies, four blank creates, and {'three' if args.sweep else 'two'} edits")


if __name__ == "__main__":
    main()
