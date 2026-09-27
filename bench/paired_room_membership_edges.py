"""Compare private-room membership form values with pinned Campfire.

Run after ``cargo build --release``. Each case uses fresh disposable databases
and compares four Accept headers, saved membership IDs, and a follow-up GET.
"""

import pathlib
import re
import subprocess
import sys


CREATE = "POST /rooms/closeds"
UPDATE = "PATCH /rooms/closeds/1"
NAME = "room%5Bname%5D=Team"
CASES = (
    ("single scalar", CREATE, "user_ids=1"),
    ("multiple list", CREATE, "user_ids%5B%5D=1&user_ids%5B%5D=2"),
    ("duplicate list", CREATE, "user_ids%5B%5D=1&user_ids%5B%5D=1"),
    ("blank list", CREATE, "user_ids%5B%5D="),
    ("missing user", CREATE, "user_ids%5B%5D=999"),
    ("comma scalar", CREATE, "user_ids=1%2C2"),
    ("suffix scalar", CREATE, "user_ids=2foo"),
    ("space scalar", CREATE, "user_ids=%202%20"),
    ("decimal scalar", CREATE, "user_ids=2.9"),
    ("exponent scalar", CREATE, "user_ids=1e2"),
    ("plus scalar", CREATE, "user_ids=%2B2"),
    ("nested hash", CREATE, "user_ids%5Bfoo%5D=1"),
    ("array of hashes", CREATE, "user_ids%5B%5D%5Bid%5D=1"),
    ("update multiple list", UPDATE, "user_ids%5B%5D=1&user_ids%5B%5D=2"),
    ("update comma scalar", UPDATE, "user_ids=1%2C2"),
    ("update leading junk", UPDATE, "user_ids=foo2"),
    ("update blank scalar", UPDATE, "user_ids="),
    ("update blank array", UPDATE, "user_ids%5B%5D="),
    ("update junk array", UPDATE, "user_ids%5B%5D=foo2"),
    ("update nested hash", UPDATE, "user_ids%5Bfoo%5D=1"),
    ("update mixed valid and junk", UPDATE, "user_ids%5B%5D=1&user_ids%5B%5D=foo2"),
    ("update missing user", UPDATE, "user_ids=999"),
    ("update omitted ids", UPDATE, ""),
)


def main():
    repo = pathlib.Path(__file__).resolve().parent.parent
    inventory = repo / "bench/paired_valid_write_route_inventory.py"
    for index, (label, route, ids) in enumerate(CASES, 1):
        result = subprocess.run(
            [sys.executable, str(inventory), "--all-accepts", "--filter", f"^{re.escape(route)}$", "--body", f"{NAME}&{ids}"],
            cwd=repo, text=True, capture_output=True,
        )
        if result.returncode:
            print(f"{label}: paired comparison failed", flush=True)
            print(result.stdout[-5000:], flush=True)
            print(result.stderr[-2000:], flush=True)
            raise SystemExit(result.returncode)
        assert "Matched 4/4 valid-CSRF submitted-form route cases" in result.stdout, (label, result.stdout)
        print(f"Checked {index}/{len(CASES)}: {label}", flush=True)
    print(f"Matched {len(CASES) * 4}/{len(CASES) * 4} room-membership form/Accept cases")


if __name__ == "__main__":
    main()
