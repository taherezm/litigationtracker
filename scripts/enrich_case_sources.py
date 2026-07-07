#!/usr/bin/env python3
"""Backfill complaint source documents and refresh case intelligence."""

from __future__ import annotations

import argparse
import os
import sys
from typing import Any

from dotenv import load_dotenv

try:
    from scripts.case_intelligence import refresh_all_case_intelligence
    from scripts.source_documents import (
        CASES_PATH,
        DEFAULT_MAX_SOURCE_DOCUMENT_CASES_PER_RUN,
        DEFAULT_MAX_SOURCE_DOCUMENTS_PER_CASE,
        DEFAULT_SOURCE_DOCUMENT_REFRESH_DAYS,
        LAST_RUN_PATH,
        UPDATES_PATH,
        enrich_cases,
        env_int,
        load_json,
        write_json,
    )
    from scripts.validate_tracker_data import validate_cases, validate_pipeline_state, validate_updates
except ModuleNotFoundError:  # pragma: no cover - supports direct script execution.
    from case_intelligence import refresh_all_case_intelligence  # type: ignore
    from source_documents import (  # type: ignore
        CASES_PATH,
        DEFAULT_MAX_SOURCE_DOCUMENT_CASES_PER_RUN,
        DEFAULT_MAX_SOURCE_DOCUMENTS_PER_CASE,
        DEFAULT_SOURCE_DOCUMENT_REFRESH_DAYS,
        LAST_RUN_PATH,
        UPDATES_PATH,
        enrich_cases,
        env_int,
        load_json,
        write_json,
    )
    from validate_tracker_data import validate_cases, validate_pipeline_state, validate_updates  # type: ignore


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--max-cases",
        type=int,
        default=env_int(
            "MAX_SOURCE_DOCUMENT_CASES_PER_RUN",
            DEFAULT_MAX_SOURCE_DOCUMENT_CASES_PER_RUN,
            minimum=0,
        ),
        help="maximum number of cases to attempt in this run",
    )
    parser.add_argument(
        "--max-documents-per-case",
        type=int,
        default=env_int(
            "MAX_SOURCE_DOCUMENTS_PER_CASE",
            DEFAULT_MAX_SOURCE_DOCUMENTS_PER_CASE,
            minimum=1,
        ),
        help="maximum source complaint documents to store per case",
    )
    parser.add_argument(
        "--refresh-days",
        type=int,
        default=env_int(
            "SOURCE_DOCUMENT_REFRESH_DAYS",
            DEFAULT_SOURCE_DOCUMENT_REFRESH_DAYS,
            minimum=1,
        ),
        help="age in days after which existing source documents may be refreshed",
    )
    parser.add_argument("--force", action="store_true", help="refresh even when source documents already exist")
    return parser.parse_args()


def main() -> int:
    load_dotenv()
    args = parse_args()
    cases = load_json(CASES_PATH, [])
    updates = load_json(UPDATES_PATH, [])
    last_run = load_json(LAST_RUN_PATH, {})

    if not isinstance(cases, list):
        print("cases.json must contain a list.", file=sys.stderr)
        return 1
    if not isinstance(updates, list):
        updates = []
    if not isinstance(last_run, dict):
        last_run = {}

    api_key = (os.environ.get("COURTLISTENER_API_KEY") or "").strip()
    if not api_key:
        last_run["source_documents_enriched"] = 0
        last_run["source_documents_complete"] = False
        last_run["source_documents_rate_limited"] = False
        last_run["source_documents_skipped_reason"] = "missing_courtlistener_api_key"
        write_json(LAST_RUN_PATH, last_run)
        refresh_all_case_intelligence(cases, updates)
        write_json(CASES_PATH, cases)
        print("Skipped source document enrichment: missing COURTLISTENER_API_KEY.")
        return 0

    enriched, rate_limited = enrich_cases(
        cases,
        api_key=api_key,
        max_cases=max(0, args.max_cases),
        max_documents_per_case=max(1, args.max_documents_per_case),
        force=bool(args.force),
        refresh_days=max(1, args.refresh_days),
    )
    refresh_all_case_intelligence(cases, updates)
    write_json(CASES_PATH, cases)

    last_run["source_documents_enriched"] = enriched
    last_run["source_documents_complete"] = not rate_limited
    last_run["source_documents_rate_limited"] = rate_limited
    last_run["max_source_document_cases_per_run"] = max(0, args.max_cases)
    last_run.pop("source_documents_skipped_reason", None)
    write_json(LAST_RUN_PATH, last_run)

    errors: list[str] = []
    errors.extend(validate_cases(cases))
    errors.extend(validate_updates(updates))
    errors.extend(validate_pipeline_state(last_run))
    if errors:
        print("Source document enrichment completed, but validation failed:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1

    print(f"Enriched source documents for {enriched} cases.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

