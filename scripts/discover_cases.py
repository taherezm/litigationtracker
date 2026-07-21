#!/usr/bin/env python3
"""Discover newly filed AI/IP federal cases through CourtListener."""

from __future__ import annotations

import html
import hashlib
import json
import os
import random
import re
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urljoin, urlparse

import feedparser
import requests
from anthropic import Anthropic
from dotenv import load_dotenv

try:
    from scripts.case_intelligence import (
        TRANSPARENT_FALLBACK_SENTENCE,
        claim_text,
        normalize_claims,
        refresh_case_intelligence,
    )
except ModuleNotFoundError:  # pragma: no cover - supports direct script execution.
    from case_intelligence import (  # type: ignore
        TRANSPARENT_FALLBACK_SENTENCE,
        claim_text,
        normalize_claims,
        refresh_case_intelligence,
    )

try:
    from scripts.cl_client import CourtListenerClient, RateLimitExceeded
except ModuleNotFoundError:  # pragma: no cover - supports direct script execution.
    from cl_client import CourtListenerClient, RateLimitExceeded  # type: ignore


BASE_DIR = Path(__file__).resolve().parents[1]
DATA_DIR = BASE_DIR / "data"
CASES_PATH = DATA_DIR / "cases.json"
LAST_RUN_PATH = DATA_DIR / "last_run.json"
COURTLISTENER_BASE = "https://www.courtlistener.com"
COURTLISTENER_SEARCH_URL = f"{COURTLISTENER_BASE}/api/rest/v4/search/"
COURTHOUSE_NEWS_FEED = "https://www.courthousenews.com/feed/"
MODEL_ENV_VAR = "LEGAL_AI_MODEL"
MAX_RETRIES = 3
ANTHROPIC_TIMEOUT = 30.0
DEFAULT_MAX_DISCOVERY_CANDIDATES = 5
MAX_REJECTED_DOCKETS = 500
DISCOVERY_CURSOR_VERSION = 1
AI_TERMS = (
    "ai",
    "artificial intelligence",
    "generative",
    "openai",
    "anthropic",
    "chatgpt",
    "large language model",
    "llm",
    "machine learning",
    "training data",
    "stable diffusion",
    "neural network",
)
IP_CLAIM_TERMS = (
    ("copyright infringement", ("copyright", "17:501")),
    ("patent infringement", ("patent", "35:")),
    ("trade secret", ("trade secret", "defend trade secrets act", "dtsa")),
    ("right of publicity", ("right of publicity", "voice cloning", "deepfake")),
    ("DMCA 1202", ("dmca", "1202")),
    ("trademark", ("trademark", "15:")),
)
CLAIM_SUMMARY_LABELS = {
    "copyright infringement": "copyright infringement",
    "patent infringement": "patent infringement",
    "trade secret": "trade secret misappropriation",
    "right of publicity": "right-of-publicity",
    "dmca 1202": "DMCA section 1202",
    "trademark": "trademark",
}
BANNED_SUMMARY_PHRASES = (
    "violated copyright infringement rights",
    "violated patent infringement rights",
    "violated trademark rights",
    "violated trade secret rights",
    "violated intellectual property claims",
    "the tracker is monitoring",
    "how intellectual property doctrines apply to ai development and use",
    "in a dispute involving artificial intelligence systems, model outputs, or training data",
    "ai systems, model outputs, or training data",
    "unspecified ip or privacy claims",
    "claims against ai developer claims against",
)

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
    '"software" "patent infringement"',
    '"open source" "license violation"',
    '"trade secret" "software"',
    '"reverse engineering" "trade secret"',
    '"API" "copyright infringement"',
    '"scraping" copyright',
    '"data breach" "trade secret"',
    '"algorithm" patent',
    '"machine learning" copyright',
    '"neural network" patent',
    '"computer implemented" patent',
    '"autonomous vehicle" patent',
    '"biometric" privacy',
    '"facial recognition" privacy',
    '"deepfake" "right of publicity"',
    '"voice cloning" "right of publicity"',
    '"NFT" copyright',
    '"blockchain" "intellectual property"',
]

DISCOVERY_QUERY_SET_HASH = hashlib.sha256("\n".join(SEARCH_QUERIES).encode("utf-8")).hexdigest()

RSS_TERMS = (
    "ai",
    "artificial intelligence",
    "copyright",
    "patent",
    "openai",
    "generative",
)

DOCKET_RE = re.compile(r"\b\d:\d{2}-[a-z]{2}-\d{4,6}\b", re.IGNORECASE)


def utc_today() -> date:
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


def model_name() -> str:
    return require_env(MODEL_ENV_VAR)


def max_discovery_candidates() -> int:
    value = (os.environ.get("MAX_DISCOVERY_CANDIDATES") or "").strip()
    if not value:
        return DEFAULT_MAX_DISCOVERY_CANDIDATES
    try:
        return max(0, int(value))
    except ValueError:
        return DEFAULT_MAX_DISCOVERY_CANDIDATES


def anthropic_backoff_sleep(attempt: int) -> None:
    delay = min(2**attempt, 16) + random.uniform(0, 0.25)
    time.sleep(delay)


def anthropic_message(client: Anthropic, prompt: str, max_tokens: int) -> str:
    for attempt in range(MAX_RETRIES + 1):
        try:
            message = client.messages.create(
                model=model_name(),
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
        except Exception as exc:  # Provider exceptions vary across SDK releases.
            status = getattr(exc, "status_code", None)
            if (status == 429 or (isinstance(status, int) and status >= 500)) and attempt < MAX_RETRIES:
                anthropic_backoff_sleep(attempt)
                continue
            raise
    raise RuntimeError("Model request failed after retries")


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


def term_in_text(text: str, term: str) -> bool:
    normalized_term = clean_text(term).lower()
    if not normalized_term:
        return False
    if re.fullmatch(r"[a-z0-9 ]+", normalized_term):
        return bool(re.search(rf"(?<![a-z0-9]){re.escape(normalized_term)}(?![a-z0-9])", text))
    return normalized_term in text


def normalize_sentence_for_dedupe(sentence: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", sentence.lower()).strip()


def dedupe_repeated_summary_sentences(summary: str) -> str:
    text = clean_text(summary)
    sentences = [sentence.strip() for sentence in re.findall(r"[^.!?]+(?:[.!?]+|$)", text) if clean_text(sentence)]
    if len(sentences) < 2:
        return text

    normalized = [normalize_sentence_for_dedupe(sentence) for sentence in sentences]
    for block_size in range(1, (len(sentences) // 2) + 1):
        if len(sentences) % block_size != 0:
            continue
        block = normalized[:block_size]
        if block and normalized == block * (len(sentences) // block_size):
            return " ".join(sentences[:block_size])

    deduped: list[str] = []
    previous = ""
    for sentence, normalized_sentence in zip(sentences, normalized):
        if normalized_sentence and normalized_sentence != previous:
            deduped.append(sentence)
        previous = normalized_sentence
    return " ".join(deduped)


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


def docket_identity(docket_number: Any, docket_id: Any = "", court: Any = "") -> str:
    normalized_id = clean_text(docket_id)
    if normalized_id:
        return f"id:{normalized_id}"
    return f"court:{clean_text(court).lower()}|docket:{docket_key(docket_number)}"


def candidate_identity(candidate: dict[str, Any]) -> str:
    return docket_identity(
        candidate.get("docket_number"),
        candidate.get("docket_id"),
        candidate.get("court"),
    )


def courtlistener_url(docket_id: str, *sources: dict[str, Any]) -> str:
    for source in sources:
        value = first_value(source, ("courtlistener_url", "docket_absolute_url", "absolute_url", "url", "resource_uri"))
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
    for key in (
        "assigned_to_str",
        "assignedToStr",
        "referred_to_str",
        "referredToStr",
        "judge",
        "judges",
        "assigned_to",
        "assignedTo",
        "referred_to",
        "referredTo",
    ):
        value = docket.get(key)
        if isinstance(value, list):
            for item in value:
                values.append(clean_text(item))
        elif isinstance(value, dict):
            values.append(
                clean_text(
                    first_value(
                        value,
                        ("name_full", "nameFull", "name", "display_name", "displayName", "short_name", "shortName"),
                    )
                )
            )
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
        "court": clean_text(
            first_value(result, ("court", "court_id", "courtId", "court_citation_string", "courtCitationString"))
        ),
        "date_filed": clean_text(first_value(result, ("dateFiled", "date_filed", "date_created", "dateCreated"))),
        "parties": clean_text(first_value(result, ("party", "parties", "party_name", "partyName"))),
        "snippet": clean_text(first_value(result, ("snippet", "description", "plain_text", "text"))),
    }


def rejected_docket_cache(raw_rejected_dockets: Any) -> tuple[list[str], set[str]]:
    if not isinstance(raw_rejected_dockets, list):
        raw_rejected_dockets = []
    rejected_dockets: list[str] = []
    rejected_docket_set: set[str] = set()
    for item in raw_rejected_dockets:
        key = clean_text(item)
        if not key:
            continue
        if key in rejected_docket_set:
            rejected_dockets.remove(key)
        else:
            rejected_docket_set.add(key)
        rejected_dockets.append(key)
    return rejected_dockets, rejected_docket_set


def remember_rejected_docket(rejected_dockets: list[str], rejected_docket_set: set[str], key: str) -> None:
    key = clean_text(key)
    if not key:
        return
    if key in rejected_docket_set:
        rejected_dockets.remove(key)
    else:
        rejected_docket_set.add(key)
    rejected_dockets.append(key)


def normalized_search_page_url(value: Any) -> str:
    text = clean_text(value)
    if not text:
        return ""
    absolute = urljoin(f"{COURTLISTENER_BASE}/", text)
    parsed = urlparse(absolute)
    if (
        parsed.scheme != "https"
        or parsed.hostname not in {"www.courtlistener.com", "courtlistener.com"}
        or parsed.path.rstrip("/") != "/api/rest/v4/search"
    ):
        return ""
    return absolute


def validated_search_page_url(
    value: Any,
    query: str,
    search_after: str | None,
    search_before: str | None,
) -> str:
    """Bind a pagination URL to the exact search that produced it."""

    absolute = normalized_search_page_url(value)
    if not absolute:
        return ""
    params = parse_qs(urlparse(absolute).query, keep_blank_values=True)
    expected = {
        "q": query,
        "type": "d",
        "order_by": "score desc",
        "page_size": "20",
    }
    if search_after:
        expected["filed_after"] = search_after
    if search_before:
        expected["filed_before"] = search_before
    for name, expected_value in expected.items():
        if params.get(name) != [expected_value]:
            return ""
    for name, expected_value in (("filed_after", search_after), ("filed_before", search_before)):
        if not expected_value and name in params:
            return ""
    pagination_keys = {name for name in ("cursor", "page") if name in params}
    if len(pagination_keys) != 1:
        return ""
    pagination_key = next(iter(pagination_keys))
    if len(params[pagination_key]) != 1 or not params[pagination_key][0]:
        return ""
    if set(params) != set(expected) | pagination_keys:
        return ""
    return absolute


def search_case_page(
    cl: CourtListenerClient,
    query: str,
    search_after: str | None,
    search_before: str | None = None,
    page_url: str | None = None,
) -> tuple[list[dict[str, Any]], str]:
    params: dict[str, Any] = {
        "q": query,
        "type": "d",
        "order_by": "score desc",
        "page_size": 20,
    }
    if search_after:
        params["filed_after"] = search_after
    if search_before:
        params["filed_before"] = search_before
    request_url = COURTLISTENER_SEARCH_URL
    request_params: dict[str, Any] | None = params
    if page_url:
        request_url = validated_search_page_url(page_url, query, search_after, search_before)
        if not request_url:
            raise RateLimitExceeded("Saved CourtListener search page URL is invalid; restarting this query.")
        request_params = None
    try:
        data = cl.get_json(request_url, params=request_params)
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else None
        if status in {401, 403}:
            raise SystemExit(
                f"CourtListener rejected COURTLISTENER_API_KEY (HTTP {status}); "
                "fix the repository secret before the pipeline can run."
            ) from exc
        raise
    results = data.get("results", []) if isinstance(data.get("results"), list) else []
    raw_next = clean_text(data.get("next"))
    next_url = validated_search_page_url(raw_next, query, search_after, search_before)
    if raw_next and not next_url:
        raise RateLimitExceeded("CourtListener returned an invalid search pagination URL.")
    return results, next_url


def search_cases(cl: CourtListenerClient, query: str, search_after: str | None) -> list[dict[str, Any]]:
    results, _ = search_case_page(cl, query, search_after)
    return results


def collect_query_candidates(
    cl: CourtListenerClient,
    search_after: str,
    search_before: str,
    skipped_dockets: set[str],
    limit: int,
    start_index: int,
    start_page_url: str = "",
    queries: list[str] | None = None,
) -> tuple[dict[str, dict[str, Any]], int, str, bool, bool]:
    """Collect query candidates and return a retry-safe next-query cursor.

    A rate limit leaves the failed query as the next query. A candidate-cap
    interruption also keeps the current query so accepted/rejected docket
    deduplication can drain the rest of that result page on a later run.
    """

    query_list = queries if queries is not None else SEARCH_QUERIES
    next_query_index, start_index_valid = cursor_index_state(start_index, len(query_list))
    current_page_url = (
        validated_search_page_url(
            start_page_url,
            query_list[next_query_index],
            search_after,
            search_before,
        )
        if start_page_url and start_index_valid and next_query_index < len(query_list)
        else ""
    )
    candidates_by_docket: dict[str, dict[str, Any]] = {}

    for index in range(next_query_index, len(query_list)):
        query = query_list[index]
        while True:
            requested_page_url = current_page_url
            try:
                results, following_page_url = search_case_page(
                    cl,
                    query,
                    search_after,
                    search_before,
                    requested_page_url,
                )
            except RateLimitExceeded as exc:
                print(f"Warning: skipped search query after CourtListener rate limit: {query} ({exc})")
                return candidates_by_docket, index, requested_page_url, True, False

            for result in results:
                candidate = result_to_candidate(result, query)
                if not candidate:
                    continue
                key = candidate_identity(candidate)
                if key not in skipped_dockets:
                    candidates_by_docket.setdefault(key, candidate)
                if limit and len(candidates_by_docket) >= limit:
                    print(f"Warning: discovery candidate collection stopped at {limit} candidates.")
                    return candidates_by_docket, index, requested_page_url, False, True

            if following_page_url:
                current_page_url = following_page_url
                continue
            next_query_index = index + 1
            current_page_url = ""
            break

    return candidates_by_docket, next_query_index, "", False, False


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
{{"relevant": true/false, "confidence": "high"/"medium"/"low", "reason": "one sentence", "claims": ["list"]}}"""
    return parse_json_object(anthropic_message(client, prompt, max_tokens=300))


def fallback_classification(candidate: dict[str, Any]) -> dict[str, Any]:
    raw = candidate.get("raw", {})
    text = " ".join(
        clean_text(value)
        for value in (
            candidate.get("case_name"),
            candidate.get("court"),
            candidate.get("parties"),
            candidate.get("snippet"),
            first_value(raw, ("cause", "suitNature", "caseName", "case_name_full")),
        )
    ).lower()
    claims = [claim for claim, terms in IP_CLAIM_TERMS if any(term_in_text(text, term) for term in terms)]
    ai_tags = [term for term in AI_TERMS if term_in_text(text, term)]
    relevant = bool(claims and ai_tags)
    return {
        "relevant": relevant,
        "confidence": "medium" if relevant else "low",
        "reason": "Deterministic fallback based on AI and IP terms in CourtListener search metadata.",
        "claims": claims,
    }


def english_join(items: list[str]) -> str:
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    if len(items) == 2:
        return f"{items[0]} and {items[1]}"
    return f"{', '.join(items[:-1])}, and {items[-1]}"


def claim_summary_text(claims: list[str]) -> str:
    labels: list[str] = []
    for claim in claims:
        label = CLAIM_SUMMARY_LABELS.get(clean_text(claim).lower())
        if not label:
            label = clean_text(claim).lower()
        if label and label not in labels:
            labels.append(label)
    return english_join(labels) or "intellectual property"


def deterministic_case_summary(case_name: str, claims: list[str], parties: dict[str, str]) -> str:
    plaintiff = clean_text(parties.get("plaintiff")) or "The plaintiff"
    defendant = clean_text(parties.get("defendant")) or "the defendant"
    normalized_claims = normalize_claims(claims)
    claims_text = claim_text(normalized_claims)
    return (
        f"{plaintiff} filed {claims_text} claims against {defendant}. "
        f"Newly filed case. {TRANSPARENT_FALLBACK_SENTENCE}"
    )


def legalize_case_summary(summary: str, case_name: str, claims: list[str], parties: dict[str, str]) -> str:
    text = dedupe_repeated_summary_sentences(summary)
    lowered = text.lower()
    if not text or any(phrase in lowered for phrase in BANNED_SUMMARY_PHRASES):
        return deterministic_case_summary(case_name, claims, parties)
    first_sentence = re.split(r"(?<=[.!?])\s+", text, maxsplit=1)[0].lower()
    allegation_words = ("alleges", "asserts", "claims", "contends", "argues", "accuses")
    if "violated" in first_sentence and not any(word in first_sentence for word in allegation_words):
        return deterministic_case_summary(case_name, claims, parties)
    return text


def use_model_case_summaries() -> bool:
    return any(
        (os.environ.get(name) or "").strip().lower() in {"1", "true", "yes"}
        for name in ("USE_MODEL_CASE_SUMMARIES", "USE_ANTHROPIC_CASE_SUMMARIES")
    )


def generate_plain_language_summary(client: Anthropic, case_name: str, claims: list[str], parties: dict[str, str]) -> str:
    if not use_model_case_summaries():
        return deterministic_case_summary(case_name, claims, parties)
    prompt = f"""Write exactly 2 sentences about this lawsuit for a public AI/IP litigation tracker.
Audience: law students, lawyers, and interested non-lawyers.

Legal precision rules:
- Describe allegations, not proven facts. Use "alleges," "asserts," "claims," or "contends."
- Do not say a defendant "violated" the law unless the input says a court has ruled that way.
- Never write "violated copyright infringement rights." Prefer "asserts copyright infringement claims" or "alleges infringement of copyrighted works."
- Do not invent procedural posture, holdings, rulings, statutory sections, or factual details.
- Sentence 1 should say who is suing whom and what claims are asserted.
- Sentence 2 should explain why the case matters for AI and intellectual property law.

Case: {case_name}, Claims: {claims}, Parties: {parties}"""
    try:
        return legalize_case_summary(anthropic_message(client, prompt, max_tokens=150), case_name, claims, parties)
    except Exception as exc:
        print(f"Warning: using fallback plain-language summary for {case_name}: {exc}")
        return deterministic_case_summary(case_name, claims, parties)


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
    claims = normalize_claims(listify(classification.get("claims")))
    docket_id = extract_docket_id(docket) or candidate["docket_id"]
    today = utc_today().isoformat()

    case = {
        "id": slugify(case_name, docket_number, existing_ids),
        "name": case_name,
        "court": clean_text(
            first_value(docket, ("court", "court_id", "courtId", "court_citation_string", "courtCitationString"))
        )
        or candidate.get("court", ""),
        "court_full": clean_text(
            first_value(docket, ("court_full_name", "court_name", "courtFullName", "courtName", "court"))
        )
        or candidate.get("court", ""),
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
        "source": "courtlistener",
        "courtlistener_url": courtlistener_url(str(docket_id), docket, candidate.get("raw", {})),
        "last_updated": today,
        "discovered_date": today,
    }
    refresh_case_intelligence(case, [])
    return case


def rss_docket_numbers() -> list[str]:
    feed = feedparser.parse(COURTHOUSE_NEWS_FEED)
    docket_numbers: list[str] = []
    seen: set[str] = set()
    for entry in feed.entries:
        text = clean_text(f"{entry.get('title', '')} {entry.get('description', '')} {entry.get('summary', '')}")
        lowered = text.lower()
        if not any(term_in_text(lowered, term) for term in RSS_TERMS):
            continue
        for docket_number in sorted(set(DOCKET_RE.findall(text))):
            key = docket_key(docket_number)
            if key and key not in seen:
                seen.add(key)
                docket_numbers.append(docket_number)
    return docket_numbers


def collect_rss_candidates(
    cl: CourtListenerClient,
    docket_numbers: list[str],
    skipped_dockets: set[str],
    limit: int,
    start_index: int = 0,
    start_page_url: str = "",
) -> tuple[dict[str, dict[str, Any]], int, str, bool, bool]:
    """Collect every search page for each RSS docket with a resumable cursor."""

    candidates_by_docket: dict[str, dict[str, Any]] = {}
    next_rss_index, start_index_valid = cursor_index_state(start_index, len(docket_numbers))
    current_page_url = (
        validated_search_page_url(start_page_url, docket_numbers[next_rss_index], None, None)
        if start_page_url and start_index_valid and next_rss_index < len(docket_numbers)
        else ""
    )
    for index in range(next_rss_index, len(docket_numbers)):
        docket_number = docket_numbers[index]
        while True:
            requested_page_url = current_page_url
            try:
                results, following_page_url = search_case_page(
                    cl,
                    docket_number,
                    search_after=None,
                    search_before=None,
                    page_url=requested_page_url,
                )
            except RateLimitExceeded as exc:
                print(f"Warning: skipped RSS docket lookup after rate limit: {docket_number} ({exc})")
                return candidates_by_docket, index, requested_page_url, True, False

            for result in results:
                candidate = result_to_candidate(result, f"rss:{COURTHOUSE_NEWS_FEED}")
                if not candidate:
                    continue
                key = candidate_identity(candidate)
                if key not in skipped_dockets:
                    candidates_by_docket.setdefault(key, candidate)
                if limit and len(candidates_by_docket) >= limit:
                    print(f"Warning: RSS candidate collection stopped at {limit} candidates.")
                    return candidates_by_docket, index, requested_page_url, False, True

            if following_page_url:
                current_page_url = following_page_url
                continue
            next_rss_index = index + 1
            current_page_url = ""
            break

    return candidates_by_docket, next_rss_index, "", False, False


def search_after_date(cases: list[dict[str, Any]], last_run: dict[str, Any]) -> str:
    discovery_last_run_date = clean_text(last_run.get("discovery_last_run_date"))
    if discovery_last_run_date:
        return discovery_last_run_date

    legacy_last_run_date = clean_text(last_run.get("last_run_date"))
    if len(cases) >= 5 and legacy_last_run_date and last_run.get("discovery_complete", True):
        return legacy_last_run_date

    return (utc_today() - timedelta(days=90)).isoformat()


def valid_iso_date(value: Any) -> str:
    text = clean_text(value)
    try:
        date.fromisoformat(text)
    except ValueError:
        return ""
    return text


def cursor_index_state(value: Any, upper_bound: int) -> tuple[int, bool]:
    if isinstance(value, bool):
        return 0, False
    if isinstance(value, int):
        index = value
    elif isinstance(value, str) and re.fullmatch(r"-?\d+", value.strip()):
        index = int(value)
    else:
        return 0, False
    return (index, True) if 0 <= index <= upper_bound else (0, False)


def normalized_cursor_index(value: Any, upper_bound: int) -> int:
    return cursor_index_state(value, upper_bound)[0]


def normalized_pending_candidate_state(value: Any) -> tuple[list[dict[str, Any]], bool]:
    """Return retry payloads plus whether any persisted payload was malformed."""

    if value is None:
        return [], False
    if not isinstance(value, list):
        return [], True
    candidates_by_docket: dict[str, dict[str, Any]] = {}
    invalid = False
    for item in value:
        if not isinstance(item, dict):
            invalid = True
            continue
        raw_value = item.get("raw")
        if not isinstance(raw_value, dict):
            invalid = True
            raw_value = {}
        candidate = {
            "source": clean_text(item.get("source")) or "classifier-retry",
            "raw": raw_value,
            "docket_id": clean_text(item.get("docket_id")),
            "docket_number": normalize_docket_number(item.get("docket_number")),
            "case_name": clean_text(item.get("case_name")),
            "court": clean_text(item.get("court")),
            "date_filed": clean_text(item.get("date_filed")),
            "parties": clean_text(item.get("parties")),
            "snippet": clean_text(item.get("snippet")),
        }
        if not candidate["docket_id"] or not candidate["docket_number"] or not candidate["case_name"]:
            invalid = True
            continue
        candidates_by_docket.setdefault(candidate_identity(candidate), candidate)
    return list(candidates_by_docket.values()), invalid


def normalized_pending_candidates(value: Any) -> list[dict[str, Any]]:
    return normalized_pending_candidate_state(value)[0]


def discovery_cursor(cases: list[dict[str, Any]], last_run: dict[str, Any]) -> dict[str, Any]:
    """Load a cursor or conservatively restart the anchored discovery cycle."""

    raw = last_run.get("discovery_cursor")
    if not isinstance(raw, dict):
        raw = {}

    today = utc_today()
    baseline = valid_iso_date(search_after_date(cases, last_run))
    if not baseline or date.fromisoformat(baseline) > today:
        baseline = (today - timedelta(days=90)).isoformat()
    baseline_date = date.fromisoformat(baseline)

    raw_window_start = valid_iso_date(raw.get("window_start"))
    raw_window_through = valid_iso_date(raw.get("window_through"))
    anchors_valid = bool(
        raw_window_start
        and raw_window_through
        and date.fromisoformat(raw_window_start)
        <= baseline_date
        <= date.fromisoformat(raw_window_through)
        <= today
    )
    cursor_matches = bool(
        raw.get("version") == DISCOVERY_CURSOR_VERSION
        and clean_text(raw.get("query_set_sha256")) == DISCOVERY_QUERY_SET_HASH
        and anchors_valid
    )

    if cursor_matches:
        window_start = raw_window_start
        window_through = raw_window_through
    else:
        safe_start = baseline_date
        if raw_window_start and date.fromisoformat(raw_window_start) <= today:
            safe_start = min(safe_start, date.fromisoformat(raw_window_start))
        safe_through = today
        if raw_window_through:
            candidate_through = date.fromisoformat(raw_window_through)
            if max(safe_start, baseline_date) <= candidate_through <= today:
                safe_through = candidate_through
        window_start = safe_start.isoformat()
        window_through = safe_through.isoformat()

    pending_candidates, pending_state_invalid = normalized_pending_candidate_state(raw.get("pending_candidates"))
    progress_valid = (
        cursor_matches
        and not pending_state_invalid
        and raw.get("phase") in {"queries", "rss"}
    )
    if progress_valid:
        next_query_index, query_index_valid = cursor_index_state(
            raw.get("next_query_index"),
            len(SEARCH_QUERIES),
        )
    else:
        next_query_index, query_index_valid = 0, False
    phase = (
        "rss"
        if progress_valid and raw.get("phase") == "rss" and next_query_index == len(SEARCH_QUERIES)
        else "queries"
    )

    raw_rss_dockets = raw.get("rss_docket_numbers") if phase == "rss" else []
    saved_rss_dockets: list[str] = []
    seen_rss_dockets: set[str] = set()
    rss_snapshot_invalid = phase == "rss" and not isinstance(raw_rss_dockets, list)
    if isinstance(raw_rss_dockets, list):
        for item in raw_rss_dockets:
            if not isinstance(item, str):
                rss_snapshot_invalid = True
                continue
            docket_number = normalize_docket_number(item)
            key = docket_key(docket_number)
            if not docket_number:
                rss_snapshot_invalid = True
            elif key in seen_rss_dockets:
                rss_snapshot_invalid = True
            else:
                seen_rss_dockets.add(key)
                saved_rss_dockets.append(docket_number)

    if rss_snapshot_invalid:
        progress_valid = False
        phase = "queries"
        next_query_index = 0
        saved_rss_dockets = []

    query_page_url = ""
    raw_query_page_url = clean_text(raw.get("query_page_url"))
    if progress_valid and query_index_valid and phase == "queries" and raw_query_page_url:
        if next_query_index < len(SEARCH_QUERIES):
            query_page_url = validated_search_page_url(
                raw_query_page_url,
                SEARCH_QUERIES[next_query_index],
                window_start,
                window_through,
            )
        else:
            next_query_index = 0

    if progress_valid and phase == "rss":
        next_rss_index, rss_index_valid = cursor_index_state(
            raw.get("next_rss_index"),
            len(saved_rss_dockets),
        )
    else:
        next_rss_index, rss_index_valid = 0, False
    rss_page_url = ""
    raw_rss_page_url = clean_text(raw.get("rss_page_url"))
    if progress_valid and rss_index_valid and phase == "rss" and raw_rss_page_url:
        if next_rss_index < len(saved_rss_dockets):
            rss_page_url = validated_search_page_url(
                raw_rss_page_url,
                saved_rss_dockets[next_rss_index],
                None,
                None,
            )
        else:
            next_rss_index = 0

    return {
        "version": DISCOVERY_CURSOR_VERSION,
        "window_start": window_start,
        "window_through": window_through,
        "query_set_sha256": DISCOVERY_QUERY_SET_HASH,
        "phase": phase,
        "next_query_index": next_query_index,
        "query_page_url": query_page_url,
        "rss_docket_numbers": saved_rss_dockets,
        "next_rss_index": next_rss_index,
        "rss_page_url": rss_page_url,
        "pending_candidates": pending_candidates,
        "pending_state_invalid": pending_state_invalid,
    }


def force_discovery() -> bool:
    return (os.environ.get("FORCE_DISCOVERY") or "").strip().lower() in {"1", "true", "yes"}


def reset_run_rate_limit_state() -> bool:
    return (os.environ.get("RESET_COURTLISTENER_RATE_LIMIT_STATE") or "").strip().lower() in {
        "1",
        "true",
        "yes",
    }


def discovery_already_current(last_run: dict[str, Any]) -> bool:
    return (
        clean_text(last_run.get("discovery_last_run_date")) == utc_today().isoformat()
        and bool(last_run.get("discovery_complete", True))
        and not isinstance(last_run.get("discovery_cursor"), dict)
        and not force_discovery()
    )


def main() -> None:
    load_dotenv()
    cases = load_json(CASES_PATH, [])
    if not isinstance(cases, list):
        cases = []
    last_run = load_json(LAST_RUN_PATH, {})
    if not isinstance(last_run, dict):
        last_run = {}
    previous_discovery_complete = bool(last_run.get("discovery_complete", True))

    if reset_run_rate_limit_state():
        last_run["courtlistener_rate_limited"] = False

    if discovery_already_current(last_run):
        last_run["cases_discovered"] = 0
        last_run["discovery_complete"] = True
        last_run["discovery_candidate_cap_reached"] = False
        last_run["discovery_phase"] = "complete"
        last_run["discovery_queries_completed"] = len(SEARCH_QUERIES)
        last_run["discovery_queries_total"] = len(SEARCH_QUERIES)
        last_run.pop("discovery_cursor", None)
        last_run.pop("discovery_incomplete_reason", None)
        last_run.pop("discovery_incomplete_since", None)
        if last_run.get("docket_update_complete", True):
            last_run["last_run_date"] = utc_today().isoformat()
        write_json(LAST_RUN_PATH, last_run)
        print(f"Discovery already current for {utc_today().isoformat()}; skipping.")
        return

    courtlistener_key = require_env("COURTLISTENER_API_KEY")
    anthropic_key = require_env("ANTHROPIC_API_KEY")
    model_name()

    known_dockets = {
        docket_identity(
            case.get("docket_number"),
            case.get("courtlistener_docket_id"),
            case.get("court"),
        )
        for case in cases
        if case.get("docket_number")
    }
    rejected_dockets, rejected_docket_set = rejected_docket_cache(last_run.get("rejected_dockets", []))
    skipped_dockets = known_dockets | rejected_docket_set
    existing_ids = {clean_text(case.get("id")) for case in cases if case.get("id")}
    limit = max_discovery_candidates()
    cursor = discovery_cursor(cases, last_run)
    if cursor["pending_state_invalid"]:
        print(
            "Warning: malformed pending discovery state was discarded; "
            "restarting the full anchored source sweep."
        )
    search_after = cursor["window_start"]
    cycle_through = cursor["window_through"]

    pending_candidates = [
        candidate
        for candidate in cursor["pending_candidates"]
        if candidate_identity(candidate) not in skipped_dockets
    ]
    pending_identities = {candidate_identity(candidate) for candidate in pending_candidates}
    source_skipped_dockets = skipped_dockets | pending_identities
    if limit:
        pending_to_process = pending_candidates[:limit]
        deferred_pending_candidates = pending_candidates[limit:]
        source_limit = max(0, limit - len(pending_to_process))
    else:
        pending_to_process = pending_candidates
        deferred_pending_candidates = []
        source_limit = 0

    cl = CourtListenerClient(courtlistener_key)
    client = Anthropic(api_key=anthropic_key, timeout=ANTHROPIC_TIMEOUT, max_retries=0)
    candidates_by_docket: dict[str, dict[str, Any]] = {}
    phase = str(cursor["phase"])
    next_query_index = int(cursor["next_query_index"])
    query_page_url = str(cursor["query_page_url"])
    saved_rss_dockets = list(cursor["rss_docket_numbers"]) if phase == "rss" else []
    next_rss_index = int(cursor["next_rss_index"]) if phase == "rss" else 0
    rss_page_url = str(cursor["rss_page_url"]) if phase == "rss" else ""
    query_rate_limited = False
    query_cap_reached = False
    rss_rate_limited = False
    rss_cap_reached = False
    source_collection_complete = bool(
        phase == "rss"
        and next_query_index == len(SEARCH_QUERIES)
        and next_rss_index == len(saved_rss_dockets)
        and not rss_page_url
    )
    discovery_candidate_cap_reached = bool(deferred_pending_candidates)

    can_collect_sources = not limit or source_limit > 0
    if phase == "queries" and can_collect_sources:
        (
            candidates_by_docket,
            next_query_index,
            query_page_url,
            query_rate_limited,
            query_cap_reached,
        ) = collect_query_candidates(
            cl,
            search_after,
            cycle_through,
            source_skipped_dockets,
            source_limit,
            next_query_index,
            query_page_url,
        )
        discovery_candidate_cap_reached = discovery_candidate_cap_reached or query_cap_reached
    elif phase == "queries":
        discovery_candidate_cap_reached = True

    if not query_rate_limited and not query_cap_reached and next_query_index == len(SEARCH_QUERIES):
        phase = "rss"
        if cursor["phase"] == "rss":
            saved_rss_dockets = list(cursor["rss_docket_numbers"])
            next_rss_index = int(cursor["next_rss_index"])
            rss_page_url = str(cursor["rss_page_url"])
        else:
            saved_rss_dockets = rss_docket_numbers()
            next_rss_index = 0
            rss_page_url = ""
        query_page_url = ""
        source_collection_complete = next_rss_index == len(saved_rss_dockets) and not rss_page_url

        rss_limit = 0 if not limit else max(0, source_limit - len(candidates_by_docket))
        can_collect_rss = not limit or rss_limit > 0
        if not source_collection_complete and can_collect_rss:
            rss_skipped_dockets = source_skipped_dockets | set(candidates_by_docket)
            (
                rss_candidates,
                next_rss_index,
                rss_page_url,
                rss_rate_limited,
                rss_cap_reached,
            ) = collect_rss_candidates(
                cl,
                saved_rss_dockets,
                rss_skipped_dockets,
                rss_limit,
                next_rss_index,
                rss_page_url,
            )
            candidates_by_docket.update(rss_candidates)
            discovery_candidate_cap_reached = discovery_candidate_cap_reached or rss_cap_reached
            source_collection_complete = bool(
                not rss_rate_limited
                and not rss_cap_reached
                and next_rss_index == len(saved_rss_dockets)
                and not rss_page_url
            )
        elif not source_collection_complete:
            discovery_candidate_cap_reached = True

    candidates = pending_to_process + list(candidates_by_docket.values())
    if limit and len(candidates) > limit:
        raise RuntimeError(
            f"Internal discovery budget error: selected {len(candidates)} candidates with a limit of {limit}."
        )

    discovered: list[dict[str, Any]] = []
    failed_candidates: list[dict[str, Any]] = []
    for candidate in candidates:
        key = candidate_identity(candidate)
        classification = fallback_classification(candidate)
        if classification.get("relevant"):
            print(f"Using deterministic classifier for {candidate['docket_number']}.")
        else:
            try:
                classification = classify_case(client, candidate)
                if not isinstance(classification, dict):
                    raise ValueError("model classifier returned non-object JSON")
            except json.JSONDecodeError:
                failed_candidates.append(candidate)
                print(f"Warning: model returned malformed classifier JSON for {candidate['docket_number']}.")
                continue
            except Exception as exc:
                failed_candidates.append(candidate)
                print(f"Warning: model classifier failed for {candidate['docket_number']}: {exc}")
                continue
        if not classification.get("relevant"):
            fallback = fallback_classification(candidate)
            if fallback.get("relevant"):
                classification = fallback
                print(f"Warning: using deterministic relevance fallback for {candidate['docket_number']}.")
            else:
                remember_rejected_docket(rejected_dockets, rejected_docket_set, key)
                continue
        if clean_text(classification.get("confidence")).lower() not in {"high", "medium"}:
            fallback = fallback_classification(candidate)
            if fallback.get("relevant"):
                classification = fallback
                print(f"Warning: using deterministic confidence fallback for {candidate['docket_number']}.")
            else:
                remember_rejected_docket(rejected_dockets, rejected_docket_set, key)
                continue
        docket = candidate.get("raw", {})
        if not isinstance(docket, dict):
            docket = {}
        discovered.append(build_case(candidate, docket, classification, client, existing_ids))

    cases.extend(discovered)
    write_json(CASES_PATH, cases)

    pending_candidates = normalized_pending_candidates(deferred_pending_candidates + failed_candidates)
    classification_failed = bool(failed_candidates)
    discovery_complete = source_collection_complete and not pending_candidates
    courtlistener_rate_limited = query_rate_limited or rss_rate_limited
    last_run["cases_discovered"] = len(discovered)
    last_run["discovery_complete"] = discovery_complete
    last_run["discovery_candidate_cap_reached"] = discovery_candidate_cap_reached
    last_run["discovery_queries_completed"] = (
        len(SEARCH_QUERIES) if discovery_complete else next_query_index
    )
    last_run["discovery_queries_total"] = len(SEARCH_QUERIES)
    last_run["discovery_rss_dockets_completed"] = next_rss_index
    last_run["discovery_rss_dockets_total"] = len(saved_rss_dockets)
    last_run["courtlistener_rate_limited"] = (
        bool(last_run.get("courtlistener_rate_limited")) or courtlistener_rate_limited
    )
    last_run["rejected_dockets"] = rejected_dockets[-MAX_REJECTED_DOCKETS:]
    if discovery_complete:
        last_run["discovery_phase"] = "complete"
        last_run["discovery_last_run_date"] = cycle_through
        last_run.pop("discovery_cursor", None)
        last_run.pop("discovery_incomplete_reason", None)
        last_run.pop("discovery_incomplete_since", None)
        if last_run.get("docket_update_complete", True):
            last_run["last_run_date"] = cycle_through
    else:
        if classification_failed:
            incomplete_reason = "classification"
        elif discovery_candidate_cap_reached:
            incomplete_reason = "candidate_cap"
        elif courtlistener_rate_limited:
            incomplete_reason = "rate_limit"
        else:
            incomplete_reason = "source_collection"
        last_run["discovery_phase"] = phase
        last_run["discovery_incomplete_reason"] = incomplete_reason
        if not clean_text(last_run.get("discovery_incomplete_since")):
            if previous_discovery_complete:
                last_run["discovery_incomplete_since"] = utc_today().isoformat()
            else:
                last_run["discovery_incomplete_since"] = (
                    valid_iso_date(last_run.get("discovery_last_run_date")) or utc_today().isoformat()
                )
        last_run["discovery_cursor"] = {
            "version": DISCOVERY_CURSOR_VERSION,
            "window_start": search_after,
            "window_through": cycle_through,
            "query_set_sha256": DISCOVERY_QUERY_SET_HASH,
            "phase": phase,
            "next_query_index": next_query_index,
            "query_page_url": query_page_url if phase == "queries" else "",
            "rss_docket_numbers": saved_rss_dockets if phase == "rss" else [],
            "next_rss_index": next_rss_index if phase == "rss" else 0,
            "rss_page_url": rss_page_url if phase == "rss" else "",
            "pending_candidates": pending_candidates,
        }
    write_json(LAST_RUN_PATH, last_run)

    print(f"Discovered {len(discovered)} new cases.")


if __name__ == "__main__":
    main()
