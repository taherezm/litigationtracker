#!/usr/bin/env python3
"""Fetch and extract complaint source documents for case-level intelligence."""

from __future__ import annotations

import html
import json
import os
import random
import re
import time
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import Any

import requests

try:
    from scripts.case_intelligence import (
        extract_ai_conduct,
        extract_technology_or_model,
        extract_works_or_data,
    )
except ModuleNotFoundError:  # pragma: no cover - supports direct script execution.
    from case_intelligence import (  # type: ignore
        extract_ai_conduct,
        extract_technology_or_model,
        extract_works_or_data,
    )


BASE_DIR = Path(__file__).resolve().parents[1]
DATA_DIR = BASE_DIR / "data"
CASES_PATH = DATA_DIR / "cases.json"
UPDATES_PATH = DATA_DIR / "updates.json"
LAST_RUN_PATH = DATA_DIR / "last_run.json"

COURTLISTENER_BASE = "https://www.courtlistener.com"
COURTLISTENER_STORAGE_BASE = "https://storage.courtlistener.com"
RECAP_DOCUMENTS_URL = f"{COURTLISTENER_BASE}/api/rest/v4/recap-documents/"
MAX_RETRIES = 3
TIMEOUT = 30
COURTLISTENER_REQUEST_PAUSE_SECONDS = 4
COURTLISTENER_BASE_BACKOFF_SECONDS = 10
COURTLISTENER_MAX_RETRY_AFTER_SECONDS = 30
DEFAULT_MAX_SOURCE_DOCUMENT_CASES_PER_RUN = 8
DEFAULT_MAX_SOURCE_DOCUMENTS_PER_CASE = 2
DEFAULT_SOURCE_DOCUMENT_REFRESH_DAYS = 45
MAX_RECAP_DOCUMENTS_PER_CASE = 60
MAX_PDF_BYTES = 8 * 1024 * 1024
MAX_PDF_PAGES = 12
MAX_SOURCE_TEXT_CHARS = 50000
MAX_EXCERPT_CHARS = 900

COMPLAINT_PATTERNS = (
    r"\bcomplaint\b",
    r"\bclass action complaint\b",
    r"\bamended complaint\b",
    r"\bpetition\b",
    r"\bcounterclaim\b",
)
NON_COMPLAINT_PATTERNS = (
    r"\bsummons\b",
    r"\bcivil cover sheet\b",
    r"\bcorporate disclosure\b",
    r"\bnotice of appearance\b",
    r"\bpro hac vice\b",
    r"\bcertificate of interested\b",
    r"\bstanding order\b",
    r"\bcase assigned\b",
)
SOURCE_TERMS = (
    "artificial intelligence",
    "generative ai",
    "training data",
    "trained",
    "large language model",
    "llm",
    "machine learning",
    "openai",
    "anthropic",
    "chatgpt",
    "claude",
    "grok",
    "gemini",
    "stable diffusion",
    "copyright",
    "patent",
    "trade secret",
    "right of publicity",
    "scraping",
)


class RateLimitExceeded(RuntimeError):
    """Raised when CourtListener asks the pipeline to stop for this run."""


def utc_today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


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


def env_int(name: str, default: int, minimum: int = 0) -> int:
    value = (os.environ.get(name) or "").strip()
    if not value:
        return default
    try:
        parsed = int(value)
    except ValueError:
        print(f"Warning: invalid {name}={value!r}; using {default}.")
        return default
    if parsed < minimum:
        print(f"Warning: {name} must be at least {minimum}; using {default}.")
        return default
    return parsed


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


def download_pdf_bytes(session: requests.Session, filepath_local: str) -> bytes:
    path = clean_text(filepath_local)
    if not path:
        return b""
    url = path if path.startswith("http") else f"{COURTLISTENER_STORAGE_BASE}/{path.lstrip('/')}"
    try:
        response = session.get(url, timeout=TIMEOUT, stream=True)
        response.raise_for_status()
    except requests.RequestException:
        return b""
    content = bytearray()
    for chunk in response.iter_content(chunk_size=65536):
        if not chunk:
            continue
        content.extend(chunk)
        if len(content) > MAX_PDF_BYTES:
            return b""
    return bytes(content)


def extract_pdf_text(pdf_bytes: bytes) -> str:
    if not pdf_bytes:
        return ""
    try:
        from pypdf import PdfReader  # type: ignore
    except Exception:
        return ""
    try:
        reader = PdfReader(BytesIO(pdf_bytes))
        parts: list[str] = []
        for index, page in enumerate(reader.pages):
            if index >= MAX_PDF_PAGES:
                break
            parts.append(clean_text(page.extract_text()))
            if len(" ".join(parts)) >= MAX_SOURCE_TEXT_CHARS:
                break
    except Exception:
        return ""
    return clean_text(" ".join(parts))[:MAX_SOURCE_TEXT_CHARS]


def recap_document_url(document: dict[str, Any]) -> str | None:
    filepath = clean_text(document.get("filepath_local"))
    if filepath:
        if filepath.startswith("http"):
            return filepath
        return f"{COURTLISTENER_STORAGE_BASE}/{filepath.lstrip('/')}"
    resource_uri = clean_text(document.get("resource_uri"))
    return resource_uri or None


def document_number(document: dict[str, Any]) -> str:
    return clean_text(document.get("document_number")) or clean_text(document.get("pacer_doc_id"))


def attachment_number(document: dict[str, Any]) -> str:
    value = clean_text(document.get("attachment_number"))
    return "" if value in {"0", "None", "none", "null"} else value


def sentence_split(value: Any) -> list[str]:
    text = clean_text(value)
    return [sentence.strip() for sentence in re.findall(r"[^.!?]+(?:[.!?]+|$)", text) if clean_text(sentence)]


def term_in_text(text: str, term: str) -> bool:
    normalized = clean_text(term).lower()
    if not normalized:
        return False
    return bool(re.search(rf"(?<![a-z0-9]){re.escape(normalized)}(?![a-z0-9])", text))


def source_term_score(sentence: str) -> int:
    lowered = sentence.lower()
    score = 0
    for term in SOURCE_TERMS:
        if term_in_text(lowered, term):
            score += 1
    allegation_terms = ("allege", "assert", "claim", "contend", "accuse", "train", "scrap", "copy")
    if any(term in lowered for term in allegation_terms):
        score += 2
    return score


def allegation_excerpt(text: str) -> str:
    sentences = sentence_split(text)
    if not sentences:
        return ""
    ranked = sorted(
        enumerate(sentences[:240]),
        key=lambda item: (source_term_score(item[1]), -item[0]),
        reverse=True,
    )
    selected_indexes: list[int] = []
    for index, sentence in ranked:
        if source_term_score(sentence) <= 0:
            continue
        for candidate_index in (max(0, index - 1), index, min(len(sentences) - 1, index + 1)):
            if candidate_index not in selected_indexes:
                selected_indexes.append(candidate_index)
        if len(selected_indexes) >= 3:
            break
    if not selected_indexes:
        selected_indexes = [0]
    excerpt = clean_text(" ".join(sentences[index] for index in sorted(selected_indexes)))
    return excerpt[:MAX_EXCERPT_CHARS].rstrip(" ,;")


def document_score(document: dict[str, Any], text: str) -> int:
    description = clean_text(document.get("description"))
    haystack = f"{description} {text[:3000]}"
    score = 0
    if any(re.search(pattern, haystack, flags=re.IGNORECASE) for pattern in COMPLAINT_PATTERNS):
        score += 100
    if "amended complaint" in haystack.lower():
        score += 20
    if document_number(document) in {"1", "1.0"}:
        score += 15
    if not attachment_number(document):
        score += 5
    if any(re.search(pattern, haystack, flags=re.IGNORECASE) for pattern in NON_COMPLAINT_PATTERNS):
        score -= 80
    score += min(source_term_score(allegation_excerpt(text)) * 4, 40)
    return score


def document_text(session: requests.Session, document: dict[str, Any]) -> str:
    text = clean_text(document.get("plain_text"))
    if text:
        return text[:MAX_SOURCE_TEXT_CHARS]
    if document.get("is_available") is False:
        return ""
    pdf_bytes = download_pdf_bytes(session, clean_text(document.get("filepath_local")))
    return extract_pdf_text(pdf_bytes)


def normalized_source_document(session: requests.Session, document: dict[str, Any], checked_at: str) -> dict[str, Any] | None:
    text = document_text(session, document)
    excerpt = allegation_excerpt(text)
    description = clean_text(document.get("description")) or "Complaint"
    score = document_score(document, text)
    if score < 60:
        return None

    facts = {
        "ai_conduct_alleged": extract_ai_conduct(text),
        "works_or_data_at_issue": extract_works_or_data(text),
        "technology_or_model_at_issue": extract_technology_or_model(text),
    }
    return {
        "type": "complaint",
        "source": "courtlistener_recap",
        "recap_document_id": clean_text(document.get("id")) or None,
        "docket_entry_number": document_number(document) or None,
        "attachment_number": attachment_number(document) or None,
        "description": description,
        "courtlistener_url": recap_document_url(document),
        "ocr_status": clean_text(document.get("ocr_status")) or None,
        "page_count": document.get("page_count"),
        "extracted_at": checked_at,
        "text_excerpt": excerpt,
        "facts": facts,
    }


def fetch_recap_documents(session: requests.Session, api_key: str, docket_id: str) -> list[dict[str, Any]]:
    params: dict[str, Any] = {
        "docket_entry__docket": docket_id,
        "order_by": "document_number,attachment_number,id",
        "page_size": min(MAX_RECAP_DOCUMENTS_PER_CASE, 100),
        "fields": ",".join(
            (
                "id",
                "resource_uri",
                "description",
                "document_number",
                "attachment_number",
                "is_available",
                "filepath_local",
                "plain_text",
                "ocr_status",
                "page_count",
                "pacer_doc_id",
            )
        ),
    }
    time.sleep(COURTLISTENER_REQUEST_PAUSE_SECONDS + random.uniform(0, 1))
    data = get_json(session, RECAP_DOCUMENTS_URL, api_key=api_key, params=params)
    results = data.get("results")
    return [item for item in results if isinstance(item, dict)] if isinstance(results, list) else []


def dedupe_source_documents(documents: list[dict[str, Any]]) -> list[dict[str, Any]]:
    deduped: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for document in documents:
        key = (
            clean_text(document.get("recap_document_id")),
            clean_text(document.get("docket_entry_number")),
            clean_text(document.get("attachment_number")),
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(document)
    return deduped


def enrich_case_source_documents(
    case: dict[str, Any],
    session: requests.Session,
    api_key: str,
    *,
    checked_at: str | None = None,
    max_documents: int | None = None,
) -> bool:
    docket_id = clean_text(case.get("courtlistener_docket_id"))
    checked_at = checked_at or utc_today()
    if not docket_id:
        case["source_documents_status"] = "missing_docket_id"
        case["source_documents_last_checked"] = checked_at
        return False

    raw_documents = fetch_recap_documents(session, api_key, docket_id)
    source_documents: list[dict[str, Any]] = []
    for document in raw_documents:
        normalized = normalized_source_document(session, document, checked_at)
        if normalized:
            source_documents.append(normalized)

    source_documents.sort(
        key=lambda document: (
            clean_text(document.get("docket_entry_number")) != "1",
            clean_text(document.get("attachment_number")) or "0",
            clean_text(document.get("recap_document_id")),
        )
    )
    limit = max_documents if max_documents is not None else DEFAULT_MAX_SOURCE_DOCUMENTS_PER_CASE
    source_documents = dedupe_source_documents(source_documents)[:limit]
    case["source_documents"] = source_documents
    case["source_documents_status"] = "found" if source_documents else "not_found"
    case["source_documents_last_checked"] = checked_at
    return bool(source_documents)


def parse_iso_date(value: Any) -> datetime | None:
    text = clean_text(value)
    if not text:
        return None
    try:
        return datetime.strptime(text[:10], "%Y-%m-%d")
    except ValueError:
        return None


def source_documents_stale(case: dict[str, Any], refresh_days: int) -> bool:
    if not case.get("source_documents"):
        return True
    checked = parse_iso_date(case.get("source_documents_last_checked"))
    if checked is None:
        return True
    today = datetime.strptime(utc_today(), "%Y-%m-%d")
    return (today - checked).days >= refresh_days


def eligible_cases(cases: list[dict[str, Any]], *, force: bool, refresh_days: int) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    for case in cases:
        if not isinstance(case, dict):
            continue
        if clean_text(case.get("status")) == "resolved" and case.get("source_documents"):
            continue
        if force or source_documents_stale(case, refresh_days):
            selected.append(case)
    selected.sort(
        key=lambda case: (
            bool(case.get("source_documents")),
            clean_text(case.get("source_documents_last_checked")) or "0000-00-00",
            clean_text(case.get("date_filed")) or "9999-99-99",
        )
    )
    return selected


def enrich_cases(
    cases: list[dict[str, Any]],
    *,
    api_key: str,
    max_cases: int,
    max_documents_per_case: int,
    force: bool = False,
    refresh_days: int = DEFAULT_SOURCE_DOCUMENT_REFRESH_DAYS,
) -> tuple[int, bool]:
    session = requests.Session()
    checked_at = utc_today()
    enriched = 0
    rate_limited = False
    for case in eligible_cases(cases, force=force, refresh_days=refresh_days)[:max_cases]:
        try:
            if enrich_case_source_documents(
                case,
                session,
                api_key,
                checked_at=checked_at,
                max_documents=max_documents_per_case,
            ):
                enriched += 1
        except RateLimitExceeded as exc:
            case["source_documents_status"] = "rate_limited"
            print(f"Warning: stopped source document enrichment after CourtListener rate limit: {exc}")
            rate_limited = True
            break
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else None
            case["source_documents_status"] = f"http_{status}" if status else "http_error"
            case["source_documents_last_checked"] = checked_at
            print(f"Warning: source document lookup failed for {clean_text(case.get('name'))}: {exc}")
        except requests.RequestException as exc:
            case["source_documents_status"] = "request_error"
            case["source_documents_last_checked"] = checked_at
            print(f"Warning: source document lookup failed for {clean_text(case.get('name'))}: {exc}")
    return enriched, rate_limited
