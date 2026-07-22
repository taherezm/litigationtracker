#!/usr/bin/env python3
"""Run bounded resumable discovery passes for one workflow job."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any


BASE_DIR = Path(__file__).resolve().parents[1]
DATA_DIR = BASE_DIR / "data"
CASES_PATH = DATA_DIR / "cases.json"
LAST_RUN_PATH = DATA_DIR / "last_run.json"
DEFAULT_MAX_DISCOVERY_PASSES_PER_JOB = 20
DEFAULT_MAX_DISCOVERY_JOB_SECONDS = 2700


def load_json(path: Path, default: Any) -> Any:
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def load_last_run() -> dict[str, Any]:
    data = load_json(LAST_RUN_PATH, {})
    return data if isinstance(data, dict) else {}


def load_cases() -> list[dict[str, Any]]:
    data = load_json(CASES_PATH, [])
    if not isinstance(data, list):
        return []
    return [item for item in data if isinstance(item, dict)]


def write_last_run(last_run: dict[str, Any]) -> None:
    tmp = LAST_RUN_PATH.with_suffix(LAST_RUN_PATH.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(last_run, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    tmp.replace(LAST_RUN_PATH)


def snapshot_files(paths: tuple[Path, ...]) -> dict[Path, bytes | None]:
    snapshots: dict[Path, bytes | None] = {}
    for path in paths:
        try:
            snapshots[path] = path.read_bytes()
        except FileNotFoundError:
            snapshots[path] = None
    return snapshots


def restore_files(snapshots: dict[Path, bytes | None]) -> None:
    """Roll back a timed-out pass without rolling back its request ledger."""

    for path, content in snapshots.items():
        child_tmp = path.with_suffix(path.suffix + ".tmp")
        restore_tmp = path.with_suffix(path.suffix + ".runner-restore.tmp")
        child_tmp.unlink(missing_ok=True)
        restore_tmp.unlink(missing_ok=True)
        if content is None:
            path.unlink(missing_ok=True)
            continue
        restore_tmp.write_bytes(content)
        restore_tmp.replace(path)


def utc_today() -> date:
    return datetime.now(timezone.utc).date()


def positive_int_env(name: str, default: int) -> int:
    value = (os.environ.get(name) or "").strip()
    if not value:
        return default
    try:
        parsed = int(value)
    except ValueError:
        emit_warning(f"Invalid {name}={value!r}; using {default}.")
        return default
    if parsed < 1:
        emit_warning(f"{name} must be positive; using {default}.")
        return default
    return parsed


def non_negative_int(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def emit_warning(message: str) -> None:
    if os.environ.get("GITHUB_ACTIONS"):
        print(f"::warning::{message}")
    else:
        print(f"Warning: {message}")


def run_script(script: str, timeout_seconds: float) -> None:
    subprocess.run(
        [sys.executable, script], cwd=BASE_DIR, check=True, timeout=timeout_seconds
    )


def discovery_complete(last_run: dict[str, Any]) -> bool:
    return bool(last_run.get("discovery_complete")) and not isinstance(
        last_run.get("discovery_cursor"), dict
    )


def discovery_current(last_run: dict[str, Any]) -> bool:
    if not discovery_complete(last_run):
        return False
    try:
        checkpoint = date.fromisoformat(str(last_run.get("discovery_last_run_date") or ""))
    except ValueError:
        return False
    return checkpoint >= utc_today()


def progress_signature(last_run: dict[str, Any], cases: list[dict[str, Any]]) -> str:
    """Return stable state that must change when a resumable pass makes progress."""

    relevant_state = {
        "discovery_complete": bool(last_run.get("discovery_complete")),
        "discovery_last_run_date": last_run.get("discovery_last_run_date"),
        "discovery_phase": last_run.get("discovery_phase"),
        "discovery_cursor": last_run.get("discovery_cursor"),
        "discovery_queries_completed": last_run.get("discovery_queries_completed"),
        "discovery_queries_total": last_run.get("discovery_queries_total"),
        "discovery_rss_dockets_completed": last_run.get("discovery_rss_dockets_completed"),
        "discovery_rss_dockets_total": last_run.get("discovery_rss_dockets_total"),
        "rejected_dockets": last_run.get("rejected_dockets", []),
        "cases": cases,
    }
    return json.dumps(relevant_state, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def main() -> int:
    max_passes = positive_int_env(
        "MAX_DISCOVERY_PASSES_PER_JOB", DEFAULT_MAX_DISCOVERY_PASSES_PER_JOB
    )
    max_job_seconds = positive_int_env(
        "MAX_DISCOVERY_JOB_SECONDS", DEFAULT_MAX_DISCOVERY_JOB_SECONDS
    )
    started_at = time.monotonic()
    passes_run = 0
    total_cases_discovered = 0
    stop_reason = "pass_limit"

    # This flag describes the current job. Clear a previous job's rate-limit
    # result once here; an actual rate-limit result below stops the loop.
    last_run = load_last_run()
    if last_run.get("courtlistener_rate_limited"):
        last_run["courtlistener_rate_limited"] = False
        write_last_run(last_run)

    for pass_number in range(1, max_passes + 1):
        elapsed_seconds = time.monotonic() - started_at
        remaining_seconds = max_job_seconds - elapsed_seconds
        if remaining_seconds <= 0:
            stop_reason = "time_budget"
            emit_warning(
                f"Discovery job reached its {max_job_seconds}-second wall-clock budget; "
                "publishing cursor progress for the next scheduled run."
            )
            break

        before = progress_signature(load_last_run(), load_cases())
        snapshots = snapshot_files((CASES_PATH, LAST_RUN_PATH))
        print(
            f"Discovery pass {pass_number}/{max_passes} "
            f"({remaining_seconds:.0f}s job budget remaining)",
            flush=True,
        )
        try:
            run_script("scripts/discover_cases.py", remaining_seconds)
        except subprocess.TimeoutExpired:
            restore_files(snapshots)
            stop_reason = "time_budget"
            emit_warning(
                "Discovery child pass reached the job wall-clock budget. Its partial case and "
                "cursor writes were rolled back, the request ledger was preserved, and completed "
                "earlier passes will publish."
            )
            break
        passes_run += 1

        last_run = load_last_run()
        total_cases_discovered += non_negative_int(last_run.get("cases_discovered"))
        after = progress_signature(last_run, load_cases())

        if discovery_complete(last_run):
            if discovery_current(last_run):
                stop_reason = "complete"
                print("Discovery source sweep is current through today.")
                break
            if before == after:
                stop_reason = "no_progress"
                emit_warning(
                    "Discovery completed an older sweep without advancing durable state; stopping "
                    "this job to avoid an automatic retry loop."
                )
                break
            checkpoint = str(last_run.get("discovery_last_run_date") or "unknown")
            print(
                f"Discovery completed the anchored sweep through {checkpoint}; "
                "starting the next sweep so coverage reaches today."
            )
            continue

        if last_run.get("courtlistener_rate_limited"):
            stop_reason = "rate_limit"
            emit_warning(
                "CourtListener rate-limited discovery; publishing the exact saved cursor for the "
                "next scheduled run."
            )
            break

        incomplete_reason = str(last_run.get("discovery_incomplete_reason") or "")
        if incomplete_reason == "classification":
            stop_reason = "classification"
            emit_warning(
                "Discovery has durable pending classifier work; deferring retries to the next "
                "scheduled run instead of repeatedly calling the provider."
            )
            break

        if not (
            last_run.get("discovery_candidate_cap_reached")
            and incomplete_reason == "candidate_cap"
        ):
            stop_reason = incomplete_reason or "incomplete"
            emit_warning(
                "Discovery stopped for a non-cap reason; publishing durable progress and "
                "deferring retries to the next scheduled run. "
                f"Reason: {stop_reason}."
            )
            break

        if before == after:
            stop_reason = "no_progress"
            emit_warning(
                "Discovery made no durable progress in the last pass; stopping this job to avoid "
                "an automatic retry loop."
            )
            break
    else:
        emit_warning(
            f"Discovery is not current after {max_passes} automatic passes; publishing cursor "
            "progress for the next scheduled run."
        )

    last_run = load_last_run()
    last_run["cases_discovered"] = total_cases_discovered
    last_run["discovery_passes_run"] = passes_run
    last_run["max_discovery_passes_per_job"] = max_passes
    last_run["discovery_job_stop_reason"] = stop_reason
    write_last_run(last_run)
    print(
        f"Discovery job ran {passes_run} completed pass(es), discovered "
        f"{total_cases_discovered} case(s), and stopped because: {stop_reason}."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
