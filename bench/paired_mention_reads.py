"""Measure equal 40-rich-mention bot API pages on Rustfire and pinned Campfire."""

import argparse
import hashlib
import html
import http.client
import json
import pathlib
import sqlite3
import subprocess
import tempfile
import urllib.parse

from direct_lookup import free_port, start_server, stop_server
from paired_banned_content import BUNDLE, REPOSITORY, REVISION, RUBY, isolated_campfire, start_redis
from paired_direct_lookup import login_campfire, seed_campfire, seed_rustfire, wait_for_server
from paired_mention_webhook import BOT_ID, BOT_TOKEN, bot_sgid, seed_bot


PATH = f"/rooms/1/{BOT_ID}-{BOT_TOKEN}/messages"


def post_mentions(port, cookie, csrf, sgid):
    attachment = f'<action-text-attachment sgid="{html.escape(sgid, quote=True)}" content-type="application/vnd.campfire.mention"></action-text-attachment>'
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    try:
        for number in range(40):
            body = urllib.parse.urlencode({
                "message[body]": f"<div>Read {number} {attachment}</div>",
                "message[client_message_id]": f"paired-rich-read-{number}",
                "authenticity_token": csrf,
            })
            connection.request("POST", "/rooms/1/messages", body, {
                "Cookie": cookie,
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "text/vnd.turbo-stream.html, text/html",
            })
            response = connection.getresponse()
            payload = response.read()
            assert response.status == 200, (number, response.status, payload[:300])
    finally:
        connection.close()


def fetch(port):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    try:
        connection.request("GET", PATH, headers={"Accept": "application/json"})
        response = connection.getresponse()
        payload = response.read()
        assert response.status == 200 and response.getheader("Content-Type", "").startswith("application/json"), (response.status, payload[:300])
        return payload
    finally:
        connection.close()


def normalized(payload):
    messages = json.loads(payload)
    for message in messages:
        message.pop("created_at")
        for target, key in ((message, "url"), (message["creator"], "avatar_url")):
            url = urllib.parse.urlsplit(target[key])
            target[key] = url.path + ("?" + url.query if url.query else "")
    return messages


def measure(binary, port, clients, seconds, expected):
    command = [str(binary), "--base", f"http://127.0.0.1:{port}", "--path", PATH,
               "--cookie", "benchmark=1", "--expected-sha256", hashlib.sha256(expected).hexdigest(),
               "--expected-content-type", "application/json", "--clients", str(clients), "--seconds", str(seconds)]
    result = subprocess.run(command, text=True, capture_output=True, check=True)
    report = json.loads(result.stdout.strip().splitlines()[-1])
    assert report["errors"] == 0 and report["successes"] > 0, report
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--clients", type=int, nargs="+", default=[32])
    parser.add_argument("--seconds", type=float, default=10)
    parser.add_argument("--campfire-workers", type=int, default=22)
    parser.add_argument("--campfire-first", action="store_true")
    parser.add_argument("--slo-ms", type=float, help="optional p95 latency objective for reporting the largest sampled client count")
    parser.add_argument("--sample-dir", type=pathlib.Path)
    args = parser.parse_args()
    if any(client < 1 for client in args.clients) or args.seconds <= 0 or args.campfire_workers < 1 or (args.slo_ms is not None and args.slo_ms <= 0):
        parser.error("clients, seconds, Campfire workers, and optional SLO must be positive")
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPOSITORY, text=True).strip() == REVISION
    with tempfile.TemporaryDirectory(prefix="paired-mention-reads-") as scratch:
        temp = pathlib.Path(scratch)
        binary = temp / "checked_get"
        subprocess.run(["go", "build", "-o", str(binary), "bench/checked_get.go"], check=True)
        rust_db, camp_db = temp / "rust.sqlite3", temp / "camp.sqlite3"
        rust_port, camp_port, redis_port = free_port(), free_port(), free_port()
        seed_rustfire(rust_db, rust_port, [])
        camp_env = seed_campfire(REPOSITORY, RUBY, BUNDLE, REPOSITORY / "storage/db/production.sqlite3", camp_db, [], camp_port, temp)
        checkout = isolated_campfire(temp, redis_port)
        camp_env.update({"REDIS_URL": f"redis://127.0.0.1:{redis_port}", "WEB_CONCURRENCY": str(args.campfire_workers)})
        seed_bot(rust_db, False, "http://127.0.0.1:1/hook", with_webhooks=False)
        seed_bot(camp_db, True, "http://127.0.0.1:1/hook", with_webhooks=False)
        with sqlite3.connect(camp_db) as camp, sqlite3.connect(rust_db) as rust:
            rust.executemany("UPDATE users SET name=?2,updated_at=?3 WHERE id=?1", camp.execute("SELECT id,name,updated_at FROM users WHERE id IN(1,2)").fetchall())
            rust.execute("UPDATE rooms SET name=? WHERE id=1", [camp.execute("SELECT name FROM rooms WHERE id=1").fetchone()[0]])
        redis, redis_log = start_redis(temp, redis_port)
        try:
            def run_rustfire():
                process = start_server(rust_db, rust_port, {"RUSTFIRE_CAMPFIRE_SECRET_KEY_BASE": camp_env["SECRET_KEY_BASE"], "RUSTFIRE_DISABLE_WEBHOOKS": "0"})
                try:
                    sgid = bot_sgid(rust_port, "session_token=benchmark-session", False)
                    post_mentions(rust_port, "session_token=benchmark-session", "benchmark-csrf", sgid)
                    body = fetch(rust_port)
                    reports = {client: measure(binary, rust_port, client, args.seconds, body) for client in args.clients}
                    return sgid, body, reports
                finally:
                    stop_server(process)

            def run_campfire():
                with open(temp / "puma.log", "w+") as log:
                    process = subprocess.Popen([str(RUBY), str(RUBY.parent / "bundle"), "exec", "puma", "-C", "config/puma.rb"], cwd=checkout, env=camp_env, stdout=log, stderr=log)
                    try:
                        wait_for_server(camp_port, process)
                        cookie, csrf = login_campfire(camp_port)
                        sgid = bot_sgid(camp_port, cookie, True)
                        post_mentions(camp_port, cookie, csrf, sgid)
                        body = fetch(camp_port)
                        reports = {client: measure(binary, camp_port, client, args.seconds, body) for client in args.clients}
                        return sgid, body, reports
                    finally:
                        stop_server(process)

            if args.campfire_first:
                (camp_sgid, camp_body, camp_reports), (rust_sgid, rust_body, rust_reports) = run_campfire(), run_rustfire()
            else:
                (rust_sgid, rust_body, rust_reports), (camp_sgid, camp_body, camp_reports) = run_rustfire(), run_campfire()
        finally:
            redis.terminate()
            redis.wait(timeout=10)
            redis_log.close()
    assert rust_sgid == camp_sgid
    rust_messages, camp_messages = normalized(rust_body), normalized(camp_body)
    assert len(rust_messages) == len(camp_messages) == 40
    assert rust_messages == camp_messages, next(((left, right) for left, right in zip(rust_messages, camp_messages) if left != right), None)
    assert all("<action-text-attachment" in item["body"]["html"] for item in rust_messages)
    if args.sample_dir:
        args.sample_dir.mkdir(parents=True, exist_ok=True)
        (args.sample_dir / "rustfire-rich-mentions.json").write_bytes(rust_body)
        (args.sample_dir / "campfire-rich-mentions.json").write_bytes(camp_body)
    print("PASS all 40 normalized rich-mention bot messages match pinned Campfire; every timed GET matched its own warmed body hash")
    def report(body, reads):
        result = {"body_bytes": len(body), "reads": reads}
        if args.slo_ms is not None:
            result["largest_sampled_clients_under_slo"] = max((client for client in args.clients if reads[client]["errors"] == 0 and reads[client]["p95_ms"] < args.slo_ms), default=0)
        return result
    print(json.dumps({"clients": args.clients, "seconds": args.seconds, "campfire_workers": args.campfire_workers, "campfire_first": args.campfire_first, "slo_ms": args.slo_ms,
                      "rustfire": report(rust_body, rust_reports),
                      "campfire": report(camp_body, camp_reports)}, sort_keys=True))


if __name__ == "__main__":
    main()
