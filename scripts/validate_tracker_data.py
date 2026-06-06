#!/usr/bin/env python3
"""Validate generated tracker data before publication."""

from __future__ import annotations

import html
import json
import os
import re
import sys
from pathlib import Path
from typing import Any


BASE_DIR = Path(__file__).resolve().parents[1]
DATA_DIR = BASE_DIR / "data"
CASES_PATH = DATA_DIR / "cases.json"
UPDATES_PATH = DATA_DIR / "updates.json"
LAST_RUN_PATH = DATA_DIR / "last_run.json"
EMPTY_ENTRY_SUMMARY_MARKERS = (
    "no docket entry text",
    "no entry text",
    "cannot summarize",
    "unable to summarize",
    "courtlistener recorded docket activity",
)


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        value = " ".join(clean_text(item) for item in value)
    elif isinstance(value, dict):
        value = " ".join(clean_text(item) for item in value.values())
    text = html.unescape(str(value))
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def normalize_sentence(sentence: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", sentence.lower()).strip()


def repeated_summary_text(value: Any) -> bool:
    text = clean_text(value)
    sentences = [sentence.strip() for sentence in re.findall(r"[^.!?]+(?:[.!?]+|$)", text) if clean_text(sentence)]
    if len(sentences) < 2:
        return False

    normalized = [normalize_sentence(sentence) for sentence in sentences]
    for left, right in zip(normalized, normalized[1:]):
        if left and left == right:
            return True

    for block_size in range(1, (len(normalized) // 2) + 1):
        if len(normalized) % block_size != 0:
            continue
        block = normalized[:block_size]
        if block and normalized == block * (len(normalized) // block_size):
            return True
    return False


def empty_entry_placeholder_summary(value: Any) -> bool:
    text = clean_text(value).lower()
    return bool(text) and any(marker in text for marker in EMPTY_ENTRY_SUMMARY_MARKERS)


def load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def validate_cases(cases: Any) -> list[str]:
    errors: list[str] = []
    if not isinstance(cases, list):
        return ["cases.json must contain a list."]

    seen_ids: set[str] = set()
    seen_dockets: set[tuple[str, str]] = set()
    for index, case in enumerate(cases):
        if not isinstance(case, dict):
            errors.append(f"cases[{index}] must be an object.")
            continue

        label = clean_text(case.get("name")) or f"cases[{index}]"
        case_id = clean_text(case.get("id"))
        if not case_id:
            errors.append(f"{label}: missing id.")
        elif case_id in seen_ids:
            errors.append(f"{label}: duplicate case id {case_id}.")
        seen_ids.add(case_id)

        court = clean_text(case.get("court"))
        docket_number = clean_text(case.get("docket_number"))
        docket_key = (court, docket_number)
        if docket_number and docket_key in seen_dockets:
            errors.append(f"{label}: duplicate docket number {docket_number} in {court or 'unknown court'}.")
        seen_dockets.add(docket_key)

        if repeated_summary_text(case.get("plain_language_summary")):
            errors.append(f"{label}: plain_language_summary repeats the same sentence block.")

        for entry in case.get("docket_entries", []):
            if not isinstance(entry, dict):
                continue
            entry_number = clean_text(entry.get("entry_number")) or "unknown"
            if not clean_text(entry.get("raw_text")):
                errors.append(f"{label} entry {entry_number}: empty raw_text entries must not be published.")
            if not clean_text(entry.get("summary")):
                errors.append(f"{label} entry {entry_number}: missing public summary.")
            if repeated_summary_text(entry.get("summary")):
                errors.append(f"{label} entry {entry_number}: summary repeats the same sentence block.")
            if empty_entry_placeholder_summary(entry.get("summary")):
                errors.append(f"{label} entry {entry_number}: summary exposes missing docket text.")

        for ruling in case.get("key_rulings", []):
            if isinstance(ruling, dict) and repeated_summary_text(ruling.get("summary")):
                ruling_date = clean_text(ruling.get("date")) or "unknown date"
                errors.append(f"{label} ruling {ruling_date}: summary repeats the same sentence block.")

    return errors


def validate_updates(updates: Any) -> list[str]:
    errors: list[str] = []
    if not isinstance(updates, list):
        return ["updates.json must contain a list."]

    for index, update in enumerate(updates):
        if not isinstance(update, dict):
            errors.append(f"updates[{index}] must be an object.")
            continue
        if not clean_text(update.get("summary")):
            case_name = clean_text(update.get("case_name")) or f"updates[{index}]"
            entry_number = clean_text(update.get("entry_number")) or "unknown"
            errors.append(f"{case_name} update {entry_number}: missing public summary.")
        if repeated_summary_text(update.get("summary")):
            case_name = clean_text(update.get("case_name")) or f"updates[{index}]"
            entry_number = clean_text(update.get("entry_number")) or "unknown"
            errors.append(f"{case_name} update {entry_number}: summary repeats the same sentence block.")
        if empty_entry_placeholder_summary(update.get("summary")):
            case_name = clean_text(update.get("case_name")) or f"updates[{index}]"
            entry_number = clean_text(update.get("entry_number")) or "unknown"
            errors.append(f"{case_name} update {entry_number}: summary exposes missing docket text.")
    return errors


def validate_pipeline_state(last_run: Any) -> list[str]:
    errors: list[str] = []
    if not isinstance(last_run, dict):
        return ["last_run.json must contain an object."]
    if last_run.get("courtlistener_rate_limited"):
        message = "CourtListener rate-limited this run; valid fetched data will publish and the missed window will be retried."
        if os.environ.get("GITHUB_ACTIONS"):
            print(f"::warning::{message}", file=sys.stderr)
        else:
            print(f"Warning: {message}", file=sys.stderr)
    if last_run.get("docket_entry_cap_reached"):
        deferred = clean_text(last_run.get("summaries_deferred")) or "some"
        cap = clean_text(last_run.get("max_summaries_per_run")) or "configured"
        message = (
            f"Summary cap reached; {deferred} docket entries were deferred after the "
            f"{cap}-summary run budget and will be retried."
        )
        if os.environ.get("GITHUB_ACTIONS"):
            print(f"::warning::{message}", file=sys.stderr)
        else:
            print(f"Warning: {message}", file=sys.stderr)
    return errors


def main() -> int:
    errors = validate_cases(load_json(CASES_PATH))
    errors.extend(validate_updates(load_json(UPDATES_PATH)))
    errors.extend(validate_pipeline_state(load_json(LAST_RUN_PATH)))
    if errors:
        print("Tracker data validation failed:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1
    print("Tracker data validation passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
