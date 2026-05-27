#!/usr/bin/env python3
"""Pull new docket entries for active AI/IP cases."""

from __future__ import annotations

import html
import json
import os
import random
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parents[1]
DATA_DIR = BASE_DIR / "data"
CASES_PATH = DATA_DIR / "cases.json"
UPDATES_PATH = DATA_DIR / "updates.json"
LAST_RUN_PATH = DATA_DIR / "last_run.json"
COURTLISTENER_BASE = "https://www.courtlistener.com"
DOCKET_ENTRIES_URL = f"{COURTLISTENER_BASE}/api/rest/v4/docket-entries/"
MAX_RETRIES = 3
TIMEOUT = 30
COURTLISTENER_REQUEST_PAUSE_SECONDS = 4

RESOLUTION_SIGNALS = (
    "JUDGMENT",
    "DISMISSED WITH PREJUDICE",
    "SETTLED",
    "AFFIRMED",
    "REVERSED",
    "MANDATE ISSUED",
)


class RateLimitExceeded(RuntimeError):
    """Raised when an API keeps returning 429 after the required retries."""


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


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


def first_value(data: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        value = data.get(key)
        if value not in (None, "", []):
            return value
    return ""


def retry_after_seconds(response: requests.Response) -> float | None:
    value = response.headers.get("Retry-After")
    if not value:
        return None
    if value.isdigit():
        return float(value)
    try:
        retry_at = datetime.strptime(value, "%a, %d %b %Y %H:%M:%S %Z").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return max(0.0, (retry_at - datetime.now(timezone.utc)).total_seconds())


def backoff_sleep(attempt: int, response: requests.Response | None = None) -> None:
    retry_after = retry_after_seconds(response) if response is not None else None
    delay = retry_after if retry_after is not None else min(30 * (2**attempt), 180)
    delay += random.uniform(0, 0.5)
    time.sleep(delay)


def get_json(
    session: requests.Session,
    url: str,
    api_key: str,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    headers = {"Authorization": f"Token {api_key}"}
    for attempt in range(MAX_RETRIES + 1):
        response = session.get(url, headers=headers, params=params, timeout=TIMEOUT)
        if response.status_code == 429 or response.status_code >= 500:
            if attempt < MAX_RETRIES:
                backoff_sleep(attempt, response)
                continue
            if response.status_code == 429:
                raise RateLimitExceeded(f"CourtListener rate limit persisted for {url}")
        response.raise_for_status()
        return response.json()
    raise RuntimeError(f"Request failed after retries: {url}")


def normalize_entry_number(entry: dict[str, Any]) -> str:
    value = first_value(entry, ("entry_number", "entryNumber", "number", "id"))
    return clean_text(value)


def entry_date(entry: dict[str, Any]) -> str:
    return clean_text(first_value(entry, ("date_filed", "dateFiled", "date_entered", "dateEntered", "date")))


def entry_text(entry: dict[str, Any]) -> str:
    return clean_text(first_value(entry, ("description", "entry_text", "entryText", "text", "short_description")))


def fetch_entries(session: requests.Session, api_key: str, docket_id: str, last_run_date: str) -> list[dict[str, Any]]:
    time.sleep(COURTLISTENER_REQUEST_PAUSE_SECONDS + random.uniform(0, 1))
    params = {
        "docket": docket_id,
        "date_filed__gte": last_run_date,
        "order_by": "-entry_number",
        "page_size": 50,
    }
    data = get_json(session, DOCKET_ENTRIES_URL, api_key=api_key, params=params)
    return data.get("results", []) if isinstance(data.get("results"), list) else []


def last_run_date(last_run: dict[str, Any]) -> str:
    value = clean_text(last_run.get("last_run_date"))
    if value:
        return value
    return (utc_now().date() - timedelta(days=5)).isoformat()


def needs_review(raw_text: str) -> bool:
    upper = raw_text.upper()
    return any(signal in upper for signal in RESOLUTION_SIGNALS)


def main() -> None:
    load_dotenv()
    cases = load_json(CASES_PATH, [])
    updates = load_json(UPDATES_PATH, [])
    last_run = load_json(LAST_RUN_PATH, {})

    if not isinstance(cases, list):
        cases = []
    if not isinstance(updates, list):
        updates = []
    if not isinstance(last_run, dict):
        last_run = {}

    active_cases = [
        case
        for case in cases
        if case.get("status") == "active" and clean_text(case.get("courtlistener_docket_id"))
    ]
    if not active_cases:
        last_run["entries_updated"] = 0
        last_run["docket_update_complete"] = True
        write_json(LAST_RUN_PATH, last_run)
        print("Updated 0 entries across 0 cases.")
        return

    api_key = os.environ.get("COURTLISTENER_API_KEY")
    if not api_key:
        raise SystemExit("Missing required environment variable: COURTLISTENER_API_KEY")

    session = requests.Session()
    since = last_run_date(last_run)
    new_updates: list[dict[str, Any]] = []
    changed_case_count = 0
    docket_update_complete = True
    now = utc_now().replace(microsecond=0).isoformat().replace("+00:00", "Z")

    for case in active_cases:
        docket_id = clean_text(case.get("courtlistener_docket_id"))
        existing_numbers = {
            clean_text(entry.get("entry_number"))
            for entry in case.get("docket_entries", [])
            if isinstance(entry, dict) and clean_text(entry.get("entry_number"))
        }
        case_new_entries = 0
        try:
            fetched_entries = fetch_entries(session, api_key, docket_id, since)
        except RateLimitExceeded as exc:
            docket_update_complete = False
            print(f"Warning: skipped docket update after CourtListener rate limit: {docket_id} ({exc})")
            continue
        for raw_entry in fetched_entries:
            number = normalize_entry_number(raw_entry)
            if not number or number in existing_numbers:
                continue
            raw_text = entry_text(raw_entry)
            new_entry = {
                "entry_number": number,
                "date": entry_date(raw_entry),
                "raw_text": raw_text,
                "summary": None,
                "significance": None,
            }
            case.setdefault("docket_entries", []).append(new_entry)
            existing_numbers.add(number)
            case_new_entries += 1
            if needs_review(raw_text):
                case["status"] = "needs_review"
            new_updates.append(
                {
                    "case_id": case.get("id"),
                    "case_name": case.get("name"),
                    "entry_date": new_entry["date"],
                    "summary": None,
                    "significance": None,
                    "entry_number": number,
                    "logged_at": now,
                }
            )
        if case_new_entries:
            changed_case_count += 1

    updates = new_updates + updates
    write_json(CASES_PATH, cases)
    write_json(UPDATES_PATH, updates)

    last_run["entries_updated"] = len(new_updates)
    last_run["docket_update_complete"] = docket_update_complete
    write_json(LAST_RUN_PATH, last_run)

    print(f"Updated {len(new_updates)} entries across {changed_case_count} cases.")


if __name__ == "__main__":
    main()
