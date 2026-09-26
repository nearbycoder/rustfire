"""Compare a pasted Open Graph preview in the pinned Campfire and Rustfire browsers."""

import json
from html.parser import HTMLParser
import pathlib
import shutil
import sqlite3
import subprocess
import tempfile
import uuid

from direct_lookup import free_port, start_server, stop_server
from paired_banned_content import BUNDLE, REPOSITORY, REVISION, RUBY, start_redis
from paired_direct_lookup import seed_campfire, seed_rustfire, wait_for_server
from paired_reply_browser import browser


MOCK_PREVIEW = {
    "title": "A normal looking link",
    "description": "Nothing to see here",
    "url": "https://example.com/harmless",
    "image": 'https://example.com/image.png?from=" style="outline:9px solid red',
}


class AttachmentMarkup(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tokens = []

    def handle_starttag(self, tag, attrs):
        self.tokens.append((tag, tuple(sorted(attrs))))

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        self.tokens.append(("/", tag))

    def handle_data(self, data):
        if data.strip():
            self.tokens.append(("text", data.strip()))


def inspect_paste(session, port):
    base = f"http://127.0.0.1:{port}"
    browser(session, "open", f"{base}/session/new")
    browser(session, "fill", 'input[name="email_address"]', "benchmark@example.invalid")
    browser(session, "fill", 'input[name="password"]', "benchmark-password")
    browser(session, "click", 'button[name="log_in"]')
    browser(session, "wait", "--url", "**/rooms/1")
    browser(session, "wait", "#composer trix-editor")
    browser(session, "wait", "--load", "networkidle")
    browser(session, "eval", """(() => {
      const original = window.fetch;
      window.__previewPayload = %s;
      window.__unfurlRequests = 0;
      window.fetch = function(input, options) {
        const url = typeof input === 'string' ? input : input.url;
        if (new URL(url, location.href).pathname === '/unfurl_link') {
          window.__unfurlRequests++;
          return Promise.resolve(new Response(JSON.stringify(window.__previewPayload), {
            status: 200, headers: {'Content-Type':'application/json'}
          }));
        }
        return original.apply(this, arguments);
      };
    })()""" % json.dumps(MOCK_PREVIEW))
    browser(session, "eval", """(() => {
      const editor = document.querySelector('#composer trix-editor');
      editor.focus();
      const clipboardData = new DataTransfer();
      clipboardData.setData('text/plain', 'https://example.com/harmless');
      editor.dispatchEvent(new ClipboardEvent('paste', {
        clipboardData, bubbles: true, cancelable: true
      }));
    })()""")
    browser(session, "wait", "#composer trix-editor .og-embed")
    result = json.loads(json.loads(browser(session, "eval", """(() => {
      const editor = document.querySelector('#composer trix-editor');
      const card = editor.querySelector('.og-embed');
      const image = card.querySelector('img');
      return JSON.stringify({
        requestCount: window.__unfurlRequests,
        previewCount: editor.querySelectorAll('.og-embed').length,
        title: card.querySelector('.og-embed__title')?.textContent,
        description: card.querySelector('.og-embed__description')?.textContent,
        imageAttributes: [...image.attributes].map(attribute => [attribute.name, attribute.value]).sort(),
        wrapperClass: card.parentElement.getAttribute('class'),
        attachmentContent: editor.editor.getDocument().getAttachments()[0]?.getAttributes().content
      });
    })()""")))
    markup = AttachmentMarkup()
    markup.feed(result.pop("attachmentContent"))
    result["attachmentMarkup"] = markup.tokens
    browser(session, "eval", """(() => {
      window.__previewPayload.image = null;
      document.querySelector('#composer trix-editor').editor.loadHTML('');
    })()""")
    browser(session, "eval", """(() => {
      const editor = document.querySelector('#composer trix-editor');
      editor.focus();
      const clipboardData = new DataTransfer();
      clipboardData.setData('text/plain', 'https://example.com/harmless');
      editor.dispatchEvent(new ClipboardEvent('paste', {
        clipboardData, bubbles: true, cancelable: true
      }));
    })()""")
    browser(session, "wait", "200")
    result["imageLess"] = json.loads(json.loads(browser(session, "eval", """(() => JSON.stringify({
      requestCount: window.__unfurlRequests,
      previewCount: document.querySelectorAll('#composer trix-editor .og-embed').length
    }))()""")))
    assert result["imageLess"] == {"requestCount": 2, "previewCount": 0}, result
    return result


def inspect_cancelled_paste(session, port):
    browser(session, "open", f"http://127.0.0.1:{port}/rooms/1")
    browser(session, "wait", "#composer trix-editor")
    browser(session, "wait", "--load", "networkidle")
    browser(session, "eval", """(() => {
      const original = window.fetch;
      window.__unfurlRequests = 0;
      window.__unfurlAborts = 0;
      window.fetch = function(input, options) {
        const url = typeof input === 'string' ? input : input.url;
        if (new URL(url, location.href).pathname === '/unfurl_link') {
          const count = ++window.__unfurlRequests;
          if (count === 1) return new Promise((resolve, reject) => {
            const signal = options?.signal || input?.signal;
            signal?.addEventListener('abort', () => {
              window.__unfurlAborts++;
              reject(new DOMException('cancelled', 'AbortError'));
            }, {once: true});
            window.__resolveStale = () => resolve(new Response(JSON.stringify({
              title: 'Stale preview', description: 'Old request',
              url: 'https://example.com/first', image: 'https://example.com/first.png'
            }), {status: 200, headers: {'Content-Type':'application/json'}}));
          });
          return Promise.resolve(new Response(JSON.stringify({
            title: 'Current preview', description: 'New request',
            url: 'https://example.com/second', image: 'https://example.com/second.png'
          }), {status: 200, headers: {'Content-Type':'application/json'}}));
        }
        return original.apply(this, arguments);
      };
    })()""")

    def paste(url):
        browser(session, "eval", """((url) => {
          const editor = document.querySelector('#composer trix-editor');
          editor.focus();
          const clipboardData = new DataTransfer();
          clipboardData.setData('text/plain', url);
          editor.dispatchEvent(new ClipboardEvent('paste', {
            clipboardData, bubbles: true, cancelable: true
          }));
        })(%s)""" % json.dumps(url))

    paste("https://example.com/first")
    browser(session, "wait", "100")
    assert json.loads(browser(session, "eval", "window.__unfurlRequests")) == 1
    browser(session, "eval", "document.querySelector('#composer trix-editor').editor.loadHTML('')")
    paste("https://example.com/second")
    browser(session, "wait", "#composer trix-editor .og-embed__title")
    browser(session, "eval", "window.__resolveStale?.()")
    browser(session, "wait", "100")
    return json.loads(json.loads(browser(session, "eval", """(() => JSON.stringify({
      requests: window.__unfurlRequests,
      aborted: window.__unfurlAborts,
      cards: [...document.querySelectorAll('#composer trix-editor .og-embed__title')].map(node => node.textContent)
    }))()""")))


def main():
    assert shutil.which("agent-browser"), "agent-browser CLI is required"
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPOSITORY, text=True).strip() == REVISION
    sessions = [f"unfurl-rust-{uuid.uuid4().hex[:8]}", f"unfurl-camp-{uuid.uuid4().hex[:8]}"]
    with tempfile.TemporaryDirectory(prefix="paired-unfurl-browser-") as scratch:
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
            rust = start_server(rust_db, rust_port, {"RUSTFIRE_CAMPFIRE_SECRET_KEY_BASE": environment["SECRET_KEY_BASE"]})
            try:
                with open(temp / "puma.log", "w+") as log:
                    camp = subprocess.Popen(
                        [str(RUBY), str(RUBY.parent / "bundle"), "exec", "puma", "-C", "config/puma.rb"],
                        cwd=REPOSITORY, env=environment, stdout=log, stderr=log,
                    )
                    try:
                        wait_for_server(camp_port, camp)
                        rust_result = inspect_paste(sessions[0], rust_port)
                        camp_result = inspect_paste(sessions[1], camp_port)
                        assert rust_result == camp_result, "Pasted preview differs from Campfire"
                        rust_cancelled = inspect_cancelled_paste(sessions[0], rust_port)
                        camp_cancelled = inspect_cancelled_paste(sessions[1], camp_port)
                        assert rust_cancelled == camp_cancelled == {
                            "requests": 2, "aborted": 1, "cards": ["Current preview"]
                        }, (rust_cancelled, camp_cancelled)
                    finally:
                        for session in sessions:
                            subprocess.run(["agent-browser", "--session", session, "close"], capture_output=True, timeout=15)
                        stop_server(camp)
            finally:
                stop_server(rust)
        finally:
            redis.terminate()
            redis.wait(timeout=10)
            redis_log.close()
    print("PASS pasted Open Graph preview, quoted image URL, missing image, and request cancellation match Campfire")


if __name__ == "__main__":
    main()
