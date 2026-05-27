#!/usr/bin/env python3
"""Discover newly filed AI/IP federal cases through CourtListener."""

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

import feedparser
import requests
from anthropic import Anthropic
from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parents[1]
DATA_DIR = BASE_DIR / "data"
CASES_PATH = DATA_DIR / "cases.json"
LAST_RUN_PATH = DATA_DIR / "last_run.json"
COURTLISTENER_BASE = "https://www.courtlistener.com"
COURTLISTENER_SEARCH_URL = f"{COURTLISTENER_BASE}/api/rest/v4/search/"
COURTHOUSE_NEWS_FEED = "https://www.courthousenews.com/feed/"
MODEL = "claude-sonnet-4-20250514"
MAX_RETRIES = 3
TIMEOUT = 30
COURTLISTENER_REQUEST_PAUSE_SECONDS = 4
COURTLISTENER_BASE_BACKOFF_SECONDS = 10
DEFAULT_MAX_DISCOVERY_CANDIDATES = 40

SEARCH_QUERIES = [
    '"generative AI" copyright',
    '"training data" infringement',
    '"large language model" copyright',
    '"artificial intelligence" "copyright infringement"',
    '"AI" "training data" patent',
    '"stable diffusion" copyright',
    '"ChatGPT" infringement',
    '"OpenAI" copyright',
    '"Anthropic" copyright',
    '"Google" "Gemini" copyright',
    '"right of publicity" "artificial intelligence"',
    '"AI-generated" copyright',
    '"deep learning" "trade secret"',
    '"machine learning" patent infringement',
    '"DMCA" "artificial intelligence"',
    'copyright "foundation model"',
    '"text and data mining" copyright',
    '"synthetic data" copyright',
]

RSS_TERMS = (
    "ai",
    "artificial intelligence",
    "copyright",
    "patent",
    "openai",
    "generative",
)

DOCKET_RE = re.compile(r"\b\d:\d{2}-[a-z]{2}-\d{4,6}\b", re.IGNORECASE)


class RateLimitExceeded(RuntimeError):
    """Raised when an API keeps returning 429 after the required retries."""


def utc_today() -> datetime.date:
    return datetime.now(timezone.utc).date()


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


def require_env(name: str) -> str:
    value = (os.environ.get(name) or "").strip()
    if not value:
        raise SystemExit(f"Missing required environment variable: {name}")
    return value


def max_discovery_candidates() -> int:
    value = (os.environ.get("MAX_DISCOVERY_CANDIDATES") or "").strip()
    if not value:
        return DEFAULT_MAX_DISCOVERY_CANDIDATES
    try:
        return max(0, int(value))
    except ValueError:
        return DEFAULT_MAX_DISCOVERY_CANDIDATES


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
    delay = retry_after if retry_after is not None else min(COURTLISTENER_BASE_BACKOFF_SECONDS * (2**attempt), 60)
    delay += random.uniform(0, 0.5)
    time.sleep(delay)


def get_json(
    session: requests.Session,
    url: str,
    api_key: str | None = None,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    headers = {"Authorization": f"Token {api_key}"} if api_key else {}
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


def anthropic_message(client: Anthropic, prompt: str, max_tokens: int) -> str:
    for attempt in range(MAX_RETRIES + 1):
        try:
            message = client.messages.create(
                model=MODEL,
                max_tokens=max_tokens,
                messages=[{"role": "user", "content": prompt}],
            )
            parts: list[str] = []
            for block in message.content:
                text = getattr(block, "text", None)
                if text:
                    parts.append(text)
                elif isinstance(block, dict) and block.get("text"):
                    parts.append(str(block["text"]))
            return "".join(parts).strip()
        except Exception as exc:  # Anthropic exceptions vary across SDK releases.
            status = getattr(exc, "status_code", None)
            if (status == 429 or (isinstance(status, int) and status >= 500)) and attempt < MAX_RETRIES:
                backoff_sleep(attempt)
                continue
            raise
    raise RuntimeError("Anthropic request failed after retries")


def parse_json_object(text: str) -> dict[str, Any]:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?", "", stripped, flags=re.IGNORECASE).strip()
        stripped = re.sub(r"```$", "", stripped).strip()
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start != -1 and end != -1 and end > start:
        stripped = stripped[start : end + 1]
    return json.loads(stripped)


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


def extract_docket_id(data: dict[str, Any]) -> str:
    value = first_value(data, ("courtlistener_docket_id", "docket_id", "docketId", "docket", "id"))
    if isinstance(value, dict):
        value = first_value(value, ("id", "pk", "resource_uri", "absolute_url", "url"))
    text = clean_text(value)
    match = re.search(r"(\d+)(?:/)?$", text)
    return match.group(1) if match else text


def normalize_docket_number(value: Any) -> str:
    return re.sub(r"\s+", " ", clean_text(value)).strip()


def docket_key(value: Any) -> str:
    return normalize_docket_number(value).lower()


def courtlistener_url(docket_id: str, *sources: dict[str, Any]) -> str:
    for source in sources:
        value = first_value(source, ("courtlistener_url", "absolute_url", "url", "resource_uri"))
        text = clean_text(value)
        if not text:
            continue
        if text.startswith("http"):
            return text
        if text.startswith("/"):
            return f"{COURTLISTENER_BASE}{text}"
    return f"{COURTLISTENER_BASE}/docket/{docket_id}/"


def split_parties(case_name: str, fallback: str = "") -> dict[str, str]:
    text = clean_text(case_name)
    match = re.split(r"\s+v\.?\s+", text, maxsplit=1, flags=re.IGNORECASE)
    if len(match) == 2:
        return {"plaintiff": match[0].strip(" ,;"), "defendant": match[1].strip(" ,;")}
    fallback_text = clean_text(fallback)
    return {"plaintiff": fallback_text, "defendant": ""}


def extract_judges(docket: dict[str, Any]) -> list[str]:
    values: list[str] = []
    for key in ("assigned_to_str", "referred_to_str", "judge", "judges", "assigned_to", "referred_to"):
        value = docket.get(key)
        if isinstance(value, list):
            for item in value:
                values.append(clean_text(item))
        elif isinstance(value, dict):
            values.append(clean_text(first_value(value, ("name_full", "name", "display_name", "short_name"))))
        else:
            values.append(clean_text(value))
    judges: list[str] = []
    for value in values:
        if value and not value.isdigit() and value not in judges:
            judges.append(value)
    return judges


def slugify(case_name: str, docket_number: str, existing_ids: set[str]) -> str:
    base = re.sub(r"\bv\.?\b", " v ", case_name.lower())
    slug = re.sub(r"[^a-z0-9]+", "-", base).strip("-")
    if not slug:
        slug = re.sub(r"[^a-z0-9]+", "-", docket_number.lower()).strip("-")
    slug = slug[:80].strip("-") or "case"
    candidate = slug
    suffix = 2
    while candidate in existing_ids:
        candidate = f"{slug}-{suffix}"
        suffix += 1
    existing_ids.add(candidate)
    return candidate


def result_to_candidate(result: dict[str, Any], source: str) -> dict[str, Any] | None:
    docket_number = normalize_docket_number(
        first_value(result, ("docket_number", "docketNumber", "docket_number_core", "docketNumberCore"))
    )
    docket_id = extract_docket_id(result)
    case_name = clean_text(
        first_value(result, ("caseNameFull", "case_name_full", "caseName", "case_name", "caseNameShort", "caption"))
    )
    if not docket_number or not docket_id or not case_name:
        return None
    return {
        "source": source,
        "raw": result,
        "docket_id": docket_id,
        "docket_number": docket_number,
        "case_name": case_name,
        "court": clean_text(first_value(result, ("court", "court_id", "court_citation_string", "courtCitationString"))),
        "date_filed": clean_text(first_value(result, ("dateFiled", "date_filed", "date_created", "dateCreated"))),
        "parties": clean_text(first_value(result, ("party", "parties", "party_name", "partyName"))),
        "snippet": clean_text(first_value(result, ("snippet", "description", "plain_text", "text"))),
    }


def search_cases(session: requests.Session, api_key: str, query: str, search_after: str | None) -> list[dict[str, Any]]:
    time.sleep(COURTLISTENER_REQUEST_PAUSE_SECONDS + random.uniform(0, 1))
    params: dict[str, Any] = {
        "q": query,
        "type": "d",
        "order_by": "score desc",
        "page_size": 20,
    }
    if search_after:
        params["filed_after"] = search_after
    data = get_json(session, COURTLISTENER_SEARCH_URL, api_key=api_key, params=params)
    return data.get("results", []) if isinstance(data.get("results"), list) else []


def fetch_docket(session: requests.Session, api_key: str, docket_id: str) -> dict[str, Any]:
    time.sleep(COURTLISTENER_REQUEST_PAUSE_SECONDS + random.uniform(0, 1))
    return get_json(session, f"{COURTLISTENER_BASE}/api/rest/v4/dockets/{docket_id}/", api_key=api_key)


def classify_case(client: Anthropic, candidate: dict[str, Any]) -> dict[str, Any]:
    prompt = f"""You are a legal classifier for an AI/IP litigation tracker.

Case: {candidate["case_name"]}
Court: {candidate.get("court", "")}
Docket: {candidate["docket_number"]}
Filed: {candidate.get("date_filed", "")}
Parties: {candidate.get("parties", "")}
Snippet: {candidate.get("snippet", "")}

Is this case primarily or substantially about intellectual property claims (copyright, patent, trade secret, trademark, or right of publicity) arising from or directly involving artificial intelligence systems, AI-generated content, or AI training data?

Respond ONLY with valid JSON, no preamble:
{{"relevant": true/false, "confidence": "high"/"medium"/"low", "reason": "one sentence", "claims": ["list"], "tags": ["from: training data, copyright, patent, LLM, image generation, music, news media, right of publicity, trade secret, DMCA, fair use, output similarity"]}}"""
    return parse_json_object(anthropic_message(client, prompt, max_tokens=300))


def generate_plain_language_summary(client: Anthropic, case_name: str, claims: list[str], parties: dict[str, str]) -> str:
    prompt = f"""Write exactly 2 sentences about this lawsuit for a general audience with a college reading level.
Sentence 1: What the plaintiff claims happened.
Sentence 2: Why this case matters for AI and intellectual property law.
Factual and neutral. No legal jargon.

Case: {case_name}, Claims: {claims}, Parties: {parties}"""
    return clean_text(anthropic_message(client, prompt, max_tokens=150))


def listify(value: Any) -> list[str]:
    if isinstance(value, list):
        values = value
    elif value:
        values = [value]
    else:
        values = []
    cleaned: list[str] = []
    for item in values:
        text = clean_text(item).lower()
        if text and text not in cleaned:
            cleaned.append(text)
    return cleaned


def build_case(
    candidate: dict[str, Any],
    docket: dict[str, Any],
    classification: dict[str, Any],
    client: Anthropic,
    existing_ids: set[str],
) -> dict[str, Any]:
    docket_number = normalize_docket_number(
        first_value(docket, ("docket_number", "docketNumber", "docket_number_core", "docketNumberCore"))
    ) or candidate["docket_number"]
    case_name = clean_text(
        first_value(docket, ("case_name_full", "caseNameFull", "case_name", "caseName", "case_name_short"))
    ) or candidate["case_name"]
    parties = split_parties(case_name, clean_text(first_value(docket, ("party", "parties"))) or candidate.get("parties", ""))
    claims = listify(classification.get("claims"))
    tags = listify(classification.get("tags"))
    docket_id = extract_docket_id(docket) or candidate["docket_id"]
    today = utc_today().isoformat()

    return {
        "id": slugify(case_name, docket_number, existing_ids),
        "name": case_name,
        "court": clean_text(first_value(docket, ("court", "court_id", "court_citation_string"))) or candidate.get("court", ""),
        "court_full": clean_text(first_value(docket, ("court_full_name", "court_name", "courtFullName"))) or candidate.get("court", ""),
        "docket_number": docket_number,
        "courtlistener_docket_id": str(docket_id),
        "date_filed": clean_text(first_value(docket, ("date_filed", "dateFiled"))) or candidate.get("date_filed", ""),
        "claims": claims,
        "legal_theories": [],
        "status": "active",
        "procedural_posture": "Filed",
        "parties": parties,
        "judges": extract_judges(docket),
        "key_rulings": [],
        "docket_entries": [],
        "plain_language_summary": generate_plain_language_summary(client, case_name, claims, parties),
        "tags": tags,
        "source": "courtlistener",
        "courtlistener_url": courtlistener_url(str(docket_id), docket, candidate.get("raw", {})),
        "last_updated": today,
        "discovered_date": today,
    }


def collect_rss_candidates(session: requests.Session, api_key: str) -> list[dict[str, Any]]:
    feed = feedparser.parse(COURTHOUSE_NEWS_FEED)
    candidates: list[dict[str, Any]] = []
    for entry in feed.entries:
        text = clean_text(f"{entry.get('title', '')} {entry.get('description', '')} {entry.get('summary', '')}")
        lowered = text.lower()
        if not any(term in lowered for term in RSS_TERMS):
            continue
        for docket_number in sorted(set(DOCKET_RE.findall(text))):
            try:
                results = search_cases(session, api_key, docket_number, search_after=None)
            except RateLimitExceeded as exc:
                print(f"Warning: skipped RSS docket lookup after rate limit: {docket_number} ({exc})")
                raise
            for result in results:
                candidate = result_to_candidate(result, f"rss:{COURTHOUSE_NEWS_FEED}")
                if candidate:
                    candidates.append(candidate)
    return candidates


def search_after_date(cases: list[dict[str, Any]], last_run: dict[str, Any]) -> str:
    last_run_date = clean_text(last_run.get("last_run_date"))
    if len(cases) < 5 or not last_run_date:
        return (utc_today() - timedelta(days=90)).isoformat()
    return last_run_date


def main() -> None:
    load_dotenv()
    courtlistener_key = require_env("COURTLISTENER_API_KEY")
    anthropic_key = require_env("ANTHROPIC_API_KEY")

    cases = load_json(CASES_PATH, [])
    if not isinstance(cases, list):
        cases = []
    last_run = load_json(LAST_RUN_PATH, {})
    if not isinstance(last_run, dict):
        last_run = {}

    known_dockets = {docket_key(case.get("docket_number")) for case in cases if case.get("docket_number")}
    existing_ids = {clean_text(case.get("id")) for case in cases if case.get("id")}
    search_after = search_after_date(cases, last_run)

    session = requests.Session()
    client = Anthropic(api_key=anthropic_key)
    candidates_by_docket: dict[str, dict[str, Any]] = {}
    discovery_complete = True

    for query in SEARCH_QUERIES:
        try:
            results = search_cases(session, courtlistener_key, query, search_after)
        except RateLimitExceeded as exc:
            discovery_complete = False
            print(f"Warning: skipped search query after CourtListener rate limit: {query} ({exc})")
            break
        for result in results:
            candidate = result_to_candidate(result, query)
            if not candidate:
                continue
            key = docket_key(candidate["docket_number"])
            if key not in known_dockets:
                candidates_by_docket.setdefault(key, candidate)

    try:
        rss_candidates = collect_rss_candidates(session, courtlistener_key)
    except RateLimitExceeded as exc:
        discovery_complete = False
        print(f"Warning: skipped RSS discovery after CourtListener rate limit ({exc})")
        rss_candidates = []

    for candidate in rss_candidates:
        key = docket_key(candidate["docket_number"])
        if key not in known_dockets:
            candidates_by_docket.setdefault(key, candidate)

    candidates = list(candidates_by_docket.values())
    limit = max_discovery_candidates()
    if limit and len(candidates) > limit:
        discovery_complete = False
        print(f"Warning: discovery candidate list capped at {limit} of {len(candidates)} candidates.")
        candidates = candidates[:limit]

    discovered: list[dict[str, Any]] = []
    for candidate in candidates:
        classification = classify_case(client, candidate)
        if not classification.get("relevant"):
            continue
        if clean_text(classification.get("confidence")).lower() not in {"high", "medium"}:
            continue
        try:
            docket = fetch_docket(session, courtlistener_key, candidate["docket_id"])
        except RateLimitExceeded as exc:
            discovery_complete = False
            print(f"Warning: skipped docket fetch after CourtListener rate limit: {candidate['docket_id']} ({exc})")
            break
        discovered.append(build_case(candidate, docket, classification, client, existing_ids))

    cases.extend(discovered)
    write_json(CASES_PATH, cases)

    last_run["cases_discovered"] = len(discovered)
    last_run["discovery_complete"] = discovery_complete
    write_json(LAST_RUN_PATH, last_run)

    print(f"Discovered {len(discovered)} new cases.")


if __name__ == "__main__":
    main()
