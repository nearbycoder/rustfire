"""Probe signed RoomMessagesChannel fanout on disposable Rustfire and Campfire fixtures.

Requires the pinned Campfire checkout, its installed bundle, and Redis on localhost:6379.
Both runs use the same number of sockets and messages. Message HTML still differs.
"""

import argparse
import pathlib
import subprocess
import tempfile

from direct_lookup import ROOT, free_port, start_server, stop_server
from paired_direct_lookup import login_campfire, seed_campfire, seed_rustfire, wait_for_server


def fanout(app, port, cookie, csrf, sockets, messages):
    result = subprocess.run(
        [
            "node", "bench/fanout.mjs", "--app", app,
            "--base", f"http://127.0.0.1:{port}", "--cookie", cookie,
            "--csrf", csrf, "--room", "1", "--sockets", str(sockets),
            "--messages", str(messages),
        ],
        cwd=ROOT, text=True, capture_output=True,
    )
    if result.returncode:
        raise RuntimeError(f"{app} fanout failed: {result.stdout}\n{result.stderr}")
    return result.stdout.strip()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sockets", type=int, default=50)
    parser.add_argument("--messages", type=int, default=5)
    parser.add_argument("--campfire-workers", type=int, default=1)
    parser.add_argument("--campfire-repo", type=pathlib.Path, default=pathlib.Path("/tmp/once-campfire-reference"))
    parser.add_argument("--ruby", type=pathlib.Path, default=pathlib.Path("/tmp/rustfire-baseline/local/bin/ruby"))
    parser.add_argument("--bundle-path", type=pathlib.Path, default=pathlib.Path("/tmp/rustfire-baseline/bundle"))
    args = parser.parse_args()
    if args.sockets < 1 or args.messages < 1 or args.campfire_workers < 1:
        parser.error("sockets, messages, and workers must be positive")
    repo = args.campfire_repo.resolve()
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    if revision != "91d294f4a09f9bbe37f9548959bfcb43645678fb":
        parser.error(f"Campfire source is at {revision}, not the pinned compatibility target")
    ruby = args.ruby.resolve()
    with tempfile.TemporaryDirectory(prefix="paired-turbo-fanout-") as scratch:
        temp = pathlib.Path(scratch)
        rust_db, camp_db = temp / "rust.sqlite3", temp / "camp.sqlite3"
        rust_port, camp_port = free_port(), free_port()
        seed_rustfire(rust_db, rust_port, [])
        camp_env = seed_campfire(repo, ruby, args.bundle_path.resolve(), repo / "storage/db/production.sqlite3", camp_db, [], camp_port, temp)
        camp_env["WEB_CONCURRENCY"] = str(args.campfire_workers)

        rust = start_server(rust_db, rust_port, {
            "RUSTFIRE_DISABLE_PUSH": "0", "RUSTFIRE_DISABLE_WEBHOOKS": "0",
            "RUSTFIRE_CAMPFIRE_SECRET_KEY_BASE": camp_env["SECRET_KEY_BASE"],
        })
        try:
            rust_result = fanout("rustfire-turbo", rust_port, "session_token=benchmark-session", "benchmark-csrf", args.sockets, args.messages)
        finally:
            stop_server(rust)

        with open(temp / "puma.log", "w+") as log:
            camp = subprocess.Popen(
                [str(ruby), str(ruby.parent / "bundle"), "exec", "puma", "-C", "config/puma.rb"],
                cwd=repo, env=camp_env, stdout=log, stderr=log,
            )
            try:
                wait_for_server(camp_port, camp)
                cookie, csrf = login_campfire(camp_port)
                camp_result = fanout("campfire", camp_port, cookie, csrf, args.sockets, args.messages)
            except Exception:
                log.flush()
                log.seek(0)
                print(log.read()[-2000:])
                raise
            finally:
                stop_server(camp)
        print(f"rustfire-turbo {rust_result}")
        print(f"campfire       {camp_result}")


if __name__ == "__main__":
    main()
