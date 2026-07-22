# Pipeline recovery: resumable discovery and honest freshness

## Why this change was needed

The tracker could keep running successfully without advancing discovery. Docket polling consumed most of CourtListener's effective hourly allowance first, and discovery restarted from the first fixed-order query on every run. The same early queries received the few remaining requests while later queries were indefinitely starved. Because quota deferral was treated as a valid partial result, the workflow stayed green even after the completed discovery checkpoint became stale.

The public page compounded the confusion by labeling the newest `case.last_updated` processing timestamp as `Updated`. That timestamp was neither the latest legal activity nor the oldest docket-poll checkpoint, so quiet but fully checked dockets looked indistinguishable from a pipeline that had stopped.

This upgrade separates those concepts and makes every unit of discovery work resumable.

## What changed

### Separate quota windows

`.github/workflows/scheduled_update.yml` now has two daily schedules:

- docket polling at `13:17 UTC`;
- case discovery at `18:47 UTC`.

Manual dispatch requires the `dockets` or `discovery` phase. The runs share the persisted `data/cl_request_log.json` ledger but are nominally five and a half hours apart, so they normally avoid consuming the default effective 49-request hourly window together. The shared ledger protects delayed overlaps, and workflow concurrency queues overlapping runs instead of letting them write canonical data concurrently.

The workflow reads optional repository variables for `CL_REQUESTS_PER_MINUTE`, `CL_REQUESTS_PER_HOUR`, and `CL_REQUESTS_PER_DAY`. Unset values use the conservative sustained defaults in `scripts/cl_client.py`; temporary promotional limits are not hard-coded. `CL_BATCHED_CHANGE_DETECTION` remains disabled because an indexed-search false negative could incorrectly advance a quiet docket checkpoint.

### Exhaustive, resumable discovery

Discovery anchors each sweep to `window_start` and `window_through` and applies both filing-date bounds to CourtListener searches. It now follows every validated CourtListener `next` URL rather than reading only the first 20-result page.

`last_run.discovery_cursor` persists:

- the query-set hash, phase, and next query index;
- `query_page_url` for the current CourtListener query page;
- the RSS docket snapshot, index, and `rss_page_url`;
- `pending_candidates`, a list of normalized candidates awaiting a final classifier decision.

Pending candidates consume the classification budget before newly fetched candidates and appear first in classification order. A candidate with an unresolved classifier attempt stays in the cursor until it is either added to `cases.json` or deliberately rejected; transport errors and malformed classifier output no longer discard it or force a sweep back to query zero. Candidate caps and rate limits preserve the exact query/RSS page position, so later queries eventually receive coverage.

The completed checkpoint advances only after every query page, pending candidate, and RSS lookup page in the anchored sweep is finished. It advances to the saved `window_through`, not the later wall-clock date, so filings that arrived during a multi-run catch-up remain covered by the next sweep.

### Stable candidate identity

Candidate identity now prefers CourtListener's docket primary key. For example, CourtListener docket ID `12345678` is stored and compared as:

```text
id:12345678
```

Only a candidate without a CourtListener ID falls back to:

```text
court:<court>|docket:<normalized docket number>
```

Known cases, durable pending candidates, and the capped rejection cache use the same identity rule. This avoids collapsing similar docket-number formats from different courts.

### Honest status and health

The public tracker now presents two dates:

- `Latest Activity`: the newest qualifying case activity;
- `Dockets Checked Through`: the oldest `docket_last_checked` value among pollable cases.

A quiet day can leave `Latest Activity` unchanged while `Dockets Checked Through` advances, which is expected and now visible.

The site later intentionally removed the public `Dockets Checked Through` statistic in `undergradtechlaw@ed3d381`. Per-case checkpoints remain canonical pipeline state for resumable polling; the renderer contract now follows the current public UI while continuing to require `Latest Activity`.

`scripts/validate_tracker_data.py` has a warning-mode prepublication check and a strict postpublication check. Valid partial progress, including the discovery cursor, is committed and copied to the site before strict enforcement. The final step fails when either the last completed discovery checkpoint or the age of an incomplete discovery cycle exceeds `MAX_DISCOVERY_STALENESS_DAYS` (default `2`). A red run therefore reports stale coverage without throwing away the state needed to recover.

Green means discovery coverage is within the configured grace window. It does not mean that a new qualifying case or docket entry was found.

## State compatibility

The public `cases.json` and `updates.json` schemas remain compatible. New recovery fields live in `data/last_run.json`, and older code ignores unknown cursor fields. Rejected identities may now look like `id:12345678` rather than a bare docket number.

The request ledger remains the authoritative record of requests made by this pipeline during the rolling 24-hour window. Requests made elsewhere with the same CourtListener token are not in the ledger and can still cause a server-side 429; the client preserves cursor progress and defers safely when that happens.

## Rollout

1. Merge the `undergradtechlaw` status-label change first. It is backward-compatible with the existing JSON and lets the pipeline's renderer contract pass.
2. Merge the pipeline change.
3. Manually dispatch `dockets`, then dispatch `discovery` after the hourly window has cleared.
4. Confirm that `docket_last_run_date` advances independently and that `discovery_cursor` shrinks or moves forward on partial discovery runs.
5. Expect the strict health step to remain red while the old discovery checkpoint is outside the two-day grace window. It should return green only after catch-up brings completed coverage inside that window.
6. Keep batched change detection disabled until an observe-only comparison against full polling demonstrates no false negatives over multiple days.

## Rollback

Revert the pipeline change as one unit. Do not delete `data/last_run.json` or `data/cl_request_log.json`: retaining the cursor and request ledger is safer, and the prior code tolerates the extra state fields. The public case and update data do not require a migration or rollback.

The site label change is backward-compatible and may remain deployed. If both repositories must be rolled back, revert the pipeline renderer contract first and the site UI second so a scheduled run never validates an older site template against a newer contract.

Do not delete `scripts/cl_client.py` or restore the old fixed-delay request loop; the shared rolling-window limiter predates this recovery change and remains required. Restoring a combined docket-plus-discovery run would also restore the quota-starvation failure mode, so it should be used only as a short-lived emergency measure.
