from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from contextlib import ExitStack
from datetime import date
from pathlib import Path
from typing import Any, Callable
from unittest import mock

from scripts import run_discovery_passes as runner


class DiscoveryPassRunnerTests(unittest.TestCase):
    def write_json(self, path: Path, value: object) -> None:
        path.write_text(json.dumps(value), encoding="utf-8")

    def run_with_data_result(
        self,
        initial_state: dict[str, object],
        fake_run: Callable[[str], None],
        *,
        max_passes: str = "20",
        monotonic: Any = None,
        initial_cases: list[dict[str, object]] | None = None,
    ) -> tuple[dict[str, object], list[dict[str, object]]]:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            state_path = data_dir / "last_run.json"
            cases_path = data_dir / "cases.json"
            self.write_json(state_path, initial_state)
            self.write_json(cases_path, initial_cases or [])

            with ExitStack() as stack:
                stack.enter_context(mock.patch.object(runner, "LAST_RUN_PATH", state_path))
                stack.enter_context(mock.patch.object(runner, "CASES_PATH", cases_path))
                stack.enter_context(
                    mock.patch.object(
                        runner,
                        "run_script",
                        side_effect=lambda script, _timeout: fake_run(script),
                    )
                )
                stack.enter_context(mock.patch.object(runner, "utc_today", return_value=date(2026, 7, 22)))
                stack.enter_context(
                    mock.patch.dict(
                        runner.os.environ,
                        {
                            "MAX_DISCOVERY_PASSES_PER_JOB": max_passes,
                            "MAX_DISCOVERY_JOB_SECONDS": "2700",
                        },
                        clear=False,
                    )
                )
                if monotonic is not None:
                    stack.enter_context(
                        mock.patch.object(runner.time, "monotonic", side_effect=monotonic)
                    )
                self.assertEqual(runner.main(), 0)

            final_state = json.loads(state_path.read_text(encoding="utf-8"))
            final_cases = json.loads(cases_path.read_text(encoding="utf-8"))
            return final_state, final_cases

    def run_with_data(
        self,
        initial_state: dict[str, object],
        fake_run: Callable[[str], None],
        **kwargs: Any,
    ) -> dict[str, object]:
        return self.run_with_data_result(initial_state, fake_run, **kwargs)[0]

    def incomplete_state(self) -> dict[str, object]:
        return {
            "discovery_complete": False,
            "discovery_incomplete_reason": "candidate_cap",
            "discovery_candidate_cap_reached": True,
            "courtlistener_rate_limited": False,
            "rejected_dockets": [],
            "discovery_cursor": {
                "phase": "queries",
                "next_query_index": 0,
                "query_page_url": "",
                "pending_candidates": [],
            },
        }

    def test_candidate_cap_passes_continue_until_complete(self) -> None:
        calls = 0

        def fake_run(_script: str) -> None:
            nonlocal calls
            calls += 1
            if calls == 1:
                state = self.incomplete_state()
                state["cases_discovered"] = 0
                state["rejected_dockets"] = ["id:1"]
                state["discovery_cursor"]["next_query_index"] = 1  # type: ignore[index]
                runner.write_last_run(state)
                return
            runner.write_last_run(
                {
                    "cases_discovered": 1,
                    "discovery_complete": True,
                    "discovery_last_run_date": "2026-07-22",
                    "discovery_candidate_cap_reached": False,
                    "courtlistener_rate_limited": False,
                    "rejected_dockets": ["id:1"],
                }
            )
            self.write_json(runner.CASES_PATH, [{"id": "accepted-case"}])

        final = self.run_with_data(self.incomplete_state(), fake_run)
        self.assertEqual(calls, 2)
        self.assertTrue(final["discovery_complete"])
        self.assertEqual(final["cases_discovered"], 1)
        self.assertEqual(final["discovery_passes_run"], 2)
        self.assertEqual(final["discovery_job_stop_reason"], "complete")

    def test_stale_anchored_completion_starts_the_current_sweep(self) -> None:
        calls = 0

        def fake_run(_script: str) -> None:
            nonlocal calls
            calls += 1
            runner.write_last_run(
                {
                    "cases_discovered": 0,
                    "discovery_complete": True,
                    "discovery_last_run_date": "2026-07-21" if calls == 1 else "2026-07-22",
                    "discovery_candidate_cap_reached": False,
                    "courtlistener_rate_limited": False,
                    "rejected_dockets": [],
                }
            )

        final = self.run_with_data(self.incomplete_state(), fake_run)
        self.assertEqual(calls, 2)
        self.assertEqual(final["discovery_last_run_date"], "2026-07-22")
        self.assertEqual(final["discovery_job_stop_reason"], "complete")

    def test_rate_limit_stops_even_when_candidate_cap_is_true(self) -> None:
        calls = 0

        def fake_run(_script: str) -> None:
            nonlocal calls
            calls += 1
            state = self.incomplete_state()
            state["courtlistener_rate_limited"] = True
            state["discovery_incomplete_reason"] = "rate_limit"
            runner.write_last_run(state)

        final = self.run_with_data(self.incomplete_state(), fake_run)
        self.assertEqual(calls, 1)
        self.assertEqual(final["discovery_job_stop_reason"], "rate_limit")

    def test_same_cursor_with_new_rejection_counts_as_progress(self) -> None:
        calls = 0

        def fake_run(_script: str) -> None:
            nonlocal calls
            calls += 1
            state = self.incomplete_state()
            state["rejected_dockets"] = [f"id:{calls}"]
            if calls == 2:
                state.pop("discovery_cursor")
                state["discovery_complete"] = True
                state["discovery_last_run_date"] = "2026-07-22"
                state["discovery_candidate_cap_reached"] = False
                state["discovery_incomplete_reason"] = ""
            runner.write_last_run(state)

        final = self.run_with_data(self.incomplete_state(), fake_run)
        self.assertEqual(calls, 2)
        self.assertEqual(final["discovery_job_stop_reason"], "complete")

    def test_classifier_failure_defers_durable_pending_work(self) -> None:
        calls = 0

        def fake_run(_script: str) -> None:
            nonlocal calls
            calls += 1
            state = self.incomplete_state()
            state["discovery_incomplete_reason"] = "classification"
            state["discovery_cursor"]["pending_candidates"] = [  # type: ignore[index]
                {"docket_id": "1"}
            ]
            runner.write_last_run(state)

        final = self.run_with_data(self.incomplete_state(), fake_run)
        self.assertEqual(calls, 1)
        self.assertEqual(final["discovery_job_stop_reason"], "classification")

    def test_non_cap_incomplete_reason_is_not_retried(self) -> None:
        calls = 0

        def fake_run(_script: str) -> None:
            nonlocal calls
            calls += 1
            state = self.incomplete_state()
            state["discovery_candidate_cap_reached"] = False
            state["discovery_incomplete_reason"] = "source_collection"
            runner.write_last_run(state)

        final = self.run_with_data(self.incomplete_state(), fake_run)
        self.assertEqual(calls, 1)
        self.assertEqual(final["discovery_job_stop_reason"], "source_collection")

    def test_no_progress_guard_prevents_retry_loop(self) -> None:
        calls = 0

        def fake_run(_script: str) -> None:
            nonlocal calls
            calls += 1

        final = self.run_with_data(self.incomplete_state(), fake_run)
        self.assertEqual(calls, 1)
        self.assertEqual(final["discovery_job_stop_reason"], "no_progress")

    def test_pass_limit_publishes_aggregate_progress(self) -> None:
        calls = 0

        def fake_run(_script: str) -> None:
            nonlocal calls
            calls += 1
            state = self.incomplete_state()
            state["cases_discovered"] = 1
            state["discovery_cursor"]["next_query_index"] = calls  # type: ignore[index]
            runner.write_last_run(state)
            self.write_json(runner.CASES_PATH, [{"id": f"case-{index}"} for index in range(calls)])

        final = self.run_with_data(self.incomplete_state(), fake_run, max_passes="2")
        self.assertEqual(calls, 2)
        self.assertEqual(final["cases_discovered"], 2)
        self.assertEqual(final["discovery_passes_run"], 2)
        self.assertEqual(final["max_discovery_passes_per_job"], 2)
        self.assertEqual(final["discovery_job_stop_reason"], "pass_limit")

    def test_wall_clock_budget_stops_before_another_pass(self) -> None:
        calls = 0

        def fake_run(_script: str) -> None:
            nonlocal calls
            calls += 1
            state = self.incomplete_state()
            state["discovery_cursor"]["next_query_index"] = 1  # type: ignore[index]
            runner.write_last_run(state)

        final = self.run_with_data(
            self.incomplete_state(),
            fake_run,
            monotonic=[0.0, 0.0, 4000.0],
        )
        self.assertEqual(calls, 1)
        self.assertEqual(final["discovery_job_stop_reason"], "time_budget")

    def test_timed_out_child_rolls_back_partial_data(self) -> None:
        initial_cases = [{"id": "original-case"}]

        def fake_run(script: str) -> None:
            runner.write_last_run({"partial_child_state": True})
            self.write_json(runner.CASES_PATH, [{"id": "partial-case"}])
            raise subprocess.TimeoutExpired(script, timeout=2700)

        final, final_cases = self.run_with_data_result(
            self.incomplete_state(), fake_run, initial_cases=initial_cases
        )
        self.assertNotIn("partial_child_state", final)
        self.assertEqual(final_cases, initial_cases)
        self.assertEqual(final["discovery_passes_run"], 0)
        self.assertEqual(final["discovery_job_stop_reason"], "time_budget")


    def test_run_script_enforces_child_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "blocking_child.py"
            script.write_text("import time\ntime.sleep(5)\n", encoding="utf-8")
            with self.assertRaises(subprocess.TimeoutExpired):
                runner.run_script(str(script), timeout_seconds=0.05)

    def test_child_failure_remains_fatal(self) -> None:
        def fake_run(script: str) -> None:
            raise subprocess.CalledProcessError(1, ["python", script])

        with self.assertRaises(subprocess.CalledProcessError):
            self.run_with_data(self.incomplete_state(), fake_run)


if __name__ == "__main__":
    unittest.main()
