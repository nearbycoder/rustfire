"""Compare direct-room participant form values with pinned Campfire.

Run after ``cargo build --release``. Each case uses fresh disposable databases
and compares four Accept headers, saved membership IDs, and a follow-up GET.
"""

import argparse
import pathlib
import re
import subprocess
import sys


CASES = (
    ("single scalar", "user_ids=2"),
    ("multiple list", "user_ids%5B%5D=1&user_ids%5B%5D=2"),
    ("duplicate list", "user_ids%5B%5D=2&user_ids%5B%5D=2"),
    ("blank list", "user_ids%5B%5D="),
    ("missing list user", "user_ids%5B%5D=999"),
    ("suffix list", "user_ids%5B%5D=2foo"),
    ("space list", "user_ids%5B%5D=%202%20"),
    ("decimal list", "user_ids%5B%5D=2.9"),
    ("leading junk list", "user_ids%5B%5D=foo2"),
    ("blank scalar", "user_ids="),
    ("missing user", "user_ids=999"),
    ("comma scalar", "user_ids=1%2C2"),
    ("suffix scalar", "user_ids=2foo"),
    ("space scalar", "user_ids=%202%20"),
    ("decimal scalar", "user_ids=2.9"),
    ("exponent scalar", "user_ids=1e2"),
    ("plus scalar", "user_ids=%2B2"),
    ("leading junk", "user_ids=foo2"),
    ("nested hash", "user_ids%5Bfoo%5D=2"),
    ("array of hashes", "user_ids%5B%5D%5Bid%5D=2"),
)
QUERY_CASES = (
    ("query list", "", "user_ids%5B%5D=2"),
    ("query scalar", "", "user_ids=2"),
    ("query hash", "", "user_ids%5Bfoo%5D=2"),
    ("body and query lists", "user_ids%5B%5D=2", "user_ids%5B%5D=3"),
    ("body blank and query list", "user_ids%5B%5D=", "user_ids%5B%5D=3"),
    ("body list and query scalar", "user_ids%5B%5D=2", "user_ids=3"),
    ("body scalar and query list", "user_ids=2", "user_ids%5B%5D=3"),
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--html-only", action="store_true", help="use one Accept header while discovering differences")
    parser.add_argument("--query-only", action="store_true", help="check only query/body precedence cases")
    args = parser.parse_args()
    repo = pathlib.Path(__file__).resolve().parent.parent
    inventory = repo / "bench/paired_valid_write_route_inventory.py"
    failed = []
    all_cases = QUERY_CASES if args.query_only else tuple((label, body, "") for label, body in CASES) + QUERY_CASES
    for index, (label, body, query) in enumerate(all_cases, 1):
        command = [sys.executable, str(inventory), "--filter", re.escape("POST /rooms/directs") + "$", "--body", body]
        if query:
            command.extend(("--query", query))
        if not args.html_only:
            command.append("--all-accepts")
        result = subprocess.run(
            command,
            cwd=repo, text=True, capture_output=True,
        )
        if result.returncode:
            print(f"{label}: paired comparison failed", flush=True)
            print(result.stdout[-2500:], flush=True)
            failed.append(label)
            continue
        expected = 1 if args.html_only else 4
        form_kind = "submitted-form" if body else "empty-body"
        assert f"Matched {expected}/{expected} valid-CSRF {form_kind} route cases" in result.stdout, (label, result.stdout)
        print(f"Checked {index}/{len(all_cases)}: {label}", flush=True)
    total = len(all_cases) * (1 if args.html_only else 4)
    print(f"Matched {total - len(failed) * (1 if args.html_only else 4)}/{total} direct-room participant form/Accept cases")
    if failed:
        raise AssertionError(f"{len(failed)} form shapes differ: {failed}")


if __name__ == "__main__":
    main()
