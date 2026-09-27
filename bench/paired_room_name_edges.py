"""Compare room-name form shapes and saved room state with pinned Campfire.

Run after ``cargo build --release``. Each case uses fresh disposable databases
and checks four Accept headers through paired_valid_write_route_inventory.py.
"""

import pathlib
import re
import subprocess
import sys


CASES = (
    ("open blank create", "POST /rooms/opens", "room%5Bname%5D="),
    ("open padded create", "POST /rooms/opens", "room%5Bname%5D=++"),
    ("open omitted create", "POST /rooms/opens", "room%5Bunknown%5D=x"),
    ("open scalar create", "POST /rooms/opens", "room=scalar"),
    ("open array create", "POST /rooms/opens", "room%5B%5D=x"),
    ("open blank update", "PATCH /rooms/opens/1", "room%5Bname%5D="),
    ("open padded update", "PATCH /rooms/opens/1", "room%5Bname%5D=++"),
    ("open omitted update", "PATCH /rooms/opens/1", "room%5Bunknown%5D=x"),
    ("open scalar update", "PATCH /rooms/opens/1", "room=scalar"),
    ("open array update", "PATCH /rooms/opens/1", "room%5B%5D=x"),
    ("closed blank create", "POST /rooms/closeds", "room%5Bname%5D="),
    ("closed named create", "POST /rooms/closeds", "room%5Bname%5D=Test"),
    ("closed omitted create", "POST /rooms/closeds", "room%5Bunknown%5D=x"),
    ("closed blank update", "PATCH /rooms/closeds/1", "room%5Bname%5D="),
    ("closed named update", "PATCH /rooms/closeds/1", "room%5Bname%5D=Test"),
    ("closed omitted update", "PATCH /rooms/closeds/1", "room%5Bunknown%5D=x"),
)


def main():
    repo = pathlib.Path(__file__).resolve().parent.parent
    inventory = repo / "bench/paired_valid_write_route_inventory.py"
    for index, (label, route, body) in enumerate(CASES, 1):
        result = subprocess.run(
            [sys.executable, str(inventory), "--all-accepts", "--filter", f"^{re.escape(route)}$", "--body", body],
            cwd=repo, text=True, capture_output=True,
        )
        if result.returncode:
            print(f"{label}: paired comparison failed", flush=True)
            print(result.stdout[-5000:], flush=True)
            print(result.stderr[-2000:], flush=True)
            raise SystemExit(result.returncode)
        assert "Matched 4/4 valid-CSRF submitted-form route cases" in result.stdout, (label, result.stdout)
        print(f"Checked {index}/{len(CASES)}: {label}", flush=True)
    print(f"Matched {len(CASES) * 4}/{len(CASES) * 4} room-name form/Accept cases")


if __name__ == "__main__":
    main()
