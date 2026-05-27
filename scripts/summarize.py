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
        summary = f"The docket was updated with this entry: {clipped}"
    else:
        summary = "The docket was updated with a new entry."
    return {
        "summary": summary,
        "significance": "minor_update",
        "posture_update": None,
        "key_holding": None,
    }


def summarize_entry(client: Anthropic, case: dict[str, Any], entry: dict[str, Any]) -> dict[str, Any]:
    prompt = f"""Summarize this federal court docket entry for a public litigation tracker.
Audience: law students and attorneys. College reading level. No jargon.

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
        text = anthropic_message(client, prompt, max_tokens=200)
        try:
            return parse_json_object(text)
        except json.JSONDecodeError:
            if attempt < MAX_RETRIES:
                continue
            print(
                f"Warning: Anthropic returned malformed summary JSON for entry {clean_text(entry.get('entry_number'))}; using fallback summary."
            )
            return fallback_summary(entry)
    return fallback_summary(entry)


def is_unsummarized(entry: dict[str, Any]) -> bool:
    return entry.get("summary") is None


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
        api_key = (os.environ.get("ANTHROPIC_API_KEY") or "").strip()
        if not api_key:
            raise SystemExit("Missing required environment variable: ANTHROPIC_API_KEY")
        client = Anthropic(api_key=api_key)
        for case, entry in pending:
            result = summarize_entry(client, case, entry)
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

    if last_run.get("discovery_complete", True) and last_run.get("docket_update_complete", True):
        last_run["last_run_date"] = today
    else:
        print("Warning: last_run_date was not advanced because discovery or docket update was rate-limited.")
    last_run["summaries_generated"] = generated
    last_run.setdefault("cases_discovered", 0)
    last_run.setdefault("entries_updated", 0)

    write_json(CASES_PATH, cases)
    write_json(UPDATES_PATH, updates)
    write_json(LAST_RUN_PATH, last_run)

    print(f"Generated {generated} summaries.")


if __name__ == "__main__":
    main()
