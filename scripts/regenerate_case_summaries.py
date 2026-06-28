#!/usr/bin/env python3
"""Regenerate structured case intelligence and public case summaries."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

try:
    from scripts.case_intelligence import refresh_all_case_intelligence
    from scripts.validate_tracker_data import validate_cases, validate_pipeline_state, validate_updates
except ModuleNotFoundError:  # pragma: no cover - supports direct script execution.
    from case_intelligence import refresh_all_case_intelligence  # type: ignore
    from validate_tracker_data import validate_cases, validate_pipeline_state, validate_updates  # type: ignore


BASE_DIR = Path(__file__).resolve().parents[1]
DATA_DIR = BASE_DIR / "data"
CASES_PATH = DATA_DIR / "cases.json"
UPDATES_PATH = DATA_DIR / "updates.json"
LAST_RUN_PATH = DATA_DIR / "last_run.json"


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except json.JSONDecodeError:
        return default


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    tmp.replace(path)


def regenerate_cases(cases: list[dict[str, Any]], updates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    refresh_all_case_intelligence(cases, updates)
    return cases


def main() -> int:
    cases = load_json(CASES_PATH, [])
    updates = load_json(UPDATES_PATH, [])
    last_run = load_json(LAST_RUN_PATH, {})

    if not isinstance(cases, list):
        print("cases.json must contain a list.", file=sys.stderr)
        return 1
    if not isinstance(updates, list):
        print("updates.json must contain a list.", file=sys.stderr)
        return 1
    if not isinstance(last_run, dict):
        last_run = {}

    regenerate_cases(cases, updates)
    write_json(CASES_PATH, cases)

    errors = validate_cases(cases)
    errors.extend(validate_updates(updates))
    errors.extend(validate_pipeline_state(last_run))
    if errors:
        print("Regenerated case summaries, but tracker data validation failed:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1

    print(f"Regenerated case intelligence and public summaries for {len(cases)} cases.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
