#!/usr/bin/env python3
"""Shared CourtListener HTTP client with tier-aware, budget-aware rate limiting.

Why this exists
---------------
On 2026-05-07 Free Law Project replaced the old 5,000 requests/hour default
with per-tier rolling-window throttles (default tier: 5/minute, 50/hour,
125/day). The pipeline previously paced requests with a fixed 4-second pause
(~13/minute), which violates the default per-minute limit, burns retries on
429s, and aborts runs at the hourly wall.

This client makes the pipeline a *good citizen* of whatever tier the account
is on:

- Sustained tier limits come from optional repository-backed env vars; unset
  values use conservative authenticated-tier defaults.
- A persisted request ledger (``data/cl_request_log.json``) tracks our own
  rolling windows across runs and processes, so the client paces itself and
  almost never sees a 429 in the first place.
- Instead of a fixed 30s Retry-After abort, waits are compared against a
  per-process time budget; a run stops gracefully (raising
  ``RateLimitExceeded``, which every caller already handles by publishing
  progress and deferring to the next run) only when waiting would blow that
  budget.

The ledger only knows about requests made through this client. Anything else
on the same API token (e.g. the CourtListener MCP connector, manual curl)
still counts against the server-side windows, so the 429 path remains as a
backstop and its Retry-After is honored when it fits in the budget.

Environment variables
---------------------
COURTLISTENER_API_KEY      required by callers (passed into the client)
CL_REQUESTS_PER_MINUTE     default 5      sustained token limit
CL_REQUESTS_PER_HOUR       default 50     sustained token limit
CL_REQUESTS_PER_DAY        default 125    sustained token limit
CL_SAFETY_MARGIN           default 1      requests reserved per window
CL_TIME_BUDGET_SECONDS     default 1500   per-process wall-clock budget
CL_REQUEST_LOG_PATH        default data/cl_request_log.json
"""

from __future__ import annotations

import json
import os
import random
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import requests


BASE_DIR = Path(__file__).resolve().parents[1]
DEFAULT_LEDGER_PATH = BASE_DIR / "data" / "cl_request_log.json"

MAX_RETRIES = 3
TIMEOUT = 30
SERVER_ERROR_BASE_BACKOFF_SECONDS = 10

MINUTE = 60.0
HOUR = 3600.0
DAY = 86400.0

# Defaults match CourtListener's post-2026-05-07 authenticated tier.
DEFAULT_RPM = 5
DEFAULT_RPH = 50
DEFAULT_RPD = 125
DEFAULT_SAFETY_MARGIN = 1
DEFAULT_TIME_BUDGET_SECONDS = 1500


class RateLimitExceeded(RuntimeError):
    """Raised when polling must stop and defer remaining work to the next run.

    Kept name-compatible with the exception the pipeline scripts previously
    defined locally: every existing ``except RateLimitExceeded`` site (publish
    progress, don't advance checkpoints, set ``courtlistener_rate_limited``)
    keeps working unchanged.
    """


def _positive_int_env(name: str, default: int) -> int:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        print(f"Warning: invalid {name}={raw!r}; using {default}.")
        return default
    if value < 1:
        print(f"Warning: {name} must be positive; using {default}.")
        return default
    return value


@dataclass(frozen=True)
class TierLimits:
    """Effective per-window request caps (safety margin already applied)."""

    per_minute: int
    per_hour: int
    per_day: int

    @classmethod
    def from_env(cls) -> "TierLimits":
        margin = max(0, _positive_int_env("CL_SAFETY_MARGIN", DEFAULT_SAFETY_MARGIN))
        rpm = _positive_int_env("CL_REQUESTS_PER_MINUTE", DEFAULT_RPM)
        rph = _positive_int_env("CL_REQUESTS_PER_HOUR", DEFAULT_RPH)
        rpd = _positive_int_env("CL_REQUESTS_PER_DAY", DEFAULT_RPD)
        return cls(
            per_minute=max(1, rpm - margin),
            per_hour=max(1, rph - margin),
            per_day=max(1, rpd - margin),
        )

    def windows(self) -> tuple[tuple[float, int], ...]:
        return ((MINUTE, self.per_minute), (HOUR, self.per_hour), (DAY, self.per_day))


class RequestLedger:
    """Rolling log of request timestamps, persisted across runs and processes.

    CourtListener throttles on rolling windows, and the daily window outlives
    any single GitHub Actions job. Persisting our own send times in the data
    directory (committed by the existing ``git add data/`` step) lets every
    process — docket passes, discovery, manual dispatches on the same day —
    share one budget instead of each run assuming a fresh quota.

    Loading and writing are lazy/atomic; nothing touches disk until the first
    request is actually recorded, which keeps unit tests and dry runs clean.
    """

    def __init__(self, path: Path | None = None) -> None:
        env_path = (os.environ.get("CL_REQUEST_LOG_PATH") or "").strip()
        self.path = path or (Path(env_path) if env_path else DEFAULT_LEDGER_PATH)
        self._timestamps: list[float] | None = None

    def _load(self) -> list[float]:
        if self._timestamps is not None:
            return self._timestamps
        timestamps: list[float] = []
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                data = json.load(handle)
            raw = data.get("requests", []) if isinstance(data, dict) else []
            timestamps = [float(item) for item in raw if isinstance(item, (int, float))]
        except (FileNotFoundError, json.JSONDecodeError, ValueError, OSError):
            timestamps = []
        self._timestamps = sorted(timestamps)
        return self._timestamps

    def _write(self) -> None:
        if self._timestamps is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        payload = {
            "note": "Client-side CourtListener request log; prunes to 24h.",
            "updated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "requests": [round(ts, 3) for ts in self._timestamps],
        }
        with tmp.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        tmp.replace(self.path)

    def prune(self, now: float) -> None:
        timestamps = self._load()
        cutoff = now - DAY - MINUTE  # small slack for clock skew
        self._timestamps = [ts for ts in timestamps if ts > cutoff]

    def wait_needed(self, limits: TierLimits, now: float) -> float:
        """Seconds until a request slot is free across all rolling windows."""
        self.prune(now)
        timestamps = self._load()
        wait = 0.0
        for window, cap in limits.windows():
            in_window = [ts for ts in timestamps if ts > now - window]
            if len(in_window) >= cap:
                # Slot frees when the oldest in-window request ages out.
                oldest = in_window[len(in_window) - cap]
                wait = max(wait, (oldest + window) - now)
        return max(0.0, wait)

    def record(self, now: float) -> None:
        self._load()
        assert self._timestamps is not None
        self._timestamps.append(now)
        self.prune(now)
        self._write()


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


class CourtListenerClient:
    """requests.Session wrapper that self-paces to the account's tier.

    ``clock`` and ``sleeper`` are injectable for deterministic tests.
    """

    def __init__(
        self,
        api_key: str,
        *,
        limits: TierLimits | None = None,
        ledger: RequestLedger | None = None,
        time_budget_seconds: float | None = None,
        session: requests.Session | None = None,
        clock: Callable[[], float] = time.time,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.api_key = api_key
        self.limits = limits or TierLimits.from_env()
        self.ledger = ledger or RequestLedger()
        self.session = session or requests.Session()
        self.clock = clock
        self.sleeper = sleeper
        if time_budget_seconds is None:
            time_budget_seconds = float(
                _positive_int_env("CL_TIME_BUDGET_SECONDS", DEFAULT_TIME_BUDGET_SECONDS)
            )
        self.deadline = self.clock() + time_budget_seconds

    # -- internal helpers -------------------------------------------------

    def _remaining_budget(self) -> float:
        return self.deadline - self.clock()

    def _sleep_within_budget(self, seconds: float, reason: str) -> None:
        """Sleep, or raise RateLimitExceeded if the wait would blow the budget."""
        if seconds <= 0:
            return
        if seconds > self._remaining_budget():
            raise RateLimitExceeded(
                f"{reason} needs {int(seconds)}s but only {int(max(0, self._remaining_budget()))}s "
                "remain in this run's CourtListener budget; deferring to the next run"
            )
        self.sleeper(seconds)

    def _acquire_slot(self) -> None:
        wait = self.ledger.wait_needed(self.limits, self.clock())
        if wait > 0:
            self._sleep_within_budget(wait + random.uniform(0, 0.5), "Rolling-window pacing")
        else:
            # Tiny jitter keeps bursts of consecutive requests polite.
            self.sleeper(random.uniform(0.1, 0.4))
        self.ledger.record(self.clock())

    # -- public API --------------------------------------------------------

    def get_json(self, url: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """GET a CourtListener API URL, respecting tier limits and the run budget.

        Raises RateLimitExceeded when the run should stop and defer (budget
        exhausted, persistent 429s, or repeated transport failures), matching
        the semantics the pipeline already handles.
        """
        headers = {"Authorization": f"Token {self.api_key}"}
        for attempt in range(MAX_RETRIES + 1):
            self._acquire_slot()
            try:
                response = self.session.get(url, headers=headers, params=params, timeout=TIMEOUT)
            except requests.RequestException as exc:
                if attempt < MAX_RETRIES:
                    self._sleep_within_budget(
                        min(SERVER_ERROR_BASE_BACKOFF_SECONDS * (2**attempt), 60) + random.uniform(0, 0.5),
                        "Transport-error backoff",
                    )
                    continue
                raise RateLimitExceeded(f"CourtListener request failed for {url}: {exc}") from exc

            if response.status_code == 429:
                # Server-side throttle: something outside our ledger (MCP use,
                # manual calls, clock skew) consumed the window. Honor the
                # server's ask if it fits in the budget; otherwise defer.
                retry_after = retry_after_seconds(response)
                delay = retry_after if retry_after is not None else min(
                    SERVER_ERROR_BASE_BACKOFF_SECONDS * (2**attempt), 60
                )
                if attempt < MAX_RETRIES:
                    self._sleep_within_budget(delay + random.uniform(0, 0.5), "Server Retry-After")
                    continue
                raise RateLimitExceeded(f"CourtListener rate limit persisted for {url}")

            if response.status_code >= 500:
                if attempt < MAX_RETRIES:
                    self._sleep_within_budget(
                        min(SERVER_ERROR_BASE_BACKOFF_SECONDS * (2**attempt), 60) + random.uniform(0, 0.5),
                        "Server-error backoff",
                    )
                    continue

            response.raise_for_status()
            return response.json()
        raise RuntimeError(f"Request failed after retries: {url}")
