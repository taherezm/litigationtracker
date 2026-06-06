#!/usr/bin/env python3
"""Summarize unsummarized docket entries with Anthropic."""

from __future__ import annotations

import html
import json
import os
import random
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from anthropic import Anthropic
from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parents[1]
DATA_DIR = BASE_DIR / "data"
CASES_PATH = DATA_DIR / "cases.json"
UPDATES_PATH = DATA_DIR / "updates.json"
LAST_RUN_PATH = DATA_DIR / "last_run.json"
MODEL = "claude-sonnet-4-20250514"
MAX_RETRIES = 3
ANTHROPIC_TIMEOUT = 30.0

POSTURE_OPTIONS = {
    "Filed",
    "Motion Practice",
    "Discovery",
    "Summary Judgment",
    "Trial",
    "Appeal",
    "Settled",
    "Dismissed",
    "Judgment",
}

LEGAL_PRECISION_RULES = """Legal precision rules:
- Describe docket activity, not ultimate liability, unless the entry itself reports a court ruling.
- Use "alleges," "asserts," or "moves" for party filings. Use "the court ordered," "the court denied," or "the court granted" only for court action.
- Do not say a party "violated" the law unless the entry reports that the court held so.
- Never write phrases like "violated copyright infringement rights."
- Do not invent claims, holdings, deadlines, settlement status, or procedural posture.
- Preserve uncertainty when the entry is administrative, a notice, a filing, or a scheduling item."""
BANNED_SUMMARY_PHRASES = (
    "violated copyright infringement rights",
    "violated patent infringement rights",
    "violated trademark rights",
    "violated trade secret rights",
)
EMPTY_ENTRY_SUMMARY_MARKERS = (
    "no docket entry text",
    "no entry text",
    "cannot summarize",
    "unable to summarize",
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


def empty_entry_placeholder_summary(value: Any) -> bool:
    text = clean_text(value).lower()
    return bool(text) and any(marker in text for marker in EMPTY_ENTRY_SUMMARY_MARKERS)


def backoff_sleep(attempt: int) -> None:
    delay = min(2**attempt, 16) + random.uniform(0, 0.25)
    time.sleep(delay)


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
        except Exception as exc:
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


def fallback_summary(entry: dict[str, Any]) -> dict[str, Any]:
    raw_text = clean_text(entry.get("raw_text"))
    if raw_text:
        clipped = raw_text[:260].rstrip()
        if len(raw_text) > 260:
            clipped = f"{clipped}..."
        summary = f"This docket entry records: {clipped}"
    else:
        summary = "CourtListener recorded docket activity for this case, but did not provide public text for this entry."
    return {
        "summary": summary,
        "significance": "minor_update",
        "posture_update": None,
        "key_holding": None,
    }


def legalize_entry_summary(summary: Any, entry: dict[str, Any]) -> str | None:
    text = dedupe_repeated_summary_sentences(summary)
    if (
        not text
        or empty_entry_placeholder_summary(text)
        or any(phrase in text.lower() for phrase in BANNED_SUMMARY_PHRASES)
    ):
        return fallback_summary(entry)["summary"]
    first_sentence = re.split(r"(?<=[.!?])\s+", text, maxsplit=1)[0].lower()
    allegation_words = ("alleges", "asserts", "claims", "contends", "argues", "accuses")
    court_words = ("court", "judge", "magistrate", "order", "ordered", "granted", "denied", "held")
    if "violated" in first_sentence and not any(word in first_sentence for word in allegation_words + court_words):
        return fallback_summary(entry)["summary"]
    return text


def summarize_entry(client: Anthropic, case: dict[str, Any], entry: dict[str, Any]) -> dict[str, Any]:
    if not clean_text(entry.get("raw_text")):
        return fallback_summary(entry)

    prompt = f"""Summarize this federal court docket entry for a public litigation tracker.
Audience: law students and attorneys. College reading level. No jargon.

{LEGAL_PRECISION_RULES}

Case: {case.get("name")} ({case.get("court")})
Entry text: {entry.get("raw_text")}

Respond ONLY with valid JSON, no preamble:
{{
  "summary": "1-2 plain English sentences starting with the actor: The judge..., Plaintiff filed..., Both parties...",
  "significance": "significant_ruling" or "minor_update" or "case_resolved",
  "posture_update": "new posture if changed, else null. Options: Filed, Motion Practice, Discovery, Summary Judgment, Trial, Appeal, Settled, Dismissed, Judgment",
  "key_holding": "If significant_ruling: one sentence holding. Otherwise null."
}}"""
    for attempt in range(MAX_RETRIES + 1):
        try:
            text = anthropic_message(client, prompt, max_tokens=200)
        except Exception as exc:
            print(
                f"Warning: Anthropic summary request failed for entry {clean_text(entry.get('entry_number'))}; using fallback summary: {exc}"
            )
            return fallback_summary(entry)
        try:
            result = parse_json_object(text)
            result["summary"] = legalize_entry_summary(result.get("summary"), entry)
            return result
        except json.JSONDecodeError:
            if attempt < MAX_RETRIES:
                continue
            print(
                f"Warning: Anthropic returned malformed summary JSON for entry {clean_text(entry.get('entry_number'))}; using fallback summary."
            )
            return fallback_summary(entry)
    return fallback_summary(entry)


def is_unsummarized(entry: dict[str, Any]) -> bool:
    return not clean_text(entry.get("summary"))


def update_matching_updates(
    updates: list[dict[str, Any]],
    case: dict[str, Any],
    entry: dict[str, Any],
    summary: str,
    significance: str,
) -> None:
    case_id = clean_text(case.get("id"))
    entry_number = clean_text(entry.get("entry_number"))
    entry_date = clean_text(entry.get("date"))
    for update in updates:
        if clean_text(update.get("case_id")) != case_id:
            continue
        if clean_text(update.get("entry_number")) != entry_number:
            continue
        if entry_date and clean_text(update.get("entry_date")) != entry_date:
            continue
        update["summary"] = summary
        update["significance"] = significance


def append_key_ruling(case: dict[str, Any], entry: dict[str, Any], result: dict[str, Any]) -> None:
    summary = clean_text(result.get("summary"))
    holding = clean_text(result.get("key_holding"))
    date = clean_text(entry.get("date"))
    description = holding or summary
    if not description:
        return
    ruling = {
        "date": date,
        "description": description,
        "summary": summary,
        "significance": "significant_ruling",
    }
    key = (ruling["date"], ruling["description"])
    existing = {
        (clean_text(item.get("date")), clean_text(item.get("description")))
        for item in case.get("key_rulings", [])
        if isinstance(item, dict)
    }
    if key not in existing:
        case.setdefault("key_rulings", []).append(ruling)


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

    pending: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for case in cases:
        if not isinstance(case, dict):
            continue
        for entry in case.get("docket_entries", []):
            if isinstance(entry, dict) and is_unsummarized(entry):
                pending.append((case, entry))

    generated = 0
    today = utc_now().date().isoformat()
    if pending:
        needs_anthropic = any(clean_text(entry.get("raw_text")) for _, entry in pending)
        api_key = (os.environ.get("ANTHROPIC_API_KEY") or "").strip() if needs_anthropic else ""
        if needs_anthropic and not api_key:
            raise SystemExit("Missing required environment variable: ANTHROPIC_API_KEY")
        client = Anthropic(api_key=api_key, timeout=ANTHROPIC_TIMEOUT, max_retries=0) if needs_anthropic else None
        for case, entry in pending:
            result = summarize_entry(client, case, entry) if client else fallback_summary(entry)
            summary = clean_text(result.get("summary"))
            significance = clean_text(result.get("significance")) or "minor_update"
            if significance not in {"significant_ruling", "minor_update", "case_resolved"}:
                significance = "minor_update"

            entry["summary"] = summary
            entry["significance"] = significance
            update_matching_updates(updates, case, entry, summary, significance)

            posture = result.get("posture_update")
            if posture is not None:
                posture_text = clean_text(posture)
                if posture_text in POSTURE_OPTIONS:
                    case["procedural_posture"] = posture_text

            if significance == "significant_ruling":
                append_key_ruling(case, entry, result)
            if significance == "case_resolved":
                case["status"] = "resolved"

            case["last_updated"] = today
            generated += 1

    if last_run.get("docket_update_complete", True):
        last_run["docket_last_run_date"] = today
    else:
        print("Warning: docket_last_run_date was not advanced because docket update did not complete.")

    if last_run.get("discovery_complete", True):
        last_run["discovery_last_run_date"] = today
    else:
        print("Warning: discovery_last_run_date was not advanced because discovery did not complete.")

    if last_run.get("discovery_complete", True) and last_run.get("docket_update_complete", True):
        last_run["last_run_date"] = today
    else:
        print("Warning: last_run_date was not advanced because discovery or docket update did not complete.")
    last_run["summaries_generated"] = generated
    last_run.setdefault("cases_discovered", 0)
    last_run.setdefault("entries_updated", 0)

    write_json(CASES_PATH, cases)
    write_json(UPDATES_PATH, updates)
    write_json(LAST_RUN_PATH, last_run)

    print(f"Generated {generated} summaries.")


if __name__ == "__main__":
    main()
