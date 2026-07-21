#!/usr/bin/env python3
"""Regression checks for resumable, gap-free discovery cycles."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from contextlib import ExitStack
from datetime import date
from pathlib import Path
from urllib.parse import urlencode
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts import discover_cases  # noqa: E402
from scripts.cl_client import RateLimitExceeded  # noqa: E402


def search_result(number: int) -> dict[str, str]:
    return {
        "docket_number": f"1:26-cv-{number:05d}",
        "docket_id": str(number),
        "caseName": f"Author {number} v. AI Company",
        "court": "cand",
        "dateFiled": "2026-07-19",
        "snippet": "A newly filed technology dispute.",
    }


def search_page_url(
    query: str,
    cursor: str,
    search_after: str | None = "2026-07-10",
    search_before: str | None = "2026-07-20",
) -> str:
    params = {
        "q": query,
        "type": "d",
        "order_by": "score desc",
        "page_size": "20",
        "cursor": cursor,
    }
    if search_after:
        params["filed_after"] = search_after
    if search_before:
        params["filed_before"] = search_before
    return f"https://www.courtlistener.com/api/rest/v4/search/?{urlencode(params)}"


class DiscoveryCursorTests(unittest.TestCase):
    def test_rate_limited_query_is_the_next_query_on_resume(self) -> None:
        queries = ["q0", "q1", "q2"]
        first_calls: list[str] = []

        def first_search(
            _client: object,
            query: str,
            _after: str,
            _before: str,
            _page_url: str,
        ) -> tuple[list[dict[str, str]], str]:
            first_calls.append(query)
            if query == "q1":
                raise RateLimitExceeded("hourly budget")
            return [], ""

        with patch.object(discover_cases, "search_case_page", first_search):
            _, cursor, page_url, rate_limited, cap_reached = discover_cases.collect_query_candidates(
                object(), "2026-07-10", "2026-07-20", set(), 5, 0, queries=queries
            )

        self.assertEqual(first_calls, ["q0", "q1"])
        self.assertEqual(cursor, 1)
        self.assertTrue(rate_limited)
        self.assertFalse(cap_reached)
        self.assertEqual(page_url, "")

        resumed_calls: list[str] = []

        def resumed_search(
            _client: object,
            query: str,
            _after: str,
            _before: str,
            _page_url: str,
        ) -> tuple[list[dict[str, str]], str]:
            resumed_calls.append(query)
            return [], ""

        with patch.object(discover_cases, "search_case_page", resumed_search):
            _, cursor, page_url, rate_limited, cap_reached = discover_cases.collect_query_candidates(
                object(), "2026-07-10", "2026-07-20", set(), 5, cursor, queries=queries
            )

        self.assertEqual(resumed_calls, ["q1", "q2"])
        self.assertEqual(cursor, len(queries))
        self.assertFalse(rate_limited)
        self.assertFalse(cap_reached)
        self.assertEqual(page_url, "")

    def test_candidate_cap_retries_and_drains_the_current_query(self) -> None:
        results = [search_result(1), search_result(2), search_result(3)]
        with patch.object(discover_cases, "search_case_page", return_value=(results, "")):
            candidates, cursor, page_url, _, cap_reached = discover_cases.collect_query_candidates(
                object(), "2026-07-10", "2026-07-20", set(), 2, 0, queries=["q0"]
            )

        self.assertEqual(cursor, 0)
        self.assertTrue(cap_reached)
        self.assertEqual(len(candidates), 2)
        self.assertEqual(page_url, "")

        processed = set(candidates)
        with patch.object(discover_cases, "search_case_page", return_value=(results, "")):
            remaining, cursor, page_url, _, cap_reached = discover_cases.collect_query_candidates(
                object(), "2026-07-10", "2026-07-20", processed, 2, cursor, queries=["q0"]
            )

        self.assertEqual(cursor, 1)
        self.assertFalse(cap_reached)
        self.assertEqual(list(remaining), ["id:3"])
        self.assertEqual(page_url, "")

    def test_query_walks_every_page_before_advancing(self) -> None:
        page_two = search_page_url("q0", "second")
        calls: list[tuple[str, str]] = []

        def paged_search(
            _client: object,
            query: str,
            _after: str,
            _before: str,
            page_url: str,
        ) -> tuple[list[dict[str, str]], str]:
            calls.append((query, page_url))
            if query == "q0" and not page_url:
                return [search_result(1)], page_two
            if query == "q0" and page_url == page_two:
                return [search_result(2)], ""
            return [], ""

        with patch.object(discover_cases, "search_case_page", paged_search):
            candidates, cursor, page_url, rate_limited, cap_reached = (
                discover_cases.collect_query_candidates(
                    object(),
                    "2026-07-10",
                    "2026-07-20",
                    set(),
                    5,
                    0,
                    queries=["q0", "q1"],
                )
            )

        self.assertEqual(calls, [("q0", ""), ("q0", page_two), ("q1", "")])
        self.assertEqual(list(candidates), ["id:1", "id:2"])
        self.assertEqual(cursor, 2)
        self.assertEqual(page_url, "")
        self.assertFalse(rate_limited)
        self.assertFalse(cap_reached)

    def test_query_rate_limit_on_page_two_resumes_at_page_two(self) -> None:
        page_two = search_page_url("q0", "second")
        first_calls: list[str] = []

        def first_search(
            _client: object,
            _query: str,
            _after: str,
            _before: str,
            page_url: str,
        ) -> tuple[list[dict[str, str]], str]:
            first_calls.append(page_url)
            if not page_url:
                return [search_result(1)], page_two
            raise RateLimitExceeded("hourly budget")

        with patch.object(discover_cases, "search_case_page", first_search):
            candidates, cursor, page_url, rate_limited, cap_reached = (
                discover_cases.collect_query_candidates(
                    object(), "2026-07-10", "2026-07-20", set(), 5, 0, queries=["q0"]
                )
            )

        self.assertEqual(first_calls, ["", page_two])
        self.assertEqual(cursor, 0)
        self.assertEqual(page_url, page_two)
        self.assertTrue(rate_limited)
        self.assertFalse(cap_reached)

        resumed_calls: list[str] = []

        def resumed_search(
            _client: object,
            _query: str,
            _after: str,
            _before: str,
            requested_page_url: str,
        ) -> tuple[list[dict[str, str]], str]:
            resumed_calls.append(requested_page_url)
            return [search_result(2)], ""

        with patch.object(discover_cases, "search_case_page", resumed_search):
            remaining, cursor, page_url, rate_limited, cap_reached = (
                discover_cases.collect_query_candidates(
                    object(),
                    "2026-07-10",
                    "2026-07-20",
                    set(candidates),
                    5,
                    cursor,
                    page_url,
                    queries=["q0"],
                )
            )

        self.assertEqual(resumed_calls, [page_two])
        self.assertEqual(list(remaining), ["id:2"])
        self.assertEqual(cursor, 1)
        self.assertEqual(page_url, "")
        self.assertFalse(rate_limited)
        self.assertFalse(cap_reached)

    def test_candidate_cap_on_page_two_refetches_and_drains_that_page(self) -> None:
        page_two = search_page_url("q0", "second")

        def paged_search(
            _client: object,
            _query: str,
            _after: str,
            _before: str,
            page_url: str,
        ) -> tuple[list[dict[str, str]], str]:
            if not page_url:
                return [search_result(1)], page_two
            return [search_result(2), search_result(3)], ""

        with patch.object(discover_cases, "search_case_page", paged_search):
            candidates, cursor, page_url, _, cap_reached = discover_cases.collect_query_candidates(
                object(), "2026-07-10", "2026-07-20", set(), 2, 0, queries=["q0"]
            )
        self.assertEqual(list(candidates), ["id:1", "id:2"])
        self.assertEqual(cursor, 0)
        self.assertEqual(page_url, page_two)
        self.assertTrue(cap_reached)

        with patch.object(discover_cases, "search_case_page", paged_search):
            remaining, cursor, page_url, _, cap_reached = discover_cases.collect_query_candidates(
                object(),
                "2026-07-10",
                "2026-07-20",
                set(candidates),
                2,
                cursor,
                page_url,
                queries=["q0"],
            )
        self.assertEqual(list(remaining), ["id:3"])
        self.assertEqual(cursor, 1)
        self.assertEqual(page_url, "")
        self.assertFalse(cap_reached)

    def test_same_docket_number_in_different_courts_is_not_collapsed(self) -> None:
        first = search_result(11)
        second = search_result(12)
        second["docket_number"] = first["docket_number"]
        second["court"] = "nysd"

        with patch.object(discover_cases, "search_case_page", return_value=([first, second], "")):
            candidates, cursor, _, _, _ = discover_cases.collect_query_candidates(
                object(),
                "2026-07-10",
                "2026-07-20",
                {"id:11"},
                5,
                0,
                queries=["q0"],
            )

        self.assertEqual(cursor, 1)
        self.assertEqual(list(candidates), ["id:12"])

    def test_rss_lookup_resumes_at_the_rate_limited_docket(self) -> None:
        docket_numbers = ["1:26-cv-00001", "1:26-cv-00002", "1:26-cv-00003"]
        calls: list[str] = []

        def first_search(
            _client: object,
            query: str,
            search_after: str | None,
            search_before: str | None,
            page_url: str,
        ) -> tuple[list[dict[str, str]], str]:
            del search_after, search_before, page_url
            calls.append(query)
            if query == docket_numbers[1]:
                raise RateLimitExceeded("hourly budget")
            return [search_result(1)], ""

        with patch.object(discover_cases, "search_case_page", first_search):
            candidates, cursor, page_url, rate_limited, cap_reached = discover_cases.collect_rss_candidates(
                object(),
                docket_numbers,
                set(),
                5,
            )

        self.assertEqual(calls, docket_numbers[:2])
        self.assertEqual(cursor, 1)
        self.assertTrue(rate_limited)
        self.assertFalse(cap_reached)
        self.assertEqual(page_url, "")
        self.assertEqual(len(candidates), 1)

        calls.clear()
        with patch.object(discover_cases, "search_case_page", return_value=([], "")) as resumed_search:
            _, cursor, page_url, rate_limited, cap_reached = discover_cases.collect_rss_candidates(
                object(),
                docket_numbers,
                set(candidates),
                5,
                cursor,
            )

        self.assertEqual([call.args[1] for call in resumed_search.call_args_list], docket_numbers[1:])
        self.assertEqual(cursor, len(docket_numbers))
        self.assertFalse(rate_limited)
        self.assertFalse(cap_reached)
        self.assertEqual(page_url, "")

    def test_rss_rate_limit_on_page_two_resumes_at_page_two(self) -> None:
        docket_numbers = ["1:26-cv-00001"]
        page_two = search_page_url(docket_numbers[0], "rss-second", None, None)

        def first_search(
            _client: object,
            _query: str,
            search_after: str | None,
            search_before: str | None,
            page_url: str,
        ) -> tuple[list[dict[str, str]], str]:
            del search_after, search_before
            if not page_url:
                return [search_result(1)], page_two
            raise RateLimitExceeded("hourly budget")

        with patch.object(discover_cases, "search_case_page", first_search):
            candidates, cursor, page_url, rate_limited, cap_reached = (
                discover_cases.collect_rss_candidates(object(), docket_numbers, set(), 5)
            )
        self.assertEqual(list(candidates), ["id:1"])
        self.assertEqual(cursor, 0)
        self.assertEqual(page_url, page_two)
        self.assertTrue(rate_limited)
        self.assertFalse(cap_reached)

        resumed_pages: list[str] = []

        def resumed_search(
            _client: object,
            _query: str,
            search_after: str | None,
            search_before: str | None,
            page_url: str,
        ) -> tuple[list[dict[str, str]], str]:
            del search_after, search_before
            resumed_pages.append(page_url)
            return [search_result(2)], ""

        with patch.object(discover_cases, "search_case_page", resumed_search):
            remaining, cursor, page_url, rate_limited, cap_reached = (
                discover_cases.collect_rss_candidates(
                    object(), docket_numbers, set(candidates), 5, cursor, page_url
                )
            )
        self.assertEqual(resumed_pages, [page_two])
        self.assertEqual(list(remaining), ["id:2"])
        self.assertEqual(cursor, 1)
        self.assertEqual(page_url, "")
        self.assertFalse(rate_limited)
        self.assertFalse(cap_reached)

    def test_search_page_url_is_restricted_to_courtlistener_search(self) -> None:
        valid = "/api/rest/v4/search/?cursor=abc"
        self.assertEqual(
            discover_cases.normalized_search_page_url(valid),
            "https://www.courtlistener.com/api/rest/v4/search/?cursor=abc",
        )
        for unsafe in (
            "http://www.courtlistener.com/api/rest/v4/search/?cursor=abc",
            "https://evil.example/api/rest/v4/search/?cursor=abc",
            "https://www.courtlistener.com/api/rest/v4/dockets/?cursor=abc",
        ):
            with self.subTest(url=unsafe):
                self.assertEqual(discover_cases.normalized_search_page_url(unsafe), "")

        bound_url = search_page_url("q0", "abc")
        self.assertEqual(
            discover_cases.validated_search_page_url(
                bound_url,
                "q0",
                "2026-07-10",
                "2026-07-20",
            ),
            bound_url,
        )
        self.assertEqual(
            discover_cases.validated_search_page_url(
                bound_url,
                "different-query",
                "2026-07-10",
                "2026-07-20",
            ),
            "",
        )
        self.assertEqual(
            discover_cases.validated_search_page_url(
                f"{bound_url}&court=ca9",
                "q0",
                "2026-07-10",
                "2026-07-20",
            ),
            "",
        )
        self.assertEqual(
            discover_cases.validated_search_page_url(
                bound_url,
                "q0",
                "2026-07-11",
                "2026-07-20",
            ),
            "",
        )

    def test_legacy_and_hash_mismatched_cursors_restart_conservatively(self) -> None:
        legacy = {"discovery_last_run_date": "2026-07-10", "discovery_complete": False}
        with patch.object(discover_cases, "utc_today", return_value=date(2026, 7, 20)):
            cursor = discover_cases.discovery_cursor([], legacy)
        self.assertEqual(cursor["window_start"], "2026-07-10")
        self.assertEqual(cursor["window_through"], "2026-07-20")
        self.assertEqual(cursor["next_query_index"], 0)

    def test_corrupted_matching_cursor_cannot_skip_anchored_work(self) -> None:
        base_state = {
            "discovery_last_run_date": "2026-07-10",
            "discovery_complete": False,
        }
        base_cursor = {
            "version": discover_cases.DISCOVERY_CURSOR_VERSION,
            "window_start": "2026-07-10",
            "window_through": "2026-07-20",
            "query_set_sha256": discover_cases.DISCOVERY_QUERY_SET_HASH,
            "phase": "queries",
            "next_query_index": 0,
            "pending_candidates": [],
        }

        with patch.object(discover_cases, "utc_today", return_value=date(2026, 7, 20)):
            oversized_query = discover_cases.discovery_cursor(
                [],
                {
                    **base_state,
                    "discovery_cursor": {
                        **base_cursor,
                        "next_query_index": 999,
                        "query_page_url": search_page_url(
                            discover_cases.SEARCH_QUERIES[0],
                            "must-not-skip-page-one",
                        ),
                    },
                },
            )
        self.assertEqual(oversized_query["phase"], "queries")
        self.assertEqual(oversized_query["next_query_index"], 0)
        self.assertEqual(oversized_query["query_page_url"], "")

        with patch.object(discover_cases, "utc_today", return_value=date(2026, 7, 20)):
            oversized_rss = discover_cases.discovery_cursor(
                [],
                {
                    **base_state,
                    "discovery_cursor": {
                        **base_cursor,
                        "phase": "rss",
                        "next_query_index": len(discover_cases.SEARCH_QUERIES),
                        "rss_docket_numbers": ["1:26-cv-00001", "1:26-cv-00002"],
                        "next_rss_index": 999,
                        "rss_page_url": search_page_url(
                            "1:26-cv-00001",
                            "must-not-skip-rss-page-one",
                            None,
                            None,
                        ),
                    },
                },
            )
        self.assertEqual(oversized_rss["phase"], "rss")
        self.assertEqual(oversized_rss["next_rss_index"], 0)
        self.assertEqual(oversized_rss["rss_page_url"], "")

        with patch.object(discover_cases, "utc_today", return_value=date(2026, 7, 20)):
            later_anchor = discover_cases.discovery_cursor(
                [],
                {
                    **base_state,
                    "discovery_cursor": {
                        **base_cursor,
                        "window_start": "2026-07-15",
                        "window_through": "2026-07-19",
                        "next_query_index": 20,
                    },
                },
            )
        self.assertEqual(later_anchor["window_start"], "2026-07-10")
        self.assertEqual(later_anchor["window_through"], "2026-07-19")
        self.assertEqual(later_anchor["next_query_index"], 0)

        with patch.object(discover_cases, "utc_today", return_value=date(2026, 7, 20)):
            malformed_pending = discover_cases.discovery_cursor(
                [],
                {
                    **base_state,
                    "discovery_cursor": {
                        **base_cursor,
                        "phase": "rss",
                        "next_query_index": len(discover_cases.SEARCH_QUERIES),
                        "rss_docket_numbers": [],
                        "next_rss_index": 0,
                        "pending_candidates": [{"docket_id": "9"}],
                    },
                },
            )
        self.assertTrue(malformed_pending["pending_state_invalid"])
        self.assertEqual(malformed_pending["phase"], "queries")
        self.assertEqual(malformed_pending["next_query_index"], 0)

        valid_page = search_page_url(discover_cases.SEARCH_QUERIES[0], "safe")
        wrong_window_page = search_page_url(
            discover_cases.SEARCH_QUERIES[0],
            "wrong-window",
            "2026-07-11",
            "2026-07-20",
        )
        with patch.object(discover_cases, "utc_today", return_value=date(2026, 7, 20)):
            valid_page_cursor = discover_cases.discovery_cursor(
                [],
                {**base_state, "discovery_cursor": {**base_cursor, "query_page_url": valid_page}},
            )
            wrong_page_cursor = discover_cases.discovery_cursor(
                [],
                {
                    **base_state,
                    "discovery_cursor": {**base_cursor, "query_page_url": wrong_window_page},
                },
            )
        self.assertEqual(valid_page_cursor["query_page_url"], valid_page)
        self.assertEqual(wrong_page_cursor["query_page_url"], "")

        legacy = base_state
        mismatched = {
            **legacy,
            "discovery_cursor": {
                "version": 1,
                "window_start": "2026-07-01",
                "window_through": "2026-07-15",
                "query_set_sha256": "old-query-set",
                "phase": "queries",
                "next_query_index": 20,
            },
        }
        with patch.object(discover_cases, "utc_today", return_value=date(2026, 7, 20)):
            cursor = discover_cases.discovery_cursor([], mismatched)
        self.assertEqual(cursor["window_start"], "2026-07-01")
        self.assertEqual(cursor["window_through"], "2026-07-15")
        self.assertEqual(cursor["next_query_index"], 0)

        later_mismatched = {
            **legacy,
            "discovery_cursor": {
                "version": 1,
                "window_start": "2026-07-15",
                "window_through": "2026-07-19",
                "query_set_sha256": "old-query-set",
                "phase": "queries",
                "next_query_index": 20,
                "query_page_url": "https://www.courtlistener.com/api/rest/v4/search/?cursor=unsafe-skip",
            },
        }
        with patch.object(discover_cases, "utc_today", return_value=date(2026, 7, 20)):
            cursor = discover_cases.discovery_cursor([], later_mismatched)
        self.assertEqual(cursor["window_start"], "2026-07-10")
        self.assertEqual(cursor["window_through"], "2026-07-19")
        self.assertEqual(cursor["next_query_index"], 0)
        self.assertEqual(cursor["query_page_url"], "")

        future_mismatched = {
            **legacy,
            "discovery_cursor": {
                "version": 1,
                "window_start": "2026-07-30",
                "window_through": "2026-07-31",
                "query_set_sha256": "old-query-set",
                "phase": "queries",
                "next_query_index": 20,
            },
        }
        with patch.object(discover_cases, "utc_today", return_value=date(2026, 7, 20)):
            cursor = discover_cases.discovery_cursor([], future_mismatched)
        self.assertEqual(cursor["window_start"], "2026-07-10")
        self.assertEqual(cursor["window_through"], "2026-07-20")
        self.assertEqual(cursor["next_query_index"], 0)

    def test_same_day_checkpoint_does_not_hide_an_active_cursor(self) -> None:
        with patch.object(discover_cases, "utc_today", return_value=date(2026, 7, 20)):
            self.assertFalse(
                discover_cases.discovery_already_current(
                    {
                        "discovery_last_run_date": "2026-07-20",
                        "discovery_complete": True,
                        "discovery_cursor": {"version": 1},
                    }
                )
            )

    def test_standalone_discovery_clears_prior_run_rate_limit_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            data_dir = Path(tmp_dir)
            cases_path = data_dir / "cases.json"
            last_run_path = data_dir / "last_run.json"
            cases_path.write_text("[]", encoding="utf-8")
            last_run_path.write_text(
                json.dumps(
                    {
                        "discovery_last_run_date": "2026-07-20",
                        "discovery_complete": True,
                        "courtlistener_rate_limited": True,
                    }
                ),
                encoding="utf-8",
            )

            with (
                patch.object(discover_cases, "CASES_PATH", cases_path),
                patch.object(discover_cases, "LAST_RUN_PATH", last_run_path),
                patch.object(discover_cases, "utc_today", return_value=date(2026, 7, 20)),
                patch.dict(
                    os.environ,
                    {"RESET_COURTLISTENER_RATE_LIMIT_STATE": "true"},
                    clear=False,
                ),
            ):
                discover_cases.main()

            state = json.loads(last_run_path.read_text(encoding="utf-8"))
            self.assertFalse(state["courtlistener_rate_limited"])

    def test_cursor_persists_and_completion_uses_anchored_through_date(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            data_dir = Path(tmp_dir)
            cases_path = data_dir / "cases.json"
            last_run_path = data_dir / "last_run.json"
            cases_path.write_text("[]", encoding="utf-8")
            last_run_path.write_text(
                json.dumps({"discovery_last_run_date": "2026-07-10", "discovery_complete": False}),
                encoding="utf-8",
            )

            first_calls: list[str] = []

            def first_search(
                _client: object,
                query: str,
                _after: str,
                _before: str,
                _page_url: str,
            ) -> tuple[list[dict[str, str]], str]:
                first_calls.append(query)
                if query == "q1":
                    raise RateLimitExceeded("hourly budget")
                return [], ""

            def enter_common_patches(stack: ExitStack) -> None:
                stack.enter_context(patch.object(discover_cases, "CASES_PATH", cases_path))
                stack.enter_context(patch.object(discover_cases, "LAST_RUN_PATH", last_run_path))
                stack.enter_context(patch.object(discover_cases, "SEARCH_QUERIES", ["q0", "q1", "q2"]))
                stack.enter_context(patch.object(discover_cases, "CourtListenerClient"))
                stack.enter_context(patch.object(discover_cases, "Anthropic"))
                stack.enter_context(patch.object(discover_cases, "rss_docket_numbers", return_value=[]))
                stack.enter_context(
                    patch.dict(
                        os.environ,
                        {
                            "COURTLISTENER_API_KEY": "courtlistener",
                            "ANTHROPIC_API_KEY": "anthropic",
                            "LEGAL_AI_MODEL": "test-model",
                        },
                        clear=False,
                    )
                )

            with ExitStack() as stack:
                enter_common_patches(stack)
                stack.enter_context(patch.object(discover_cases, "utc_today", return_value=date(2026, 7, 20)))
                stack.enter_context(patch.object(discover_cases, "search_case_page", side_effect=first_search))
                discover_cases.main()

            partial = json.loads(last_run_path.read_text(encoding="utf-8"))
            self.assertEqual(first_calls, ["q0", "q1"])
            self.assertFalse(partial["discovery_complete"])
            self.assertEqual(partial["discovery_cursor"]["next_query_index"], 1)
            self.assertEqual(partial["discovery_cursor"]["window_through"], "2026-07-20")

            resumed_calls: list[str] = []

            def resumed_search(
                _client: object,
                query: str,
                _after: str,
                _before: str,
                _page_url: str,
            ) -> tuple[list[dict[str, str]], str]:
                resumed_calls.append(query)
                return [], ""

            with ExitStack() as stack:
                enter_common_patches(stack)
                stack.enter_context(patch.object(discover_cases, "utc_today", return_value=date(2026, 7, 22)))
                stack.enter_context(patch.object(discover_cases, "search_case_page", side_effect=resumed_search))
                discover_cases.main()

            complete = json.loads(last_run_path.read_text(encoding="utf-8"))
            self.assertEqual(resumed_calls, ["q1", "q2"])
            self.assertTrue(complete["discovery_complete"])
            self.assertEqual(complete["discovery_last_run_date"], "2026-07-20")
            self.assertNotIn("discovery_cursor", complete)

    def test_transient_classifier_failure_retries_saved_candidate_without_rediscovery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            data_dir = Path(tmp_dir)
            cases_path = data_dir / "cases.json"
            last_run_path = data_dir / "last_run.json"
            cases_path.write_text("[]", encoding="utf-8")
            last_run_path.write_text(
                json.dumps({"discovery_last_run_date": "2026-07-19", "discovery_complete": True}),
                encoding="utf-8",
            )

            def enter_common_patches(stack: ExitStack, run_date: date) -> None:
                stack.enter_context(patch.object(discover_cases, "CASES_PATH", cases_path))
                stack.enter_context(patch.object(discover_cases, "LAST_RUN_PATH", last_run_path))
                stack.enter_context(patch.object(discover_cases, "SEARCH_QUERIES", ["q0"]))
                stack.enter_context(patch.object(discover_cases, "CourtListenerClient"))
                stack.enter_context(patch.object(discover_cases, "Anthropic"))
                stack.enter_context(patch.object(discover_cases, "utc_today", return_value=run_date))
                stack.enter_context(
                    patch.dict(
                        os.environ,
                        {
                            "COURTLISTENER_API_KEY": "courtlistener",
                            "ANTHROPIC_API_KEY": "anthropic",
                            "LEGAL_AI_MODEL": "test-model",
                        },
                        clear=False,
                    )
                )

            with ExitStack() as stack:
                enter_common_patches(stack, date(2026, 7, 20))
                stack.enter_context(
                    patch.object(discover_cases, "search_case_page", return_value=([search_result(9)], ""))
                )
                stack.enter_context(patch.object(discover_cases, "rss_docket_numbers", return_value=[]))
                stack.enter_context(
                    patch.object(discover_cases, "classify_case", side_effect=RuntimeError("temporary model error"))
                )
                discover_cases.main()

            state = json.loads(last_run_path.read_text(encoding="utf-8"))
            self.assertFalse(state["discovery_complete"])
            self.assertEqual(state["discovery_incomplete_reason"], "classification")
            self.assertEqual(state["discovery_cursor"]["phase"], "rss")
            self.assertEqual(state["discovery_cursor"]["next_query_index"], 1)
            self.assertEqual(
                [discover_cases.candidate_identity(item) for item in state["discovery_cursor"]["pending_candidates"]],
                ["id:9"],
            )
            self.assertNotIn("id:9", state.get("rejected_dockets", []))
            self.assertEqual(json.loads(cases_path.read_text(encoding="utf-8")), [])

            with ExitStack() as stack:
                enter_common_patches(stack, date(2026, 7, 21))
                stack.enter_context(
                    patch.object(
                        discover_cases,
                        "search_case_page",
                        side_effect=AssertionError("saved classifier retries must not depend on rediscovery"),
                    )
                )
                stack.enter_context(
                    patch.object(
                        discover_cases,
                        "rss_docket_numbers",
                        side_effect=AssertionError("the saved RSS snapshot must be reused"),
                    )
                )
                stack.enter_context(
                    patch.object(
                        discover_cases,
                        "classify_case",
                        return_value={
                            "relevant": True,
                            "confidence": "high",
                            "reason": "AI copyright case.",
                            "claims": ["copyright infringement"],
                        },
                    )
                )
                discover_cases.main()

            completed_state = json.loads(last_run_path.read_text(encoding="utf-8"))
            cases = json.loads(cases_path.read_text(encoding="utf-8"))
            self.assertTrue(completed_state["discovery_complete"])
            self.assertEqual(completed_state["discovery_last_run_date"], "2026-07-20")
            self.assertNotIn("discovery_cursor", completed_state)
            self.assertEqual(len(cases), 1)
            self.assertEqual(cases[0]["courtlistener_docket_id"], "9")


if __name__ == "__main__":
    unittest.main()
