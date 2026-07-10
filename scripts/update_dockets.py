#!/usr/bin/env python3
"""Pull new docket entries for active AI/IP cases."""

from __future__ import annotations

import html
import json
import os
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

try:
    from scripts.cl_client import CourtListenerClient, RateLimitExceeded
except ImportError:  # pragma: no cover - direct script execution
    from cl_client import CourtListenerClient, RateLimitExceeded  # type: ignore


BASE_DIR = Path(__file__).resolve().parents[1]
DATA_DIR = BASE_DIR / "data"
CASES_PATH = DATA_DIR / "cases.json"
UPDATES_PATH = DATA_DIR / "updates.json"
LAST_RUN_PATH = DATA_DIR / "last_run.json"
COURTLISTENER_BASE = "https://www.courtlistener.com"
DOCKET_ENTRIES_URL = f"{COURTLISTENER_BASE}/api/rest/v4/docket-entries/"
COURTLISTENER_SEARCH_URL = f"{COURTLISTENER_BASE}/api/rest/v4/search/"
DEFAULT_MAX_SUMMARIES_PER_RUN = 100
DOCKET_REFETCH_OVERLAP_DAYS = 2
CHANGE_DETECTION_CHUNK_SIZE = 20
CHANGE_DETECTION_MAX_PAGES = 3

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


def normalize_entry_number(entry: dict[str, Any]) -> str:
    value = first_value(entry, ("entry_number", "entryNumber", "number", "id"))
    return clean_text(value)


def entry_date(entry: dict[str, Any]) -> str:
    return clean_text(first_value(entry, ("date_filed", "dateFiled", "date_entered", "dateEntered", "date")))


def entry_text(entry: dict[str, Any]) -> str:
    return clean_text(first_value(entry, ("description", "entry_text", "entryText", "text", "short_description")))


def fetch_new_entries(
    client: CourtListenerClient,
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
        try:
            data = client.get_json(next_url, params=params)
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


def batched_change_detection_enabled() -> bool:
    return (os.environ.get("CL_BATCHED_CHANGE_DETECTION") or "").strip().lower() in {"1", "true", "yes"}


def detect_active_docket_ids(
    client: CourtListenerClient,
    pollable_cases: list[dict[str, Any]],
    since_lookup: dict[str, str],
) -> set[str] | None:
    """Use the RECAP search API to find which tracked dockets have fresh entries.

    One fielded search query can cover ~20 dockets at once, so on quiet days
    the whole 40+ docket sweep costs a handful of requests instead of one per
    docket — freeing most of the daily quota for backfills and discovery.

    Returns the set of courtlistener_docket_ids with activity on/after each
    chunk's oldest checkpoint, or None on any error or unexpected response
    shape, in which case the caller falls back to the per-docket loop.
    False positives are harmless (that docket just gets polled normally);
    the fallback path exists because a silent false negative would not be.
    """
    docket_ids = [clean_text(case.get("courtlistener_docket_id")) for case in pollable_cases]
    docket_ids = [docket_id for docket_id in docket_ids if docket_id]
    if not docket_ids:
        return set()

    active: set[str] = set()
    for start in range(0, len(docket_ids), CHANGE_DETECTION_CHUNK_SIZE):
        chunk = docket_ids[start : start + CHANGE_DETECTION_CHUNK_SIZE]
        floors = [since_lookup.get(docket_id, "") for docket_id in chunk]
        floors = [floor for floor in floors if floor]
        if not floors:
            return None
        floor = min(floors)
        id_clause = " OR ".join(chunk)
        params: dict[str, Any] | None = {
            "q": f"docket_id:({id_clause}) AND entry_date_filed:[{floor} TO *]",
            "type": "r",
            "page_size": 50,
        }
        next_url = COURTLISTENER_SEARCH_URL
        seen_urls: set[str] = set()
        pages = 0
        while next_url and pages < CHANGE_DETECTION_MAX_PAGES:
            if next_url in seen_urls:
                break
            seen_urls.add(next_url)
            try:
                data = client.get_json(next_url, params=params)
            except RateLimitExceeded:
                raise
            except Exception as exc:  # Any API-shape surprise means: fall back.
                print(f"Warning: batched change detection failed ({exc}); falling back to per-docket polling.")
                return None
            results = data.get("results")
            if not isinstance(results, list):
                print("Warning: batched change detection returned unexpected shape; falling back.")
                return None
            for item in results:
                if not isinstance(item, dict):
                    continue
                hit = clean_text(first_value(item, ("docket_id", "docketId", "docket")))
                if hit:
                    active.add(hit)
            pages += 1
            next_value = clean_text(data.get("next"))
            if next_value.startswith("/"):
                next_url = f"{COURTLISTENER_BASE}{next_value}"
            else:
                next_url = next_value
            params = None
        if next_url and pages >= CHANGE_DETECTION_MAX_PAGES:
            # Too much activity to page through cheaply; treat the whole chunk
            # as active so nothing is skipped on a false negative.
            active.update(chunk)
    return active


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

    client = CourtListenerClient(api_key)
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

    # Optional cheap pre-pass: one batched search per ~20 dockets tells us
    # which dockets actually have new activity. Quiet dockets get their
    # checkpoints advanced without a per-docket request.
    active_docket_ids: set[str] | None = None
    if batched_change_detection_enabled():
        since_lookup = {
            clean_text(case.get("courtlistener_docket_id")): case_since_date(case, since)
            for case in pollable_cases
        }
        try:
            active_docket_ids = detect_active_docket_ids(client, pollable_cases, since_lookup)
        except RateLimitExceeded as exc:
            print(f"Warning: stopped docket update during change detection: {exc}")
            docket_update_complete = False
            courtlistener_rate_limited = True
            active_docket_ids = None
            pollable_cases = []
        if active_docket_ids is not None:
            quiet = 0
            for case in pollable_cases:
                docket_id = clean_text(case.get("courtlistener_docket_id"))
                if docket_id and docket_id not in active_docket_ids:
                    case["docket_last_checked"] = today
                    quiet += 1
            pollable_cases = [
                case
                for case in pollable_cases
                if clean_text(case.get("courtlistener_docket_id")) in active_docket_ids
            ]
            print(
                f"Change detection: {len(pollable_cases)} docket(s) with activity, "
                f"{quiet} quiet docket(s) advanced without polling."
            )

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
            client, docket_id, case_since_date(case, since), existing_numbers, budget
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
