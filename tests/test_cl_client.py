#!/usr/bin/env python3
"""Deterministic tests for the budget-aware CourtListener client."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.cl_client import (  # noqa: E402
    CourtListenerClient,
    RateLimitExceeded,
    RequestLedger,
    TierLimits,
)


class FakeClock:
    def __init__(self, start: float = 1_000_000.0) -> None:
        self.now = start

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += max(0.0, seconds)


class FakeResponse:
    def __init__(self, status_code: int = 200, payload: dict[str, Any] | None = None, headers: dict[str, str] | None = None) -> None:
        self.status_code = status_code
        self._payload = payload or {"results": []}
        self.headers = headers or {}

    def json(self) -> dict[str, Any]:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            import requests

            raise requests.HTTPError(f"HTTP {self.status_code}", response=self)  # type: ignore[arg-type]


class FakeSession:
    """Returns queued responses; records the fake time of each send."""

    def __init__(self, clock: FakeClock, responses: list[FakeResponse] | None = None) -> None:
        self.clock = clock
        self.responses = responses or []
        self.sent_at: list[float] = []

    def get(self, url: str, headers: Any = None, params: Any = None, timeout: Any = None) -> FakeResponse:
        self.sent_at.append(self.clock.time())
        if self.responses:
            return self.responses.pop(0)
        return FakeResponse()


def make_client(
    clock: FakeClock,
    ledger_path: Path,
    limits: TierLimits,
    responses: list[FakeResponse] | None = None,
    budget: float = 10_000.0,
) -> tuple[CourtListenerClient, FakeSession]:
    session = FakeSession(clock, responses)
    client = CourtListenerClient(
        "test-key",
        limits=limits,
        ledger=RequestLedger(ledger_path),
        time_budget_seconds=budget,
        session=session,  # type: ignore[arg-type]
        clock=clock.time,
        sleeper=clock.sleep,
    )
    return client, session


class ClClientTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.ledger_path = Path(self.tmp.name) / "cl_request_log.json"
        self.addCleanup(self.tmp.cleanup)

    def test_per_minute_spacing_is_enforced(self) -> None:
        clock = FakeClock()
        limits = TierLimits(per_minute=2, per_hour=100, per_day=1000)
        client, session = make_client(clock, self.ledger_path, limits)

        for _ in range(4):
            client.get_json("https://example.test/api")

        # With 2/minute, requests 3 and 4 must each land in a later minute
        # window than the pair before them.
        self.assertEqual(len(session.sent_at), 4)
        self.assertLess(session.sent_at[1] - session.sent_at[0], 60)
        self.assertGreaterEqual(session.sent_at[2] - session.sent_at[0], 59.0)
        self.assertGreaterEqual(session.sent_at[3] - session.sent_at[1], 59.0)

    def test_budget_exhaustion_raises_instead_of_waiting(self) -> None:
        clock = FakeClock()
        limits = TierLimits(per_minute=100, per_hour=2, per_day=1000)
        client, _ = make_client(clock, self.ledger_path, limits, budget=120.0)

        client.get_json("https://example.test/api")
        client.get_json("https://example.test/api")
        # Third request needs to wait ~an hour for the rolling window, which
        # exceeds the 120s budget: the client must defer, not stall the job.
        with self.assertRaises(RateLimitExceeded):
            client.get_json("https://example.test/api")

    def test_ledger_persists_across_client_instances(self) -> None:
        clock = FakeClock()
        limits = TierLimits(per_minute=100, per_hour=100, per_day=2)
        client_a, _ = make_client(clock, self.ledger_path, limits, budget=50.0)
        client_a.get_json("https://example.test/api")
        client_a.get_json("https://example.test/api")

        # A brand-new client (fresh process) sharing the ledger file must see
        # the day window as spent and defer rather than fire a third request.
        client_b, session_b = make_client(clock, self.ledger_path, limits, budget=50.0)
        with self.assertRaises(RateLimitExceeded):
            client_b.get_json("https://example.test/api")
        self.assertEqual(session_b.sent_at, [])

    def test_retry_after_is_honored_within_budget(self) -> None:
        clock = FakeClock()
        limits = TierLimits(per_minute=100, per_hour=100, per_day=100)
        responses = [
            FakeResponse(status_code=429, headers={"Retry-After": "45"}),
            FakeResponse(status_code=200, payload={"ok": True}),
        ]
        client, session = make_client(clock, self.ledger_path, limits, responses=responses, budget=300.0)

        data = client.get_json("https://example.test/api")

        self.assertEqual(data, {"ok": True})
        self.assertEqual(len(session.sent_at), 2)
        self.assertGreaterEqual(session.sent_at[1] - session.sent_at[0], 45.0)

    def test_retry_after_beyond_budget_defers(self) -> None:
        clock = FakeClock()
        limits = TierLimits(per_minute=100, per_hour=100, per_day=100)
        responses = [FakeResponse(status_code=429, headers={"Retry-After": "900"})]
        client, _ = make_client(clock, self.ledger_path, limits, responses=responses, budget=60.0)

        with self.assertRaises(RateLimitExceeded):
            client.get_json("https://example.test/api")

    def test_ledger_prunes_entries_older_than_a_day(self) -> None:
        clock = FakeClock()
        ledger = RequestLedger(self.ledger_path)
        ledger.record(clock.time())
        clock.sleep(90_000)  # > 24h
        ledger.record(clock.time())
        ledger.prune(clock.time())
        self.assertEqual(len(ledger._load()), 1)  # old entry aged out
        # With the stale entry pruned, only the minute window binds shortly
        # after the surviving request; 61s later even that is clear.
        self.assertEqual(ledger.wait_needed(TierLimits(1, 10, 10), clock.time() + 61), 0.0)


if __name__ == "__main__":
    unittest.main()
