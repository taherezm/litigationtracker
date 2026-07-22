# Litigation Tracker For Undergraduate Technology Law

[![Production Pipeline](https://github.com/taherezm/litigationtracker/actions/workflows/scheduled_update.yml/badge.svg?branch=main)](https://github.com/taherezm/litigationtracker/actions/workflows/scheduled_update.yml?query=branch%3Amain)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.11](https://img.shields.io/badge/Python-3.11-blue.svg)](https://www.python.org/)

Automated litigation data pipeline for Undergraduate Technology Law at IU. The system discovers federal cases involving artificial intelligence and intellectual property, monitors docket activity through CourtListener, summarizes new entries with legal-precision guardrails, and publishes normalized JSON data to the static website served from `taherezm/undergradtechlaw`.

Live tracker: [undergradtechlaw.org/tools/litigation-tracker](https://www.undergradtechlaw.org/tools/litigation-tracker/).

## Glance

- Source repository: `taherezm/litigationtracker`
- Public site repository: `taherezm/undergradtechlaw`
- Public data path: `tools/litigation-tracker/cases.json` and `tools/litigation-tracker/updates.json`
- Schedule: docket polling at `13:17 UTC`; discovery at `18:47 UTC` every day
- Pipeline: run one quota-isolated phase, validate JSON, publish progress, then enforce discovery freshness
- Cost controls: discovery classifies at most 5 candidates per pass and automatically runs up to 20 passes or 45 minutes per job; docket summaries are capped at 100 new entries per pass, with up to 2 passes per job
- Publication rule: unsummarized or placeholder docket activity is not allowed into public JSON

## Current Public Version

The public tracker is the static UI in `taherezm/undergradtechlaw` at `tools/litigation-tracker/`. This repository owns the canonical data and automation; each workflow run that reaches publication copies `data/cases.json` and `data/updates.json` into the site repo. Publication precedes the strict discovery-freshness check, so a run can publish valid progress and still end red when an incomplete discovery sweep has aged beyond its grace window. The browser page renders case counts, court counts, significant-ruling totals, `Latest Activity` from the newest qualifying case activity, and `Dockets Checked Through` from the oldest checkpoint among pollable cases. A quiet docket day therefore no longer looks like a stalled pipeline.

Daily docket polling and discovery run in separate rolling-hour quota windows. Per-case checkpoints, a discovery cursor that preserves query/page progress, durable pending classifier candidates, and a persisted request ledger let interrupted or budget-limited work continue without restarting a sweep. A successful run does not guarantee a visible activity change: new cards or entries appear only when qualifying cases or docket activity are found.

### Verified Production Snapshot — July 22, 2026

- The bounded discovery runner shipped in [PR #5](https://github.com/taherezm/litigationtracker/pull/5). Its [production verification run](https://github.com/taherezm/litigationtracker/actions/runs/29892410696) completed four automatic passes, reached the current UTC date, published both repositories, and passed the strict discovery-freshness gate.
- Canonical and live data contain 44 tracked cases and 764 activity records. Discovery is complete through July 22 across all 36 queries; the latest qualifying docket activity is dated July 20.
- Forty-three existing dockets were checked through July 21. The case discovered on July 22 is eligible for its first poll in the next regular docket phase, scheduled for `13:17 UTC`; that normal handoff is distinct from discovery freshness.
- The live `cases.json` and `updates.json` matched the canonical repository byte-for-byte at verification, and the public tracker returned HTTP 200 with the current `Latest Activity` and `Dockets Checked Through` renderer.

This is a dated deployment snapshot; the live tracker and canonical JSON remain the source of truth after later scheduled runs. The badge above follows the latest completed workflow run on `main`, regardless of whether GitHub triggered it by cron or an operator triggered the same production workflow for recovery. Every trigger runs the same tests, publication checks, and strict discovery-freshness gate. Filtering the badge to `event=schedule` can leave it displaying an obsolete pre-fix result even after a newer green production run.

## What This Repository Owns

This repository is the data and automation layer. It does not render the public UI directly. Instead, it maintains the canonical tracker state in:

- `data/cases.json`: the case index consumed by the website.
- `data/updates.json`: recent docket activity keyed back to cases.
- `data/last_run.json`: scheduler and rate-limit state, resumable discovery cursor, durable pending candidates, counters, and rejected identity cache.
- `data/cl_request_log.json`: CourtListener request timestamps retained for roughly 24 hours so later passes and jobs share the same rolling-window budget.

The static site repository (`taherezm/undergradtechlaw`) owns the browser UI. The GitHub Actions workflow in this repository checks that repository out into a local `iptl-iu-site/` directory, pushes updated `cases.json` and `updates.json` into `tools/litigation-tracker/`, and lets GitHub Pages serve them as static assets. The public tracker page fetches those JSON files client-side.

## Code Guide

| Path | Responsibility |
|---|---|
| `.github/workflows/scheduled_update.yml` | Quota-isolated daily orchestration, validation, health enforcement, and publication to both repositories. |
| `scripts/cl_client.py` | Shared CourtListener HTTP client, rolling-window pacing, persisted request ledger, retries, and run-budget deferral. |
| `scripts/run_docket_update_passes.py` | Bounded update/summarize loop; aggregates per-pass results and stops on completion or client deferral. |
| `scripts/update_dockets.py` | Docket polling, entry deduplication, per-case checkpoints, status inference, and optional batched change detection. |
| `scripts/summarize.py` | Model-backed entry summaries, significance, posture/status updates, key rulings, and case-summary refresh. |
| `scripts/discover_cases.py` | Paginated CourtListener/RSS search, resumable query/page state, durable classification work, rejection cache, and case creation. |
| `scripts/run_discovery_passes.py` | Bounded automatic discovery drain; repeats resumable candidate-cap passes and stops on completion, quota/provider deferral, no progress, or job limits. |
| `scripts/case_intelligence.py` | Deterministic case-card intelligence and public plain-language summaries. |
| `scripts/regenerate_case_summaries.py` | Maintenance-only regeneration of case intelligence from existing data; not part of the scheduled job. |
| `scripts/validate_tracker_data.py` | Publication guard for case/update shape, summaries, uniqueness, and pipeline warning state. |
| `scripts/validate_site_renderer.py` | Contract check between canonical JSON fields and the public tracker renderer. |
| `tests/` | Deterministic coverage for case intelligence, checkpoint persistence, discovery cursors, workflow contracts, health checks, and the CourtListener limiter. |

## Pipeline Overview

The tracker uses three ETL stages across two daily workflow runs:

1. `scripts/update_dockets.py`
   Polls CourtListener docket entries for every non-resolved tracked case from each case's own `docket_last_checked` checkpoint, appends new entries, and prepends recent activity records into `data/updates.json`. Run via `scripts/run_docket_update_passes.py`, which alternates docket and summarization passes.
2. `scripts/summarize.py`
   Summarizes unsummarized docket entries, classifies their litigation significance, updates procedural posture, records key rulings, marks resolved cases when appropriate, and regenerates structured case-level intelligence and public case summaries from the latest docket state.
3. `scripts/discover_cases.py`
   Runs in a later quota window, searches every result page for new candidate cases, classifies AI/IP relevance, normalizes accepted candidates into case records, initializes `case_intelligence`, and writes them into `data/cases.json`. A persisted query/page/RSS cursor and durable pending candidates resume incomplete work; same-day reruns skip discovery only when no active cursor remains unless `FORCE_DISCOVERY` is set.
   The scheduled phase invokes it through `scripts/run_discovery_passes.py`, which automatically drains resumable five-candidate batches within explicit pass and wall-clock limits.

All CourtListener traffic from docket updates and discovery goes through `scripts/cl_client.py`. Each stage reads and writes JSON directly. Writes are atomic: data is serialized to a temporary sibling file and then moved into place with `Path.replace()`.

## Discovery

Discovery combines deterministic search heuristics with model-assisted legal relevance classification.

### Candidate Sources

`discover_cases.py` collects candidates from two sources:

- CourtListener search API (`/api/rest/v4/search/`) with `type=d`, sorted by search score.
- Courthouse News RSS (`https://www.courthousenews.com/feed/`) as a secondary signal when the feed text contains litigation terms and a docket number pattern.

The CourtListener query set is intentionally broad. It covers generative AI, training data, LLMs, right of publicity, image generation, software patent disputes, scraping, open-source licensing, biometric/privacy terms, blockchain/NFT issues, trade secret language, and other technology/IP indicators.

Each `discover_cases.py` pass stops after `DEFAULT_MAX_DISCOVERY_CANDIDATES = 5` candidates by default. The cap bounds one resumable batch and can be overridden with:

```bash
export MAX_DISCOVERY_CANDIDATES=20
```

The default per-pass cap bounds each child process and gives it a frequent durable checkpoint. Hitting it sets `discovery_candidate_cap_reached: true`, keeps the completed-discovery checkpoint fixed, and preserves the current query/page position. Successfully decided candidates become known or rejected; the automatic runner refetches that page, deduplicates those decisions, and drains the remaining hits before advancing. When an anchored sweep completes behind the current UTC date, the same job starts the next sweep instead of mistaking old coverage for current coverage. One scheduled job runs at most 20 passes or 2,700 seconds and then publishes the exact cursor for the next daily run. It stops sooner when coverage reaches today, on CourtListener deferral, classifier failure, any non-cap interruption, or no durable progress. If a child exceeds the wall-clock budget, its partial case/cursor files are rolled back while the request ledger is preserved. Only unresolved classifier attempts are copied into `pending_candidates`.

Every CourtListener query is paginated until its `next` URL is empty. The cursor records both the query index and the validated CourtListener `query_page_url`, so a rate limit resumes at the exact failed page instead of replaying the beginning of the query set. RSS search pagination is tracked separately in `rss_page_url`. Candidates whose classifier attempt fails are stored in `pending_candidates`; the next run retries the candidate payload itself rather than depending on search rankings or an expiring RSS item to surface it again.

### Search Window

Discovery uses `data/last_run.json` to decide the filing-date window:

- If fewer than five cases are already tracked, or no completed discovery checkpoint exists, it searches the last 90 days.
- Otherwise, it searches from `discovery_last_run_date`.

At the start of a sweep, discovery records both the prior checkpoint (`window_start`) and that day's upper coverage marker (`window_through`). CourtListener searches use those dates as lower and upper filing bounds while walking every page. If the sweep takes multiple runs or days, successful completion advances `discovery_last_run_date` to the anchored `window_through`, not the later wall-clock date. The next sweep therefore covers filings that arrived while catch-up was in progress without letting a changing result set destabilize saved page URLs.

### Deduplication and Rejection Cache

Candidate deduplication prefers CourtListener's stable docket primary key. A docket with CourtListener ID `12345678` has identity `id:12345678`; only records without an ID fall back to `court:<court>|docket:<normalized docket number>`. This prevents identical docket-number formats in different courts from collapsing together. Existing cases, pending candidates, and rejected candidates all use the same identity function.

Rejected dockets are kept because broad keyword searches repeatedly surface false positives. The cache is capped to the most recent 500 rejected docket keys:

```json
{
  "rejected_dockets": ["id:12345678"]
}
```

### Relevance Classification

The pipeline first attempts deterministic classification using term matching:

- AI terms: `artificial intelligence`, `generative`, `OpenAI`, `Anthropic`, `ChatGPT`, `LLM`, `machine learning`, `training data`, `stable diffusion`, `neural network`, etc.
- IP claim terms: copyright, patent, trade secret, DTSA, right of publicity, voice cloning, deepfake, DMCA 1202, trademark, and related statutory markers.

If the deterministic classifier cannot confidently mark a candidate relevant, the configured model provider is used as a legal classifier. The classifier is prompted to decide whether the case is primarily or substantially about intellectual property claims arising from or directly involving AI systems, AI-generated content, or AI training data. It must return strict JSON:

```json
{
  "relevant": true,
  "confidence": "high",
  "reason": "The dispute alleges copyright infringement based on model training data.",
  "claims": ["copyright infringement"]
}
```

Low-confidence or irrelevant results are rejected unless the deterministic fallback can independently identify both an AI signal and an IP claim signal. A transport error, malformed model response, or other transient classifier failure is not a rejection: the normalized candidate remains in `pending_candidates` for the next discovery run.

### Case Record Construction

Accepted candidates are normalized into public case records. Each record gets:

- stable slug ID derived from case name or docket number;
- caption, court, docket number, CourtListener docket ID, and CourtListener URL;
- filing date, parties, judges where available, claims, and status;
- default procedural posture (`Filed`);
- empty `key_rulings` and `docket_entries` arrays;
- structured `case_intelligence`;
- a public plain-language case summary.

Case-level summaries are deterministic and are generated from `case_intelligence`, not from a generic AI/IP template. When parsed materials identify only the caption and claim type, the summary uses a transparent fallback such as: "The complaint has been docketed, but the available parsed materials do not yet identify the specific AI system, works, data, or training/output theory at issue." Legacy non-boilerplate summary sentences may be used as source material during backfill, but banned boilerplate is never republished.

## Docket Monitoring

`update_dockets.py` monitors every non-resolved case with a CourtListener docket ID, including stayed cases that may later return to active status:

```json
{
  "status": "active or stayed",
  "courtlistener_docket_id": "..."
}
```

With production batching disabled, each eligible case is polled through the shared CourtListener client:

```text
GET /api/rest/v4/docket-entries/
  docket=<courtlistener_docket_id>
  date_filed__gte=<case docket_last_checked - 2 days, or fallback>
  order_by=-entry_number
  page_size=50
```

Each case carries its own `docket_last_checked` checkpoint. When a case is fully checked in a run, its checkpoint advances to that day, even if the run is later rate-limited or capped while checking other cases. This makes progress permanent: a throttled run never causes the next run to refetch windows that already completed. The two-day overlap re-covers entries that are docketed late, and entry-number deduplication makes the overlap harmless.

Cases are processed stalest-checkpoint-first so repeated rate-limited runs rotate coverage across every open docket instead of starving the cases checked last. Cases without a valid checkpoint are seeded from the global `docket_last_run_date` before polling, so interrupted runs still persist a conservative per-case coverage floor. Newly discovered cases with no stored docket entries are polled from their filing date so the first docket update can backfill the initial case history.

Each docket update pass honors `MAX_SUMMARIES_PER_RUN`, which defaults to `100`. The scheduled job runs up to two passes, so a backlogged job can normally fetch and summarize up to 200 entries before deferring the remainder. The per-pass cap is enforced while paginating, so one backlogged docket cannot consume the whole job's API and summary budget, and it is applied before new entries are committed into the public data files so every newly accepted docket entry can be summarized in the same pass. If the cap interrupts a case, `docket_update_complete` is set to `false`, that case's checkpoint is not advanced, and overflow is retried on the next pass or daily run.

### CourtListener Request Budget

`scripts/cl_client.py` reads the account's sustained tier from `CL_REQUESTS_PER_MINUTE`, `CL_REQUESTS_PER_HOUR`, and `CL_REQUESTS_PER_DAY`. The workflow supplies optional repository variables; unset values fall back to `5 / 50 / 125`. Configure only limits shown on the token's signed-in [CourtListener API usage page](https://www.courtlistener.com/profile/api-usage/), not a temporary promotion. A safety margin of `1` makes the default effective caps `4 / 49 / 124`.

Before each request, the client consults `data/cl_request_log.json`, waits until a slot is available, records the send time, writes the refreshed ledger through temporary-file replacement, and prunes timestamps older than roughly 24 hours. The ledger is committed with `data/`, so sequential docket passes, discovery, and later workflow jobs share one view of quota already consumed by this pipeline.

Rolling-window waits, server `Retry-After` values, transport retries, and 5xx backoff must fit within `CL_TIME_BUDGET_SECONDS`. If a wait would exceed the remaining process budget, the client raises `RateLimitExceeded`; callers publish completed progress, preserve unfinished checkpoints, and defer the rest to a later daily run instead of sleeping indefinitely. The two scheduled phases are nominally separated by five and a half hours, so docket polls and paginated discovery queries normally avoid the same effective hourly window; the shared ledger still protects delayed or queued overlaps.

`CL_BATCHED_CHANGE_DETECTION` remains disabled in production. RECAP search indexing can lag or omit documentless activity, and a valid empty response would currently advance quiet checkpoints. Do not enable it until an observe-only canary compares it against full docket polling over multiple days and proves that it has no false negatives.

### Entry Deduplication

New entries are deduplicated by normalized `entry_number`. If CourtListener returns an entry number already present in the case's `docket_entries`, it is skipped.

Accepted entries are appended to the case during the run:

```json
{
  "entry_number": "42",
  "date": "2026-06-01",
  "raw_text": "ORDER granting motion...",
  "summary": null,
  "significance": null
}
```

At the same time, a recent-activity record is prepended to `data/updates.json`:

```json
{
  "case_id": "tesseract-systems-llc-v-soundhound-inc",
  "case_name": "Tesseract Systems LLC v. SoundHound, Inc.",
  "entry_date": "2026-06-01",
  "summary": null,
  "significance": null,
  "entry_number": "42",
  "logged_at": "2026-06-03T22:00:00Z"
}
```

Those `summary: null` values are an internal, same-run state only. `scripts/summarize.py` fills them before validation and publication. Validation blocks any unsummarized or placeholder docket activity from reaching the live tracker.

### Status Inference

Docket text is used to infer public case status directly. Terminal docket language such as dismissal, final judgment, mandate issuance, or settlement marks the case `resolved`; stay orders mark it `stayed`; later docket language lifting a stay returns it to `active`. The updater keeps polling every non-resolved case so stayed cases can move back to active automatically when the docket changes.

The summarization stage may also classify an entry as `case_resolved` or return a posture update such as `Stayed`, `Dismissed`, `Settled`, or `Judgment`, and those structured values update the case status before publication.

## Summarization and Legal Precision

`summarize.py` processes docket entries that have raw text but no summary, up to the configured `MAX_SUMMARIES_PER_RUN` budget. It prompts the configured model provider to return strict JSON:

```json
{
  "summary": "The court denied the motion to dismiss.",
  "significance": "significant_ruling",
  "posture_update": "Motion Practice",
  "key_holding": "The complaint plausibly alleged copyright infringement."
}
```

Allowed significance values:

- `minor_update`
- `significant_ruling`
- `case_resolved`

Allowed posture values:

- `Filed`
- `Motion Practice`
- `Discovery`
- `Summary Judgment`
- `Trial`
- `Appeal`
- `Settled`
- `Dismissed`
- `Judgment`

### Guardrails

The summarizer is constrained by legal-precision rules:

- describe docket activity, not ultimate liability, unless the entry itself reports a ruling;
- use actor-specific phrasing (`Plaintiff filed`, `The court ordered`, `The judge denied`);
- avoid saying a party "violated" the law unless a court has held that;
- do not invent claims, holdings, deadlines, settlement posture, or procedural posture;
- preserve uncertainty for notices, filings, scheduling entries, and administrative docket activity.

Generated summaries are post-processed. The code removes repeated sentence blocks, rejects known bad phrases such as "violated copyright infringement rights," and falls back to a clipped raw-text summary when the model returns malformed or legally unsafe output.

When an entry is classified as `significant_ruling`, a deduplicated key-ruling record is added to the case. When classified as `case_resolved`, the case is marked `resolved`. Matching records in `updates.json` are updated with the generated summary and significance.

After docket-entry summarization, `summarize.py` refreshes every case's `case_intelligence` and regenerates `plain_language_summary`. This keeps case cards aligned with the most meaningful docket event rather than leaving them at the initial discovery summary.

## Case-Level Intelligence

`scripts/case_intelligence.py` owns deterministic case-card intelligence. It builds:

- `claim_category`: a normalized category such as `copyright_training_data`, `copyright_news_or_publishing`, `patent_ai_software`, `trade_secret_or_transparency`, `privacy_or_consumer_protection`, or `unknown`.
- `procedural_stage`: a machine-readable stage such as `newly_filed`, `motion_to_dismiss`, `discovery`, `stayed`, `appeal`, `significant_ruling`, `judgment`, or `resolved`.
- `latest_meaningful_event`: the latest substantive event selected from key rulings, docket entries, and updates.
- `case_theory`, `current_posture`, `why_it_matters`, and `latest_change`, which are composed into the public `plain_language_summary`.
- `missing_information` and `confidence_level`, so low-information cases publish transparent limitations rather than generic prose.

Meaningful-event selection prefers complaints, amended complaints, dispositive motions, substantive orders, stays, transfers, consolidation, severance, discovery orders, appeal activity, settlement notices, dismissals, and judgments. Routine items such as cover sheets, summonses, AO-121 notices, judge assignment, pro hac vice motions, appearances, filing-fee records, and hearing acknowledgments are ignored unless no substantive event is available.

Regenerate all case-level intelligence and public case summaries from existing data with:

```bash
python scripts/regenerate_case_summaries.py
python scripts/validate_tracker_data.py
```

## Scheduler State

`data/last_run.json` tracks run state across phases. The shape is:

```json
{
  "cases_discovered": 0,
  "discovery_complete": false,
  "discovery_candidate_cap_reached": false,
  "discovery_phase": "queries",
  "discovery_incomplete_reason": "rate_limit",
  "discovery_incomplete_since": "YYYY-MM-DD",
  "discovery_queries_completed": 7,
  "discovery_queries_total": 36,
  "discovery_passes_run": 1,
  "max_discovery_passes_per_job": 20,
  "discovery_job_stop_reason": "rate_limit",
  "discovery_cursor": {
    "version": 1,
    "window_start": "YYYY-MM-DD",
    "window_through": "YYYY-MM-DD",
    "query_set_sha256": "...",
    "phase": "queries",
    "next_query_index": 7,
    "query_page_url": "",
    "pending_candidates": [],
    "rss_docket_numbers": [],
    "next_rss_index": 0,
    "rss_page_url": ""
  },
  "rejected_dockets": ["id:12345678"],
  "entries_updated": 0,
  "docket_update_complete": false,
  "courtlistener_rate_limited": true,
  "docket_entry_cap_reached": false,
  "summaries_generated": 0,
  "summaries_deferred": 0,
  "max_summaries_per_run": 100,
  "max_docket_update_passes": 2,
  "discovery_last_run_date": "YYYY-MM-DD",
  "docket_last_run_date": "YYYY-MM-DD",
  "last_run_date": "YYYY-MM-DD"
}
```

`discovery_passes_run`, `max_discovery_passes_per_job`, and `discovery_job_stop_reason` describe the outer automatic drain. They distinguish a completed current sweep from an expected bounded stop such as `rate_limit`, `classification`, `no_progress`, `time_budget`, or `pass_limit` without overwriting the source-level cursor reason.

`discovery_last_run_date` and `docket_last_run_date` advance independently. `discover_cases.py` advances its checkpoint only after every page of every query, all durable pending candidates, and every saved RSS lookup/page in the anchored sweep finish. A rate limit preserves the failed query or RSS page URL; a candidate cap preserves the current source cursor; a transient classifier failure leaves that candidate pending without caching it as rejected. Pending candidates consume the classification budget before new source candidates, appear first in classification order, and are removed only after a successful accept/reject decision. `docket_last_run_date` is the oldest valid per-case `docket_last_checked` checkpoint. `last_run_date` remains a legacy all-phases-complete checkpoint.

Each `pending_candidates` item retains the normalized `source`, raw CourtListener object, docket ID and number, caption, court, filing date, parties, and snippet. Identity is recalculated from those normalized fields when the cursor loads; a separately stored identity string is never trusted.

`courtlistener_rate_limited` describes the current run only. The docket runner clears it before docket work, and the standalone discovery job clears it before recording its own result.

`discovery_incomplete_since` starts on the first incomplete run and survives cursor resumes. The workflow publishes valid partial progress first, then runs the strict health check. It allows at most two days of completed-checkpoint age or incomplete-cycle age; after that grace window, the final health step turns the workflow red without discarding the saved cursor.

When `docket_entry_cap_reached` is `true`, the run hit the configured summary budget. Valid summarized entries still publish, and per-case checkpoints for fully checked cases still advance; only the interrupted and unreached cases retry from their prior checkpoints. The scheduled workflow runs multiple bounded docket/summarization passes before publication so a large backlog can clear in one job instead of waiting for the next scheduled run.

## Publication Flow

GitHub Actions runs `.github/workflows/scheduled_update.yml` twice daily: docket polling at `13:17 UTC` and discovery at `18:47 UTC`. Manual dispatch requires choosing the `dockets` or `discovery` phase. The workflow's concurrency queue serializes overlapping runs without replacing pending work. Each case polls from its own checkpoint with a two-day overlap; discovery resumes from its saved cursor and anchored window.

The job runs on Ubuntu with Python 3.11:

1. Check out this pipeline repository.
2. Install Python dependencies from `requirements.txt`.
3. Run the deterministic unit and workflow-contract tests before any live API call.
4. Run only the selected phase: bounded docket/summarization passes or the bounded automatic discovery drain.
5. Validate generated tracker data in warning mode.
6. Commit any changed files in `data/` back to this repository.
7. Check out `taherezm/undergradtechlaw` using `IPTL_SITE_TOKEN`.
8. Validate the site tracker renderer contract, including the two distinct status-date labels.
9. Copy `data/cases.json` and `data/updates.json` into `iptl-iu-site/tools/litigation-tracker/`.
10. Commit and push changed tracker data to the site repository.
11. Enforce the two-day discovery-coverage and incomplete-cycle health window after publication.

Because the site repository is served by GitHub Pages from `main`, the copied JSON files become available to the public tracker after the site repo deploys.

## Operations

Use the scheduled workflow for routine updates. Manual `workflow_dispatch` runs require a phase choice and use live CourtListener and model-provider API calls, so they should be treated as real production runs.

Avoid cancelling in-flight runs and avoid back-to-back manual dispatches. Cancellation before the pipeline-data commit loses that run's fetched progress while still spending CourtListener quota; cancellation after that commit can leave the site copy temporarily behind canonical data until the next queued run reconciles it.

Expected non-fatal warning states:

- CourtListener client deferral, a persistent server rate limit, or repeated request failure can leave `courtlistener_rate_limited: true`; valid fetched data still publishes, completed per-case checkpoints still advance, and the missed window is retried.
- The discovery candidate cap can leave `discovery_candidate_cap_reached: true`; the same scheduled job automatically runs more bounded passes while durable progress continues. If the job reaches its pass or time limit, valid classified cases still publish and the saved cursor resumes on the next daily run. Any transient classifier failures remain in `pending_candidates` and stop same-job retries.
- The per-pass summary cap can leave `docket_entry_cap_reached: true`; valid summarized data still publishes, and the workflow runs additional bounded passes before leaving overflow for the next daily run. CourtListener client deferral stops additional same-job passes so the workflow does not repeatedly call a constrained API.
- CourtListener or classifier failures can leave `discovery_complete: false`; discovery resumes from the saved query/page/RSS cursor, durable pending candidates, and anchored filing window.
- Discovery coverage and incomplete-cycle age are warning-only for two days. Valid partial progress is published first; persistent staleness then fails the final health step so the scheduled-workflow badge cannot remain green indefinitely.

Routine maintenance checklist:

1. Confirm the GitHub Actions workflow is active.
2. Confirm repository secrets exist: `COURTLISTENER_API_KEY`, `ANTHROPIC_API_KEY`, `LEGAL_AI_MODEL`, and `IPTL_SITE_TOKEN`.
3. Keep `MAX_SUMMARIES_PER_RUN` at `100` unless cost, timeout, or backlog data justifies a change.
4. Run `python scripts/validate_tracker_data.py` before committing hand-edited data.
5. Check the site renderer contract before changing the public tracker HTML.

## Validation

Two validation scripts protect the publication path.

### `scripts/validate_tracker_data.py`

This validates the generated JSON before it is published:

- `cases.json` must be a list.
- `updates.json` must be a list.
- case IDs must exist and be unique.
- docket numbers cannot be duplicated within the same court.
- case summaries, docket-entry summaries, update summaries, and key-ruling summaries cannot repeat the same sentence block.
- placeholder summaries such as "no docket entry text" cannot leak into public data.
- every case must have `case_intelligence` and a non-empty `plain_language_summary`.
- case summaries cannot contain old boilerplate such as "The tracker is monitoring" or generic catch-all phrases about "AI systems, model outputs, or training data."
- low-confidence intelligence must explain what information is missing.
- stayed cases must mention the stay in `current_posture` or `plain_language_summary`.
- summary fingerprints cannot collapse into effectively identical non-fallback prose across many cases.
- `--enforce-pipeline-freshness` fails when the last completed discovery checkpoint or an incomplete cycle exceeds `MAX_DISCOVERY_STALENESS_DAYS`; the workflow invokes it only after publishing valid progress.

### `scripts/validate_site_renderer.py`

This validates that the site repository still contains the expected tracker renderer contract before data is copied into it. It checks for required snippets such as:

- `Activity Dates`
- `renderActivityDays(activityDays)`
- `publicEntryText(entry)`
- `Latest Activity`
- `Dockets Checked Through`
- `latestActivityDate(state.cases)`
- `docketCheckedThrough(state.cases)`

It also blocks stale renderer text such as:

- `Case Timeline`
- `Summary pending.`

This prevents the pipeline from publishing data into a site template that no longer matches the expected public tracker behavior.

## Data Model

### Case Object

`data/cases.json` is the canonical case index. Each object contains:

- `id`: stable slug used by the front end.
- `name`: case caption.
- `court` and `court_full`: CourtListener court identifiers/names.
- `docket_number`: federal docket number.
- `courtlistener_docket_id`: CourtListener docket primary key.
- `date_filed`: filing date where available.
- `claims`: normalized claim labels.
- `legal_theories`: currently reserved for richer doctrinal tagging.
- `status`: `active`, `stayed`, or `resolved`.
- `procedural_posture`: normalized litigation stage.
- `parties`: parsed plaintiff/defendant names.
- `judges`: judge names where available.
- `key_rulings`: significant rulings extracted from docket activity.
- `docket_entries`: tracked docket entries.
- `case_intelligence`: structured source for the public case-card summary.
- `plain_language_summary`: public case summary.
- `source`: currently `courtlistener`.
- `courtlistener_url`: source link for provenance.
- `last_updated`: last pipeline update date.
- `discovered_date`: discovery date.

### Docket Entry Object

Each `docket_entries` item stores:

- `entry_number`
- `date`
- `raw_text`
- `summary`
- `significance`

`raw_text` is retained in the pipeline data because summarization and validation need source text. The public renderer controls what is exposed to users.

### Update Object

`data/updates.json` powers the recent activity feed. Each item stores:

- `case_id`
- `case_name`
- `entry_date`
- `summary`
- `significance`
- `entry_number`
- `logged_at`

Updates are prepended newest-first when docket entries are discovered.

## Reliability Characteristics

The pipeline is designed to be idempotent and safe to re-run:

- known CourtListener docket identities are skipped during discovery (`id:<CourtListener id>` when available);
- rejected false-positive candidates are cached by the same stable identity;
- discovery query/page and RSS page positions are persisted with an anchored filing window and query-set hash;
- normalized candidates that encounter transient classifier failures remain durable across runs and consume classification budget before newly fetched candidates;
- docket entries are deduplicated by entry number;
- JSON writes are atomic;
- CourtListener requests share a persisted 24-hour rolling ledger, use tier-aware pacing and bounded retry/backoff, and honor `Retry-After` when it fits the remaining process budget;
- missing CourtListener credentials or authentication failures stop the run; the client never falls back to unauthenticated requests;
- model-provider requests use bounded retries;
- `MAX_DISCOVERY_CANDIDATES` caps each candidate-classification pass, while `MAX_DISCOVERY_PASSES_PER_JOB` and `MAX_DISCOVERY_JOB_SECONDS` bound the scheduled automatic drain; candidate-cap stops retain the exact query or RSS cursor for refetch-and-deduplicate processing;
- `MAX_SUMMARIES_PER_RUN` caps each model-backed docket-summary pass, and `MAX_DOCKET_UPDATE_PASSES` bounds how many catch-up passes the workflow runs before publication or CourtListener client deferral;
- malformed summary output falls back to deterministic text; transient relevance-classifier failures remain retryable in the cursor and are never cached as rejected;
- discovery failures or incomplete docket polling prevent the affected phase checkpoint from advancing.

## Configuration

Required GitHub Actions secrets:

- `COURTLISTENER_API_KEY`: CourtListener API token used for search and docket-entry polling.
- `ANTHROPIC_API_KEY`: Anthropic API key used for relevance classification and docket-entry summarization.
- `LEGAL_AI_MODEL`: model identifier used for relevance classification and docket-entry summarization. Keep this value in GitHub secrets rather than committing it to source.
- `IPTL_SITE_TOKEN`: GitHub token with permission to push tracker data into `taherezm/undergradtechlaw`.

Optional environment variables:

- `CL_REQUESTS_PER_MINUTE`: CourtListener minute-window sustained tier limit. Defaults to `5`; set a repository variable only when the signed-in API usage page shows a different sustained limit.
- `CL_REQUESTS_PER_HOUR`: CourtListener hour-window tier limit. Defaults to `50`.
- `CL_REQUESTS_PER_DAY`: CourtListener day-window tier limit. Defaults to `125`.
- `CL_SAFETY_MARGIN`: requests reserved from each rolling window. Defaults to `1`, making the default effective caps `4 / 49 / 124`.
- `CL_TIME_BUDGET_SECONDS`: per-process wall-clock budget for CourtListener waits and backoff. Defaults to `1500`.
- `CL_REQUEST_LOG_PATH`: persisted rolling-ledger path. Defaults to `data/cl_request_log.json`.
- `CL_BATCHED_CHANGE_DETECTION`: experimental docket change-detection pre-pass. Production sets this to `0`; keep it disabled until an observe-only comparison against full polling proves it cannot miss activity.
- `MAX_DISCOVERY_CANDIDATES`: maximum number of discovery candidates to classify per pass. Defaults to `5` and is explicitly fixed at `5` in GitHub Actions.
- `MAX_DISCOVERY_PASSES_PER_JOB`: maximum resumable discovery passes per workflow job. Defaults to `20`.
- `MAX_DISCOVERY_JOB_SECONDS`: discovery runner wall-clock budget before publishing its cursor. Defaults to `2700` (45 minutes).
- `MAX_DISCOVERY_STALENESS_DAYS`: completed-coverage and incomplete-cycle grace window used by the post-publication health check. Defaults to `2`.
- `MAX_SUMMARIES_PER_RUN`: maximum number of new docket-entry summaries to generate per pass. Defaults to `100`.
- `MAX_DOCKET_UPDATE_PASSES`: maximum number of docket-update/summarization passes per workflow job. Defaults to `2` in GitHub Actions.
- `FORCE_DISCOVERY`: set to `1`, `true`, or `yes` to force discovery when the completed checkpoint is already today; an active cursor always resumes without this flag.
- `RESET_COURTLISTENER_RATE_LIMIT_STATE`: clears the prior run-level rate-limit flag for a standalone discovery phase. The workflow sets this automatically.

## Local Development

Create a virtual environment and install dependencies:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

Run the deterministic regression suite:

```bash
python -m unittest discover -s tests -v
```

Run the production-equivalent pipeline order locally. These commands use live CourtListener and Anthropic credentials, consume quota/spend, and rewrite canonical files in `data/`:

```bash
export COURTLISTENER_API_KEY=...
export ANTHROPIC_API_KEY=...
export LEGAL_AI_MODEL=...

python scripts/run_docket_update_passes.py
python scripts/run_discovery_passes.py
python scripts/validate_tracker_data.py
```

For production-equivalent ordering, run `scripts/run_docket_update_passes.py` before `scripts/run_discovery_passes.py`. The scheduled workflow targets different rolling-hour quota windows with two daily runs nominally separated by five and a half hours, while the shared ledger handles delays or queued overlaps. `scripts/discover_cases.py`, `scripts/update_dockets.py`, and `scripts/summarize.py` remain available for targeted local debugging, but the two pass runners are the normal production entrypoints.

Validate the site renderer contract against a local checkout of the site repository:

```bash
python scripts/validate_site_renderer.py /path/to/undergradtechlaw/tools/litigation-tracker/index.html
```

Use `scripts/regenerate_case_summaries.py` only for an intentional maintenance/backfill pass over existing data; it is not part of the scheduled workflow. On environments where the interpreter is named `python3`, substitute `python3` in the commands above.

The generated JSON files in `data/` should be committed only when intentionally updating tracker state. If you are testing classifier behavior, use a small `MAX_DISCOVERY_CANDIDATES` value to avoid unnecessary API calls.

## Public Data Caveat

The tracker is an automated research aid. It is not legal advice, and docket summaries should be treated as public-facing abstracts of docket activity, not as authoritative statements of liability, merits, or procedural rights. The source of record remains the court docket and underlying filings.

## License

This project is licensed under the [MIT License](LICENSE).
