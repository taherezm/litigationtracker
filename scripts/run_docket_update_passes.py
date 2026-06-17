#!/usr/bin/env python3
"""Run bounded docket-update and summarization passes for one workflow job."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any


BASE_DIR = Path(__file__).resolve().parents[1]
LAST_RUN_PATH = BASE_DIR / "data" / "last_run.json"
DEFAULT_MAX_DOCKET_UPDATE_PASSES = 2


def load_last_run() -> dict[str, Any]:
    try:
        with LAST_RUN_PATH.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def write_last_run(last_run: dict[str, Any]) -> None:
    tmp = LAST_RUN_PATH.with_suffix(LAST_RUN_PATH.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(last_run, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    tmp.replace(LAST_RUN_PATH)


def non_negative_int(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def max_update_passes() -> int:
    value = (os.environ.get("MAX_DOCKET_UPDATE_PASSES") or "").strip()
    if not value:
        return DEFAULT_MAX_DOCKET_UPDATE_PASSES
    try:
        parsed = int(value)
    except ValueError:
        print(
            f"Warning: invalid MAX_DOCKET_UPDATE_PASSES={value!r}; "
            f"using {DEFAULT_MAX_DOCKET_UPDATE_PASSES}."
        )
        return DEFAULT_MAX_DOCKET_UPDATE_PASSES
    if parsed < 1:
        print(
            "Warning: MAX_DOCKET_UPDATE_PASSES must be positive; "
            f"using {DEFAULT_MAX_DOCKET_UPDATE_PASSES}."
        )
        return DEFAULT_MAX_DOCKET_UPDATE_PASSES
    return parsed


def emit_warning(message: str) -> None:
    if os.environ.get("GITHUB_ACTIONS"):
        print(f"::warning::{message}")
    else:
        print(f"Warning: {message}")


def run_script(script: str) -> None:
    subprocess.run([sys.executable, script], cwd=BASE_DIR, check=True)


def docket_caught_up(last_run: dict[str, Any]) -> bool:
    return bool(last_run.get("docket_update_complete", True)) and not bool(
        last_run.get("docket_entry_cap_reached", False)
    )


def main() -> int:
    max_passes = max_update_passes()
    total_entries_updated = 0
    total_summaries_generated = 0
    caught_up = False
    stopped_on_rate_limit = False

    # The flag describes this run only; clear any carryover from a previous
    # run so one throttled job does not mark every later job as rate-limited.
    last_run = load_last_run()
    if last_run.get("courtlistener_rate_limited"):
        last_run["courtlistener_rate_limited"] = False
        write_last_run(last_run)

    for pass_number in range(1, max_passes + 1):
        print(f"Docket update pass {pass_number}/{max_passes}")
        run_script("scripts/update_dockets.py")
        last_run = load_last_run()
        total_entries_updated += non_negative_int(last_run.get("entries_updated"))

        run_script("scripts/summarize.py")
        last_run = load_last_run()
        total_summaries_generated += non_negative_int(last_run.get("summaries_generated"))

        if docket_caught_up(last_run):
            caught_up = True
            print("Docket checkpoint caught up.")
            break

        if last_run.get("courtlistener_rate_limited"):
            stopped_on_rate_limit = True
            emit_warning(
                "CourtListener rate-limited docket polling; publishing summarized data and retrying the "
                "remaining window on the next run."
            )
            break

    if not caught_up and not stopped_on_rate_limit:
        emit_warning(
            f"Docket update still incomplete after {max_passes} passes; publishing summarized data and "
            "retrying the remaining window on the next run."
        )

    last_run = load_last_run()
    last_run["entries_updated"] = total_entries_updated
    last_run["summaries_generated"] = total_summaries_generated
    last_run["max_docket_update_passes"] = max_passes
    write_last_run(last_run)
    print(
        f"Docket update passes generated {total_summaries_generated} summaries "
        f"for {total_entries_updated} new entries."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
