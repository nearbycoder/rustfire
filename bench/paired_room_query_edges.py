"""Compare room form/query precedence with pinned Campfire.

Run after ``cargo build --release``. Each case uses fresh disposable databases
and checks responses, saved room values and memberships, and a follow-up GET.
"""

import argparse
import pathlib
import re
import subprocess
import sys


CASES = (
    ("open create query name", "POST /rooms/opens", "room%5Bname%5D=Body", "room%5Bname%5D=Query"),
    ("open create query group", "POST /rooms/opens", "room%5Bname%5D=Body", "room%5Bfoo%5D=Query"),
    ("open create query only", "POST /rooms/opens", "", "room%5Bname%5D=Query"),
    ("open create query replaces scalar", "POST /rooms/opens", "room=bad", "room%5Bname%5D=Query"),
    ("closed create query members", "POST /rooms/closeds", "room%5Bname%5D=Team&user_ids%5B%5D=2", "user_ids%5B%5D=3"),
    ("closed update query name and members", "PATCH /rooms/closeds/1", "room%5Bname%5D=Body&user_ids%5B%5D=2", "room%5Bname%5D=Query&user_ids%5B%5D=3"),
    ("closed update query members", "PATCH /rooms/closeds/1", "room%5Bname%5D=Body&user_ids%5B%5D=2", "user_ids%5B%5D=3"),
    ("open update query group", "PATCH /rooms/opens/1", "room%5Bname%5D=Body", "room%5Bfoo%5D=Query"),
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--html-only", action="store_true")
    args = parser.parse_args()
    repo = pathlib.Path(__file__).resolve().parent.parent
    inventory = repo / "bench/paired_valid_write_route_inventory.py"
    failed = []
    for index, (label, route, body, query) in enumerate(CASES, 1):
        command = [sys.executable, str(inventory), "--filter", f"^{re.escape(route)}$", "--body", body, "--query", query]
        if not args.html_only:
            command.append("--all-accepts")
        result = subprocess.run(command, cwd=repo, text=True, capture_output=True)
        if result.returncode:
            print(f"{label}: paired comparison failed", flush=True)
            print(result.stdout[-2500:], flush=True)
            failed.append(label)
            continue
        count = 1 if args.html_only else 4
        kind = "submitted-form" if body else "empty-body"
        assert f"Matched {count}/{count} valid-CSRF {kind} route cases" in result.stdout, (label, result.stdout)
        print(f"Checked {index}/{len(CASES)}: {label}", flush=True)
    total = len(CASES) * (1 if args.html_only else 4)
    print(f"Matched {total - len(failed) * (1 if args.html_only else 4)}/{total} room query/form Accept cases")
    if failed:
        raise AssertionError(f"{len(failed)} form shapes differ: {failed}")


if __name__ == "__main__":
    main()
