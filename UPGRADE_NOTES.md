# Rate-limit refactor: tier-aware, budget-aware CourtListener client

## The problem this fixes

CourtListener replaced its old 5,000 req/hour default with rolling-window
throttles on 2026-05-07 (default authenticated tier: **5/min, 50/hour,
125/day**, most restrictive window controls). The pipeline was built for the
old world: a fixed 4-second pause (~13 req/min) that violates even a doubled
per-minute limit, a hard 30-second Retry-After abort that kills runs at the
hourly wall, and a 5-day cadence that wastes the daily quota on four out of
every five days. Observable symptoms in `data/last_run.json`:
`courtlistener_rate_limited: true`, `docket_update_complete: false`, and
per-case checkpoints spread across a full month.

## What changed

`scripts/cl_client.py` (new) now owns all CourtListener HTTP. It reads the
account's tier from env vars, keeps a persisted rolling ledger of request
timestamps in `data/cl_request_log.json` (shared across processes and runs,
committed by the existing `git add data/` step), paces every request so the
client almost never sees a 429, and compares any required wait against a
per-process time budget. When a wait won't fit, it raises the same
`RateLimitExceeded` the pipeline already handles: publish progress, keep
checkpoints, defer to the next run.

`update_dockets.py` and `discover_cases.py` lose their duplicated retry
layers (~150 lines of copy-paste deleted) and take the client instead of a
raw session. The workflow moves from every-5-days to **daily**: under rolling
windows, frequent small budget-aware runs move roughly 8–12x more data than
rare big runs that slam into the hourly wall, and the checkpoint architecture
already makes every run's progress permanent.

`update_dockets.py` also gains an **opt-in** batched change-detection
pre-pass (`CL_BATCHED_CHANGE_DETECTION=1`): one RECAP-search query per ~20
dockets identifies which tracked cases actually have new entries, so quiet
dockets advance their checkpoints without a per-docket request. On a typical
quiet day that turns ~41 polling requests into ~3, leaving most of the daily
quota for backfills and discovery.

```
.github/workflows/scheduled_update.yml |  19 +++-
scripts/cl_client.py                   | new (~300 lines)
scripts/discover_cases.py              |  83 ++-----
scripts/update_dockets.py              | 197 +++++++++-----
tests/test_checkpoint_persistence.py   |   4 +-
tests/test_cl_client.py                | new
```

All 18 tests pass (12 existing + 6 new deterministic limiter tests using an
injected fake clock). No new dependencies.

## Knobs

| Env var | Default | Notes |
|---|---|---|
| `CL_REQUESTS_PER_MINUTE` | 5 | Doubled promo (through 2026-08-06): 10 |
| `CL_REQUESTS_PER_HOUR` | 50 | Promo: 100. Membership: your tier |
| `CL_REQUESTS_PER_DAY` | 125 | Promo: 250 |
| `CL_SAFETY_MARGIN` | 1 | Requests reserved per window |
| `CL_TIME_BUDGET_SECONDS` | 1500 | Per-process wall-clock cap on waits |
| `CL_BATCHED_CHANGE_DETECTION` | 0 | Opt-in; falls back to per-docket polling on any error |
| `CL_REQUEST_LOG_PATH` | data/cl_request_log.json | Ledger location |

Changing tiers (promo starts, promo ends, membership purchased) is a one-line
edit to the workflow env block. No code changes, ever again.

## Rollout

1. Merge, then trigger one `workflow_dispatch` and read the logs. You should
   see steady pacing, zero or near-zero 429 warnings, and a graceful
   "deferring to the next run" if the hourly window fills.
2. While the promo is live, set the env block to 10 / 100 / 250 and fire a
   few manual dispatches to collapse the month-wide checkpoint backlog.
3. Batched change detection: leave it at 0 until you've smoke-tested once.
   Trigger a dispatch with `CL_BATCHED_CHANGE_DETECTION=1`, and check the log
   line `Change detection: N docket(s) with activity, M quiet docket(s)
   advanced without polling` against reality (spot-check two or three tracked
   cases on CourtListener). The fielded query it uses —
   `docket_id:(... OR ...) AND entry_date_filed:[DATE TO *]` with `type=r` —
   matches CourtListener's documented fielded-search syntax but has not been
   verified against the live API from this environment. It fails safe: any
   error, unexpected response shape, or overflow falls back to polling every
   docket, and chunks that page past the cap are treated as fully active so a
   false negative can't silently skip a docket. Once verified, flip to 1.

## Honest caveats

The ledger only knows about requests made through this client. If you use the
CourtListener MCP connector or manual API calls on the same token, those
consume the server-side windows invisibly; the client's 429 path handles that
as a backstop, but heavy interactive use right before the daily cron will
shrink that run's effective budget. Also note FLP forbids multiple accounts
per person/project — the durable throughput fix is an EDU membership, not a
second token. Finally, `MAX_SUMMARIES_PER_RUN` is an Anthropic spend control
and is untouched: clearing the CourtListener bottleneck means more entries
flowing to the summarizer, so watch Claude API spend for the first week.

## Rollback

Revert the four modified files and delete `scripts/cl_client.py`,
`tests/test_cl_client.py`, and `data/cl_request_log.json`. The data files are
forward-compatible; nothing in `cases.json`/`updates.json` changed shape.
