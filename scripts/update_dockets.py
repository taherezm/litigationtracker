#!/usr/bin/env python3
"""Pull new docket entries for active AI/IP cases."""

from __future__ import annotations

import html
import json
import os
import random
import re
import time
from datetime import date, datetime, timedelta, timezone
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
COURTLISTENER_BASE_BACKOFF_SECONDS = 10
COURTLISTENER_MAX_RETRY_AFTER_SECONDS = 30
DEFAULT_MAX_SUMMARIES_PER_RUN = 100
DOCKET_REFETCH_OVERLAP_DAYS = 2

RESOLVED_PATTERNS = (
    r"\bdismissed with prejudice\b",
    r"\b(?:case|action|complaint) (?:is |was |hereby )?dismissed\b",
    r"\border of dismissal\b",
    r"\bnotice of voluntary dismissal\b",
    r"\bclerk'?s judgment\b",
    r"\b(?:final )?judgment (?:is |was |has been )?(?:entered|affirmed|reversed)\b",
    r"\b(?:entered|enters) judgment\b",
    r"\bmandate issued\b",
    r"\bnotice of settlement\b",
    r"\bcase (?:has been |is |was )?settled\b",
    r"\bsettlement (?:has been |was |is )?(?:reached|approved)\b",
)
STAYED_PATTERNS = (
    r"\bcase (?:is |was |hereby )?stayed\b",
    r"\b(?:all )?(?:proceedings|deadlines|discovery|action) (?:are|is|be|shall be) (?:hereby )?stayed\b",
    r"\bstaying (?:this )?(?:case|action|proceedings|deadlines|discovery)\b",
    r"\border(?:ed)?[^.]{0,120}\bstay(?:ing|ed)? (?:this )?(?:case|action|proceedings|deadlines|discovery)\b",
)
STAY_LIFTED_PATTERNS = (
    r"\bstay (?:is |was |has been )?(?:lifted|terminated|vacated|dissolved)\b",
    r"\b(?:lifts|lifted|terminates|terminated|vacates|vacated|dissolves|dissolved) (?:the )?stay\b",
    r"\bcase (?:is |was |has been )?reopened\b",
)
SUMMARY_JUDGMENT_PATTERNS = (
    r"\b(?:filed|moves?|moved) (?:a |the )?motion for summary judgment\b",
    r"\bsummary judgment motion\b",
)
APPEAL_PATTERNS = (
    r"\bappellant\b",
    r"\bappellee\b",
    r"\bnotice of appeal\b",
    r"\bappeal pending\b",
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


def max_summaries_per_run() -> int:
    value = (os.environ.get("MAX_SUMMARIES_PER_RUN") or "").strip()
    if not value:
        return DEFAULT_MAX_SUMMARIES_PER_RUN
    try:
        parsed = int(value)
    except ValueError:
        print(
            f"Warning: invalid MAX_SUMMARIES_PER_RUN={value!r}; "
            f"using {DEFAULT_MAX_SUMMARIES_PER_RUN}."
        )
        return DEFAULT_MAX_SUMMARIES_PER_RUN
    if parsed < 1:
        print(
            f"Warning: MAX_SUMMARIES_PER_RUN must be positive; "
            f"using {DEFAULT_MAX_SUMMARIES_PER_RUN}."
        )
        return DEFAULT_MAX_SUMMARIES_PER_RUN
    return parsed


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
    if retry_after is not None:
        delay = min(retry_after, COURTLISTENER_MAX_RETRY_AFTER_SECONDS)
    else:
        delay = min(COURTLISTENER_BASE_BACKOFF_SECONDS * (2**attempt), 60)
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
        try:
            response = session.get(url, headers=headers, params=params, timeout=TIMEOUT)
        except requests.RequestException as exc:
            if attempt < MAX_RETRIES:
                backoff_sleep(attempt)
                continue
            raise RateLimitExceeded(f"CourtListener request failed for {url}: {exc}") from exc
        if response.status_code == 429:
            retry_after = retry_after_seconds(response)
            if retry_after is not None and retry_after > COURTLISTENER_MAX_RETRY_AFTER_SECONDS:
                raise RateLimitExceeded(
                    f"CourtListener asked for {int(retry_after)}s backoff on {url}; deferring to the next run"
                )
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


def fetch_new_entries(
    session: requests.Session,
    api_key: str,
    docket_id: str,
    since_date: str,
    existing_numbers: set[str],
    budget: int,
) -> tuple[list[dict[str, Any]], bool, bool]:
    """Fetch publishable docket entries newer than since_date that are not yet tracked.

    Returns (new_entries, fully_fetched, rate_limited). Pagination stops once
    `budget` new entries are collected so one backlogged docket cannot consume
    the whole run's API quota; the case checkpoint must not advance unless
    fully_fetched is True.
    """
    params: dict[str, Any] | None = {
        "docket": docket_id,
        "date_filed__gte": since_date,
        "order_by": "-entry_number",
        "page_size": 50,
    }
    new_entries: list[dict[str, Any]] = []
    seen_numbers: set[str] = set()
    next_url = DOCKET_ENTRIES_URL
    seen_urls: set[str] = set()
    while next_url:
        if next_url in seen_urls:
            break
        seen_urls.add(next_url)
        time.sleep(COURTLISTENER_REQUEST_PAUSE_SECONDS + random.uniform(0, 1))
        try:
            data = get_json(session, next_url, api_key=api_key, params=params)
        except RateLimitExceeded as exc:
            print(f"Warning: stopped docket update after CourtListener rate limit: {docket_id} ({exc})")
            return new_entries, False, True
        results = data.get("results")
        if isinstance(results, list):
            for item in results:
                if not isinstance(item, dict):
                    continue
                number = normalize_entry_number(item)
                if not number or number in existing_numbers or number in seen_numbers:
                    continue
                if not entry_text(item):
                    continue
                if len(new_entries) >= budget:
                    return new_entries, False, False
                seen_numbers.add(number)
                new_entries.append(item)
        next_value = clean_text(data.get("next"))
        if next_value.startswith("/"):
            next_url = f"{COURTLISTENER_BASE}{next_value}"
        else:
            next_url = next_value
        params = None
    return new_entries, True, False


def last_run_date(last_run: dict[str, Any]) -> str:
    value = clean_text(last_run.get("docket_last_run_date")) or clean_text(last_run.get("last_run_date"))
    if value:
        return value
    return (utc_now().date() - timedelta(days=5)).isoformat()


def parse_iso_date(value: Any) -> date | None:
    try:
        return datetime.strptime(clean_text(value), "%Y-%m-%d").date()
    except ValueError:
        return None


def case_since_date(case: dict[str, Any], global_since: str) -> str:
    checked = parse_iso_date(case.get("docket_last_checked"))
    if checked:
        return (checked - timedelta(days=DOCKET_REFETCH_OVERLAP_DAYS)).isoformat()
    entries = [entry for entry in case.get("docket_entries", []) if isinstance(entry, dict)]
    if entries:
        return global_since
    return clean_text(case.get("date_filed")) or global_since


def seed_missing_case_checkpoints(cases: list[dict[str, Any]], since: str) -> None:
    for case in cases:
        if not parse_iso_date(case.get("docket_last_checked")):
            case["docket_last_checked"] = since


def matches_any(patterns: tuple[str, ...], text: str) -> bool:
    return any(re.search(pattern, text, flags=re.IGNORECASE | re.DOTALL) for pattern in patterns)


def infer_case_status(raw_text: str) -> str | None:
    """Infer public case status from docket language without a manual review bucket."""
    if matches_any(RESOLVED_PATTERNS, raw_text):
        return "resolved"
    if matches_any(STAY_LIFTED_PATTERNS, raw_text):
        return "active"
    if matches_any(STAYED_PATTERNS, raw_text):
        return "stayed"
    return None


def infer_case_posture(raw_text: str, status: str | None = None) -> str | None:
    if status == "stayed":
        return "Stayed"
    if status == "resolved":
        if matches_any((r"\bsettlement\b", r"\bsettled\b"), raw_text):
            return "Settled"
        if matches_any((r"\bdismiss",), raw_text):
            return "Dismissed"
        return "Judgment"
    if matches_any(SUMMARY_JUDGMENT_PATTERNS, raw_text):
        return "Summary Judgment"
    if matches_any(APPEAL_PATTERNS, raw_text):
        return "Appeal"
    return None


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

    pollable_cases = [
        case
        for case in cases
        if case.get("status") != "resolved" and clean_text(case.get("courtlistener_docket_id"))
    ]
    if not pollable_cases:
        last_run["entries_updated"] = 0
        last_run["docket_update_complete"] = True
        last_run["docket_entry_cap_reached"] = False
        last_run["summaries_deferred"] = 0
        last_run["max_summaries_per_run"] = max_summaries_per_run()
        write_json(LAST_RUN_PATH, last_run)
        print("Updated 0 entries across 0 cases.")
        return

    api_key = (os.environ.get("COURTLISTENER_API_KEY") or "").strip()
    if not api_key:
        raise SystemExit("Missing required environment variable: COURTLISTENER_API_KEY")

    session = requests.Session()
    since = last_run_date(last_run)
    seed_missing_case_checkpoints(pollable_cases, since)
    summary_cap = max_summaries_per_run()
    new_updates: list[dict[str, Any]] = []
    changed_case_count = 0
    docket_update_complete = True
    courtlistener_rate_limited = False
    docket_entry_cap_reached = False
    now = utc_now().replace(microsecond=0).isoformat().replace("+00:00", "Z")
    today = utc_now().date().isoformat()

    # Stalest checkpoints first so a rate-limited run still rotates coverage
    # across every open docket instead of starving the cases listed last.
    pollable_cases.sort(key=lambda case: case_since_date(case, since))

    for case in pollable_cases:
        budget = summary_cap - len(new_updates)
        if budget <= 0:
            docket_update_complete = False
            docket_entry_cap_reached = True
            break
        docket_id = clean_text(case.get("courtlistener_docket_id"))
        existing_numbers = {
            clean_text(entry.get("entry_number"))
            for entry in case.get("docket_entries", [])
            if isinstance(entry, dict) and clean_text(entry.get("entry_number"))
        }
        fetched_entries, fully_fetched, rate_limited = fetch_new_entries(
            session, api_key, docket_id, case_since_date(case, since), existing_numbers, budget
        )
        case_new_entries = 0
        for raw_entry in fetched_entries:
            number = normalize_entry_number(raw_entry)
            raw_text = entry_text(raw_entry)
            new_entry = {
                "entry_number": number,
                "date": entry_date(raw_entry),
                "raw_text": raw_text,
                "summary": None,
                "significance": None,
            }
            case.setdefault("docket_entries", []).append(new_entry)
            case_new_entries += 1
            inferred_status = infer_case_status(raw_text)
            if inferred_status:
                case["status"] = inferred_status
            inferred_posture = infer_case_posture(raw_text, inferred_status or clean_text(case.get("status")))
            if inferred_posture:
                case["procedural_posture"] = inferred_posture
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
        if rate_limited:
            docket_update_complete = False
            courtlistener_rate_limited = True
            break
        if fully_fetched:
            case["docket_last_checked"] = today
        else:
            docket_update_complete = False
            docket_entry_cap_reached = True
            break

    updates = new_updates + updates
    write_json(CASES_PATH, cases)
    write_json(UPDATES_PATH, updates)

    last_run["entries_updated"] = len(new_updates)
    last_run["docket_update_complete"] = docket_update_complete
    last_run["courtlistener_rate_limited"] = bool(last_run.get("courtlistener_rate_limited")) or courtlistener_rate_limited
    last_run["docket_entry_cap_reached"] = docket_entry_cap_reached
    last_run["summaries_deferred"] = 0
    last_run["max_summaries_per_run"] = summary_cap
    write_json(LAST_RUN_PATH, last_run)

    if docket_entry_cap_reached:
        print(
            f"Warning: summary cap reached at {summary_cap}; "
            "remaining dockets resume from their saved checkpoints on the next pass."
        )
    print(f"Updated {len(new_updates)} entries across {changed_case_count} cases.")


if __name__ == "__main__":
    main()
