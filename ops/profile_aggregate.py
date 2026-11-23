"""Fail-closed, single-use Foundation profiling receipt aggregation (not acceptance)."""

from __future__ import annotations

import json
import os
import pathlib
import re
import sys

EXPECTED = {
    "ordinary": "current-head-excluding-extracted-historical",
    "historical": "immutable-0040-selected-nodes-on-isolated-postgres",
    "compat": "full-US1-US2-US3-six-successors-zero-skips",
}


def validate(root: pathlib.Path, env: dict[str, str]) -> None:
    """Require exact outcomes and one matching receipt per mandatory runner."""
    if env.get("INPUT_RESULT") != "success" or env.get("LANES_RESULT") != "success":
        raise ValueError(
            "mandatory inputs/build or runner failed, skipped or cancelled"
        )
    if (
        env.get("GITHUB_EVENT_NAME") != "workflow_dispatch"
        or env.get("GITHUB_REF")
        != "refs/heads/feature/foundation-parallel-profile-20261123"
        or not re.fullmatch(r"[0-9a-f]{40}", env.get("GITHUB_SHA", ""))
        or not env.get("GITHUB_RUN_ID", "").isdigit()
        or not env.get("GITHUB_RUN_ATTEMPT", "").isdigit()
    ):
        raise ValueError("unverified workflow/ref/SHA/run context")

    files = list(root.glob("**/profile-proof.json"))
    if len(files) != len(EXPECTED):
        raise ValueError("missing or duplicated mandatory lane evidence")
    seen: set[str] = set()
    for file in files:
        try:
            item = json.loads(file.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError) as error:
            raise ValueError("invalid lane evidence") from error
        if not isinstance(item, dict):
            raise TypeError("invalid evidence shape")
        lane = item.get("lane")
        if lane not in EXPECTED or lane in seen:
            raise ValueError("unexpected or duplicate lane")
        expected = {
            "schema": 1,
            "kind": "PROFILING_ONLY_NOT_ACCEPTANCE",
            "lane": lane,
            "sha": env["GITHUB_SHA"],
            "run_id": env["GITHUB_RUN_ID"],
            "attempt": env["GITHUB_RUN_ATTEMPT"],
            "status": "PASS",
            "coverage": EXPECTED[lane],
            "cleanup": "completed-by-checked-shell-trap",
        }
        if item != expected:
            raise ValueError("wrong candidate/run/coverage/outcome/cleanup evidence")
        seen.add(lane)
    if seen != set(EXPECTED):
        raise ValueError("incomplete mandatory lanes")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("FAIL profiling aggregate: expected one receipt directory")
    try:
        validate(pathlib.Path(sys.argv[1]), dict(os.environ))
    except (TypeError, ValueError) as error:
        raise SystemExit(f"FAIL profiling aggregate: {error}") from None
    print(
        "PROFILE_AGGREGATE status=PASS lanes=ordinary,historical,compat sha="
        + os.environ["GITHUB_SHA"]
    )
    print("EXPERIMENT ONLY: no Foundation acceptance or production gate established")
