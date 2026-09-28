"""Compare a varied live Campfire rich-text history with its Rustfire import.

Run after ``cargo build --release``. The probe posts through Campfire, stops it,
imports the database, and compares saved source, FTS text, and each message's
rendered presentation on both servers.
"""

import argparse
import http.client
import json
import os
import pathlib
import re
import sqlite3
import subprocess
import sys
import tempfile

from direct_lookup import free_port, start_server, stop_server
from paired_banned_content import BUNDLE, REPOSITORY, REVISION, RUBY, start_redis
from paired_direct_lookup import login_campfire, seed_campfire, wait_for_server
from paired_import_roundtrip import measure_imported_reads
from paired_link_preview import Presentation
from paired_rich_filters import CASES, post, stored_rich_source, sweep_cases
from paired_room_shell import get_room, section


ROOT = pathlib.Path(__file__).resolve().parents[1]
EXTRA_NAMES = {
    "sweep-entity-10", "sweep-carriage-return-0", "sweep-carriage-return-4",
    "sweep-cr-title-attribute", "sweep-cr-link-attribute", "sweep-comment-fake-link",
    "sweep-script-fake-link", "sweep-textarea-fake-link", "sweep-unicode-0",
}
def message_rows(database, campfire, corpus):
    with sqlite3.connect(database) as db:
        ids = {
            client_id.removeprefix("paired-rich-filter-"): message_id
            for message_id, client_id in db.execute("SELECT id,client_message_id FROM messages")
            if client_id.startswith("paired-rich-filter-")
        }
        assert set(ids) == {name for name, _ in corpus}, (len(ids), len(corpus))
        search = {
            name: db.execute("SELECT body FROM message_search_index WHERE rowid=?", [message_id]).fetchone()
            for name, message_id in ids.items()
        }
    source = {name: stored_rich_source(database, name, campfire) for name in ids}
    return ids, search, source


def presentation(port, cookie, name, message_id):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
    try:
        connection.request("GET", f"/rooms/1/messages/{message_id}", headers={"Cookie": cookie, "Accept": "text/html"})
        response = connection.getresponse()
        payload = response.read()
        assert response.status == 200, (name, response.status, payload[:200])
        parsed = Presentation(f"paired-rich-filter-{name}")
        parsed.feed(payload.decode())
        assert parsed.structure, (name, payload[:200])
        return parsed.structure
    finally:
        connection.close()


def older_pages(port, cookie, ids):
    ordered = sorted(ids.values())
    pages = []
    for index in range(40, len(ordered), 40):
        path = f"/rooms/1/messages?before={ordered[-index]}"
        payload = get_room(port, cookie, path)
        actual = sorted(int(value) for value in re.findall(rb'data-message-id=["\'](\d+)', payload))
        expected = ordered[max(0, len(ordered) - index - 40):len(ordered) - index]
        assert actual == expected, (path, actual, expected)
        pages.append(payload)
    return pages


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--read-clients", type=int, nargs="*", default=[])
    parser.add_argument("--seconds", type=float, default=5.0)
    parser.add_argument("--campfire-workers", type=int, default=22)
    parser.add_argument("--full-sweep", action="store_true", help="post every rich-filter sweep case and compare every imported page")
    parser.add_argument("--report", type=pathlib.Path)
    args = parser.parse_args()
    if any(clients < 1 for clients in args.read_clients) or args.seconds <= 0 or args.campfire_workers < 1:
        parser.error("positive client counts, seconds, and Campfire worker count are required")
    if args.report and not args.read_clients:
        parser.error("--report requires --read-clients")
    corpus = CASES + (sweep_cases() if args.full_sweep else [case for case in sweep_cases() if case[0] in EXTRA_NAMES])
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPOSITORY, text=True).strip() == REVISION
    with tempfile.TemporaryDirectory(prefix="paired-import-rich-corpus-") as scratch:
        temp = pathlib.Path(scratch)
        camp_db, rust_db, uploads = temp / "camp.sqlite3", temp / "rust.sqlite3", temp / "uploads"
        camp_port, rust_port, redis_port = free_port(), free_port(), free_port()
        environment = seed_campfire(REPOSITORY, RUBY, BUNDLE, REPOSITORY / "storage/db/production.sqlite3", camp_db, [], camp_port, temp)
        environment["REDIS_URL"] = f"redis://127.0.0.1:{redis_port}"
        environment["WEB_CONCURRENCY"] = "1"
        redis, redis_log = start_redis(temp, redis_port)
        try:
            with (temp / "puma.log").open("w+") as log:
                camp = subprocess.Popen(
                    [str(RUBY), str(RUBY.parent / "bundle"), "exec", "puma", "-C", "config/puma.rb"],
                    cwd=REPOSITORY, env=environment, stdout=log, stderr=log,
                )
                try:
                    wait_for_server(camp_port, camp)
                    cookie, csrf = login_campfire(camp_port)
                    for case in corpus:
                        post(camp_port, cookie, csrf, case)
                    source_ids, source_search, source_bodies = message_rows(camp_db, True, corpus)
                    source_presentations = {name: presentation(camp_port, cookie, name, message_id) for name, message_id in source_ids.items()}
                    source_room = get_room(camp_port, cookie, "/rooms/1")
                    source_older = older_pages(camp_port, cookie, source_ids)
                except Exception:
                    log.flush()
                    log.seek(0)
                    print(log.read()[-3000:])
                    raise
                finally:
                    stop_server(camp)
        finally:
            redis.terminate()
            redis.wait(timeout=10)
            redis_log.close()
        command = [sys.executable, "tools/import_campfire.py", "--source-db", str(camp_db),
                   "--source-files", str(REPOSITORY / "storage/files"), "--target-db", str(rust_db),
                   "--target-uploads", str(uploads), "--rustfire-bin", "target/release/rustfire"]
        imported = subprocess.run(command, cwd=ROOT, env=dict(os.environ, RUSTFIRE_CAMPFIRE_SECRET_KEY_BASE=environment["SECRET_KEY_BASE"]),
                                  capture_output=True, text=True, timeout=180)
        assert imported.returncode == 0, imported.stdout + imported.stderr
        counts = json.loads(imported.stdout)
        target_ids, target_search, target_bodies = message_rows(rust_db, False, corpus)
        assert target_ids == source_ids, (target_ids, source_ids)
        assert target_search == source_search, {name: (target_search[name], source_search[name]) for name in source_ids if target_search[name] != source_search[name]}
        assert target_bodies == source_bodies, {name: (target_bodies[name], source_bodies[name]) for name in source_ids if target_bodies[name] != source_bodies[name]}
        rust = start_server(rust_db, rust_port, {"RUSTFIRE_UPLOAD_DIR": str(uploads), "RUSTFIRE_CAMPFIRE_SECRET_KEY_BASE": environment["SECRET_KEY_BASE"]})
        try:
            target_presentations = {name: presentation(rust_port, cookie, name, message_id) for name, message_id in target_ids.items()}
            target_room = get_room(rust_port, cookie, "/rooms/1")
            target_older = older_pages(rust_port, cookie, target_ids)
        finally:
            stop_server(rust)
        mismatches = {name: (target_presentations[name], source_presentations[name]) for name in source_ids if target_presentations[name] != source_presentations[name]}
        assert not mismatches, dict(list(mismatches.items())[:5])
        normalized = {"normalize_times": True, "ignore_csrf_inputs": True, "normalize_avatar_paths": True,
                      "normalize_blob_paths": True, "normalize_text_origins": True}
        for part in ("head", "body"):
            assert section(source_room, part, **normalized) == section(target_room, part, **normalized), part
        for index, (source_page, target_page) in enumerate(zip(source_older, target_older), 1):
            assert section(b"<body>" + source_page + b"</body>", "body", **normalized) == section(
                b"<body>" + target_page + b"</body>", "body", **normalized), f"older page {index}"
        if args.read_clients:
            trials = measure_imported_reads(temp, REPOSITORY, RUBY, environment, camp_port, rust_db, rust_port,
                                            uploads, cookie, args.read_clients, args.seconds, args.campfire_workers, 40)
            if args.report:
                args.report.parent.mkdir(parents=True, exist_ok=True)
                args.report.write_text(json.dumps({
                    "date": "2026-09-28",
                    "campfire_commit": REVISION,
                    "rustfire_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
                    "command": "python bench/paired_import_rich_corpus.py" + (" --full-sweep" if args.full_sweep else "") + " --read-clients " + " ".join(map(str, args.read_clients))
                               + f" --seconds {args.seconds:g} --campfire-workers {args.campfire_workers} --report {args.report}",
                    "fixture": f"{len(corpus)} imported rich messages; latest 40-message room page",
                    "latest_page_bytes": {"campfire": len(source_room), "rustfire": len(target_room)},
                    "older_page_bytes": {"campfire": len(source_older[0]), "rustfire": len(target_older[0])},
                    "all_older_page_bytes": {"campfire": [len(page) for page in source_older], "rustfire": [len(page) for page in target_older]},
                    "seconds": args.seconds,
                    "campfire_workers": args.campfire_workers,
                    "trials": trials,
                }, indent=2) + "\n")
    print(f"PASS {len(corpus)} imported rich messages preserve IDs, saved ActionText source, FTS text, individual presentations, and parsed latest/{len(source_older)} older pages; latest bytes {len(source_room)}/{len(target_room)} Campfire/Rustfire; import: {counts}")


if __name__ == "__main__":
    main()
