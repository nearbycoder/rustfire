"""Compare the pinned Campfire browser gate with Rustfire on disposable accounts."""

import http.client
import pathlib
import subprocess
import tempfile

from direct_lookup import free_port, start_server, stop_server
from paired_direct_lookup import login_campfire, seed_campfire, seed_rustfire, wait_for_server


REPO = pathlib.Path("/tmp/once-campfire-reference")
RUBY = pathlib.Path("/tmp/rustfire-baseline/local/bin/ruby")
BUNDLE = pathlib.Path("/tmp/rustfire-baseline/bundle")
RUSTFIRE = pathlib.Path(__file__).resolve().parents[1]
USER_AGENTS = {
    "missing": None,
    "Firefox 114": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:109.0) Gecko/20100101 Firefox/114.0",
    "Firefox 121": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0",
    "Chrome 119": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
    "Chrome 120": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Safari 17.1": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.1 Safari/605.1.15",
    "Safari 17.2": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Safari/605.1.15",
    "Opera 103": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/117.0.0.0 Safari/537.36 OPR/103.0.0.0",
    "Opera 104": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/118.0.0.0 Safari/537.36 OPR/104.0.0.0",
    "Internet Explorer 11": "Mozilla/5.0 (Windows NT 6.3; Trident/7.0; rv:11.0) like Gecko",
    "Chrome iOS 119": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) CriOS/119.0.6045.109 Mobile/15E148 Safari/604.1",
    "Chrome iOS 120": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) CriOS/120.0.6045.109 Mobile/15E148 Safari/604.1",
    "Firefox iOS 17.0": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) FxiOS/120.0 Mobile/15E148 Safari/605.1.15",
    "Firefox iOS 17.2": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_2 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) FxiOS/120.0 Mobile/15E148 Safari/605.1.15",
    "Chromium Edge 119": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36 Edg/119.0.0.0",
    "Legacy Edge 119": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36 Edge/119.0.0.0",
    "bot": "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html) Chrome/10.0.0.0",
    "Lighthouse bot": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36 Chrome-Lighthouse/11.0",
}


def request(port, path, agent=None, cookie="", accept="text/html"):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
    headers = {"Accept": accept} if accept is not None else {}
    if agent is not None:
        headers["User-Agent"] = agent
    if cookie:
        headers["Cookie"] = cookie
    try:
        connection.request("GET", path, headers=headers)
        response = connection.getresponse()
        body = response.read()
        return response.status, response.getheader("Content-Type"), body
    finally:
        connection.close()


def summary(port, cookie):
    results = {}
    for name, agent in USER_AGENTS.items():
        paths = [("/session/new", ""), ("/up", ""), ("/webmanifest", ""), ("/service-worker", "")]
        # The pinned app returns 500 when rendering an authenticated room for this Edge UA.
        if name != "Legacy Edge 119":
            paths.append(("/rooms/1", cookie))
        for path, session in paths:
            status, content_type, body = request(port, path, agent, session)
            blocked = b"Upgrade to a supported web browser" in body
            results[name, path] = status, blocked
            if blocked:
                assert content_type and "text/html" in content_type, (name, path, content_type)
                assert all(f"{browser}</strong>".encode() in body for browser in ("Safari", "Chrome", "Firefox", "Opera"))
                assert b"/webmanifest.json" in body
            elif path == "/session/new":
                assert b"/webmanifest.json" in body
    return results


def pwa_formats(port):
    results = {}
    for path, accept in (
        ("/webmanifest", "application/json"),
        ("/webmanifest", "*/*"),
        ("/webmanifest", None),
        ("/webmanifest.json", "text/html"),
        ("/webmanifest?format=json", "text/html"),
        ("/service-worker", "*/*"),
        ("/service-worker", None),
        ("/service-worker.js", "text/html"),
    ):
        status, content_type, body = request(port, path, accept=accept)
        results[path, accept] = status, content_type.split(";", 1)[0] if content_type else None
        if status == 200:
            assert body, (path, accept)
    return results


def main():
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPO, text=True).strip() == "91d294f4a09f9bbe37f9548959bfcb43645678fb"
    for name in ("safari", "chrome", "firefox", "opera"):
        source = REPO / "app/assets/images/browsers" / f"{name}.svg"
        copy = RUSTFIRE / "static/assets/browsers" / f"{name}.svg"
        assert copy.read_bytes() == source.read_bytes()
    with tempfile.TemporaryDirectory(prefix="paired-browser-compat-") as scratch:
        temp = pathlib.Path(scratch)
        rust_db, camp_db = temp / "rust.sqlite3", temp / "camp.sqlite3"
        rust_port, camp_port = free_port(), free_port()
        seed_rustfire(rust_db, rust_port, [])
        camp_env = seed_campfire(REPO, RUBY, BUNDLE, REPO / "storage/db/production.sqlite3", camp_db, [], camp_port, temp)
        rust = start_server(rust_db, rust_port)
        try:
            for name in ("safari", "chrome", "firefox", "opera"):
                status, _, body = request(rust_port, f"/assets/browsers/{name}.svg")
                assert status == 200 and body == (RUSTFIRE / "static/assets/browsers" / f"{name}.svg").read_bytes()
            rust_results = summary(rust_port, "session_token=benchmark-session")
            rust_pwa = pwa_formats(rust_port)
        finally:
            stop_server(rust)
        log = open(temp / "puma.log", "w+")
        camp = subprocess.Popen([str(RUBY), str(RUBY.parent / "bundle"), "exec", "puma", "-C", "config/puma.rb"], cwd=REPO, env=camp_env, stdout=log, stderr=log)
        try:
            wait_for_server(camp_port, camp)
            cookie, _ = login_campfire(camp_port)
            camp_results = summary(camp_port, cookie)
            camp_pwa = pwa_formats(camp_port)
        except Exception:
            log.flush()
            log.seek(0)
            print(log.read()[-2500:])
            raise
        finally:
            stop_server(camp)
            log.close()
        assert rust_results == camp_results, [(key, rust_results[key], camp_results.get(key)) for key in rust_results if rust_results[key] != camp_results.get(key)]
        assert rust_pwa == camp_pwa, [(key, rust_pwa[key], camp_pwa.get(key)) for key in rust_pwa if rust_pwa[key] != camp_pwa.get(key)]
        print("PASS browser gate for missing, old and supported agents, bot bypass, sign-in and room reads, plus paired manifest and service-worker content negotiation")


if __name__ == "__main__":
    main()
