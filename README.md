# Undergrad Tech Law Litigation Tracker

[![Scheduled Update](https://github.com/taherezm/litigationtracker/actions/workflows/scheduled_update.yml/badge.svg)](https://github.com/taherezm/litigationtracker/actions/workflows/scheduled_update.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.11](https://img.shields.io/badge/Python-3.11-blue.svg)](https://www.python.org/)

Automated litigation data pipeline for Undergraduate Technology Law at IU. The system discovers federal cases involving artificial intelligence and intellectual property, monitors docket activity through CourtListener, summarizes new entries with legal-precision guardrails, and publishes normalized JSON data to the static website served from `taherezm/undergradtechlaw`.

Live tracker: [undergradtechlaw.org/tools/litigation-tracker](https://www.undergradtechlaw.org/tools/litigation-tracker/).

## At a Glance

- Source repository: `taherezm/litigationtracker`
- Public site repository: `taherezm/undergradtechlaw`
- Public data path: `tools/litigation-tracker/cases.json` and `tools/litigation-tracker/updates.json`
- Schedule: GitHub Actions cron at `13:17 UTC` on the configured five-day cadence (`17 13 */5 * *`)
- Pipeline: update dockets, summarize entries, discover cases, validate JSON, publish to the site
- Cost controls: discovery is capped at 5 candidates by default, and docket summaries are capped at 100 new entries per run
- Publication rule: unsummarized or placeholder docket activity is not allowed into public JSON

## Current Public Version

The public tracker is the static UI in `taherezm/undergradtechlaw` at `tools/litigation-tracker/`. This repository owns the canonical data and automation; each successful workflow run copies `data/cases.json` and `data/updates.json` into the site repo. The browser page renders case counts, court counts, latest update date, and significant-ruling totals from those JSON files at runtime, so this README describes the pipeline contract rather than hard-coded live totals.

On the current five-day cadence, the tracker checks existing dockets before discovering new cases. This keeps already-tracked litigation fresh under CourtListener/API limits, while per-case docket checkpoints let interrupted runs resume from saved progress instead of restarting the full window.

## What This Repository Owns

This repository is the data and automation layer. It does not render the public UI directly. Instead, it maintains the canonical tracker state in:

- `data/cases.json`: the case index consumed by the website.
- `data/updates.json`: recent docket activity keyed back to cases.
- `data/last_run.json`: scheduler state, rate-limit state, discovery counters, and rejected docket cache.

The static site repository (`taherezm/undergradtechlaw`) owns the browser UI. The GitHub Actions workflow in this repository checks that repository out into a local `iptl-iu-site/` directory, pushes updated `cases.json` and `updates.json` into `tools/litigation-tracker/`, and lets GitHub Pages serve them as static assets. The public tracker page fetches those JSON files client-side.

## Pipeline Overview

The tracker runs as a three-stage ETL pipeline, in this execution order:

1. `scripts/update_dockets.py`
   Polls CourtListener docket entries for every active tracked case from each case's own `docket_last_checked` checkpoint, appends new entries, and prepends recent activity records into `data/updates.json`. Run via `scripts/run_docket_update_passes.py`, which alternates docket and summarization passes.
2. `scripts/summarize.py`
   Summarizes unsummarized docket entries, classifies their litigation significance, updates procedural posture, records key rulings, and marks resolved cases when appropriate.
3. `scripts/discover_cases.py`
   Searches for new candidate cases, classifies AI/IP relevance, normalizes accepted candidates into case records, and writes them into `data/cases.json`. Runs last so docket freshness gets priority on the shared CourtListener request quota; same-day reruns skip discovery unless `FORCE_DISCOVERY` is set.

Each stage reads and writes JSON directly. Writes are atomic: data is serialized to a temporary sibling file and then moved into place with `Path.replace()`.

## Discovery

Discovery combines deterministic search heuristics with model-assisted legal relevance classification.

### Candidate Sources

`discover_cases.py` collects candidates from two sources:

- CourtListener search API (`/api/rest/v4/search/`) with `type=d`, sorted by search score.
- Courthouse News RSS (`https://www.courthousenews.com/feed/`) as a secondary signal when the feed text contains litigation terms and a docket number pattern.

The CourtListener query set is intentionally broad. It covers generative AI, training data, LLMs, right of publicity, image generation, software patent disputes, scraping, open-source licensing, biometric/privacy terms, blockchain/NFT issues, trade secret language, and other technology/IP indicators.

By default, discovery stops after `DEFAULT_MAX_DISCOVERY_CANDIDATES = 5` candidates. The cap bounds how many new candidates are classified in one run and can be overridden with:

```bash
export MAX_DISCOVERY_CANDIDATES=20
```

The default cap is a rate-limit control. Hitting it sets `discovery_candidate_cap_reached: true`, but it does not by itself block the discovery checkpoint. CourtListener queries are spaced by `COURTLISTENER_REQUEST_PAUSE_SECONDS = 4` plus jitter, and API failures use bounded exponential backoff.

### Search Window

Discovery uses `data/last_run.json` to decide the filing-date window:

- If fewer than five cases are already tracked, or no completed discovery checkpoint exists, it searches the last 90 days.
- Otherwise, it searches from `discovery_last_run_date`.

This keeps early bootstrap runs broad while making mature runs incremental.

### Deduplication and Rejection Cache

Candidate deduplication is docket-number based. Existing cases are keyed by normalized docket number, and previously rejected dockets are stored in `last_run.rejected_dockets`.

Rejected dockets are kept because broad keyword searches repeatedly surface false positives. The cache is capped to the most recent 500 rejected docket keys:

```json
{
  "rejected_dockets": ["3:26-cv-04053"]
}
```

### Relevance Classification

The pipeline first attempts deterministic classification using term matching:

- AI terms: `artificial intelligence`, `generative`, `OpenAI`, `Anthropic`, `ChatGPT`, `LLM`, `machine learning`, `training data`, `stable diffusion`, `neural network`, etc.
- IP claim terms: copyright, patent, trade secret, DTSA, right of publicity, voice cloning, deepfake, DMCA 1202, trademark, and related statutory markers.

If the deterministic classifier cannot confidently mark a candidate relevant, Anthropic is used as a legal classifier. The classifier is prompted to decide whether the case is primarily or substantially about intellectual property claims arising from or directly involving AI systems, AI-generated content, or AI training data. It must return strict JSON:

```json
{
  "relevant": true,
  "confidence": "high",
  "reason": "The dispute alleges copyright infringement based on model training data.",
  "claims": ["copyright infringement"],
  "tags": ["training data", "LLM", "copyright"]
}
```

Low-confidence or irrelevant results are rejected unless the deterministic fallback can independently identify both an AI signal and an IP claim signal.

### Case Record Construction

Accepted candidates are normalized into public case records. Each record gets:

- stable slug ID derived from case name or docket number;
- caption, court, docket number, CourtListener docket ID, and CourtListener URL;
- filing date, parties, judges where available, claims, tags, and status;
- default procedural posture (`Filed`);
- empty `key_rulings` and `docket_entries` arrays;
- a public plain-language case summary.

Plain-language case summaries are deterministic by default. If `USE_ANTHROPIC_CASE_SUMMARIES=1`, Anthropic may generate the initial case summary, but the output is still passed through legal-precision filters. The deterministic fallback avoids overstating liability and uses allegation language such as "asserts" instead of "violated."

## Docket Monitoring

`update_dockets.py` monitors every case with:

```json
{
  "status": "active",
  "courtlistener_docket_id": "..."
}
```

For each active case, the script calls the CourtListener docket entries API:

```text
GET /api/rest/v4/docket-entries/
  docket=<courtlistener_docket_id>
  date_filed__gte=<case docket_last_checked - 2 days, or fallback>
  order_by=-entry_number
  page_size=50
```

Each case carries its own `docket_last_checked` checkpoint. When a case is fully checked in a run, its checkpoint advances to that day, even if the run is later rate-limited or capped while checking other cases. This makes progress permanent: a throttled run never causes the next run to refetch windows that already completed. The two-day overlap re-covers entries that are docketed late, and entry-number deduplication makes the overlap harmless.

Cases are processed stalest-checkpoint-first so repeated rate-limited runs rotate coverage across every active docket instead of starving the cases checked last. Cases without a checkpoint fall back to the global `docket_last_run_date` (or the previous five days if that is also missing). Newly discovered cases with no stored docket entries are polled from their filing date so the first docket update can backfill the initial case history.

The docket update stage honors `MAX_SUMMARIES_PER_RUN`, which defaults to `100`. The cap is enforced while paginating, so one backlogged docket cannot consume the whole run's API budget, and it is applied before new entries are committed into the public data files so every newly accepted docket entry can be summarized in the same run. If the cap interrupts a case, `docket_update_complete` is set to `false`, that case's checkpoint is not advanced, and overflow is retried on the next pass or run.

If CourtListener answers `429` with a `Retry-After` longer than the in-run backoff budget (30 seconds), the run stops polling immediately instead of burning retries, publishes what it has, and leaves the remaining checkpoints for the next run.

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

### Resolution Signals

Some docket text is treated as requiring review. If the raw entry contains terms such as `JUDGMENT`, `DISMISSED WITH PREJUDICE`, `SETTLED`, `AFFIRMED`, `REVERSED`, or `MANDATE ISSUED`, the case status is moved to `needs_review`. The summarization stage may later classify the entry as `case_resolved` and mark the case `resolved`.

## Summarization and Legal Precision

`summarize.py` processes docket entries that have raw text but no summary, up to the configured `MAX_SUMMARIES_PER_RUN` budget. It prompts Anthropic to return strict JSON:

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

## Scheduler State

`data/last_run.json` tracks run state across phases. The shape is:

```json
{
  "cases_discovered": 0,
  "discovery_complete": true,
  "discovery_candidate_cap_reached": false,
  "rejected_dockets": ["3:26-cv-04053"],
  "entries_updated": 0,
  "docket_update_complete": false,
  "courtlistener_rate_limited": true,
  "docket_entry_cap_reached": false,
  "summaries_generated": 0,
  "summaries_deferred": 0,
  "max_summaries_per_run": 100,
  "max_docket_update_passes": 5,
  "discovery_last_run_date": "YYYY-MM-DD",
  "docket_last_run_date": "YYYY-MM-DD",
  "last_run_date": "YYYY-MM-DD"
}
```

`discovery_last_run_date` and `docket_last_run_date` advance independently. `discover_cases.py` owns the discovery checkpoint and advances it when discovery completes. `docket_last_run_date` advances when a docket pass completes every active case; it is now only the fallback window for cases that do not yet carry their own `docket_last_checked` checkpoint. `last_run_date` remains a legacy all-phases-complete checkpoint and advances only when both discovery and docket update completed. A candidate-cap hit is treated as a normal budget state and recorded in `discovery_candidate_cap_reached`; API failures or classifier failures can still leave `discovery_complete: false` and prevent the discovery checkpoint from advancing.

`courtlistener_rate_limited` describes the current run only: `run_docket_update_passes.py` clears it at the start of each job, and the docket and discovery stages OR their own results into it.

When `docket_entry_cap_reached` is `true`, the run hit the configured summary budget. Valid summarized entries still publish, and per-case checkpoints for fully checked cases still advance; only the interrupted and unreached cases retry from their prior checkpoints. The scheduled workflow runs multiple bounded docket/summarization passes before publication so a large backlog can clear in one job instead of waiting for the next scheduled run.

## Publication Flow

GitHub Actions runs `.github/workflows/scheduled_update.yml` every five days at `13:17 UTC` and also supports manual dispatch. The run is offset from the top of the hour to reduce schedule-delay risk on GitHub Actions. Each run covers a roughly five-day docket window per case; per-case checkpoints and the bounded multi-pass catch-up absorb that window, and any portion cut off by a CourtListener rate limit resumes from the saved checkpoints on the next run.

The job runs on Ubuntu with Python 3.11:

1. Check out this pipeline repository.
2. Install Python dependencies from `requirements.txt`.
3. Run `scripts/run_docket_update_passes.py`, which performs bounded `scripts/update_dockets.py` and `scripts/summarize.py` passes until `docket_update_complete` is true, `MAX_DOCKET_UPDATE_PASSES` is reached, or CourtListener rate-limits docket polling.
4. Run `scripts/discover_cases.py`. Docket updates run first so tracked cases get first claim on the CourtListener request quota; discovery is skipped if it already completed earlier the same day, and newly discovered cases backfill on the next run.
5. Validate generated tracker data.
6. Commit any changed files in `data/` back to this repository.
7. Check out `taherezm/undergradtechlaw` using `IPTL_SITE_TOKEN`.
8. Validate the site tracker renderer contract.
9. Copy `data/cases.json` and `data/updates.json` into `iptl-iu-site/tools/litigation-tracker/`.
10. Commit and push changed tracker data to the site repository.

Because the site repository is served by GitHub Pages from `main`, the copied JSON files become available to the public tracker after the site repo deploys.

## Operations

Use the scheduled workflow for routine updates. Manual `workflow_dispatch` runs are supported, but they use live CourtListener and Anthropic API calls, so they should be treated as real production runs.

Avoid cancelling in-flight runs and avoid back-to-back manual dispatches: data only commits at the end of a job, so a cancelled run loses its fetched entries while still having spent the CourtListener request quota that the next run needs.

Expected non-fatal warning states:

- CourtListener rate limits can leave `courtlistener_rate_limited: true`; valid fetched data still publishes, per-case checkpoints that completed still advance, and the missed window is retried.
- The discovery candidate cap can leave `discovery_candidate_cap_reached: true`; valid classified cases still publish, and the discovery checkpoint can still advance unless another discovery failure occurred.
- The summary cap can leave `docket_entry_cap_reached: true`; valid summarized data still publishes, and the workflow runs additional bounded passes before leaving overflow for the next run. CourtListener rate limits stop additional same-job passes so the workflow does not repeatedly call a limited API.
- CourtListener or classifier failures can leave `discovery_complete: false`; discovery resumes from the prior discovery checkpoint.

Routine maintenance checklist:

1. Confirm the GitHub Actions workflow is active.
2. Confirm repository secrets exist: `COURTLISTENER_API_KEY`, `ANTHROPIC_API_KEY`, and `IPTL_SITE_TOKEN`.
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

### `scripts/validate_site_renderer.py`

This validates that the site repository still contains the expected tracker renderer contract before data is copied into it. It checks for required snippets such as:

- `Activity Dates`
- `renderActivityDays(activityDays)`
- `publicEntryText(entry)`

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
- `status`: `active`, `needs_review`, or `resolved`.
- `procedural_posture`: normalized litigation stage.
- `parties`: parsed plaintiff/defendant names.
- `judges`: judge names where available.
- `key_rulings`: significant rulings extracted from docket activity.
- `docket_entries`: tracked docket entries.
- `plain_language_summary`: public case summary.
- `tags`: AI/IP tags used by the front end.
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

- known docket numbers are skipped during discovery;
- rejected false-positive dockets are cached;
- docket entries are deduplicated by entry number;
- JSON writes are atomic;
- CourtListener requests use retry/backoff and honor `Retry-After` where possible;
- CourtListener auth failures stop the run with an explicit secret-configuration error instead of silently falling back to unauthenticated requests;
- Anthropic requests use bounded retries;
- `MAX_DISCOVERY_CANDIDATES` caps candidate classification per run and records `discovery_candidate_cap_reached` without blocking the discovery checkpoint;
- `MAX_SUMMARIES_PER_RUN` caps each model-backed docket-summary pass, and `MAX_DOCKET_UPDATE_PASSES` bounds how many catch-up passes the workflow runs before publication or CourtListener rate limiting;
- malformed model JSON falls back to deterministic summaries or deterministic relevance checks;
- discovery failures or incomplete docket polling prevent the affected phase checkpoint from advancing.

## Configuration

Required GitHub Actions secrets:

- `COURTLISTENER_API_KEY`: CourtListener API token used for search and docket-entry polling.
- `ANTHROPIC_API_KEY`: Anthropic API key used for relevance classification and docket-entry summarization.
- `IPTL_SITE_TOKEN`: GitHub token with permission to push tracker data into `taherezm/undergradtechlaw`.

Optional environment variables:

- `MAX_DISCOVERY_CANDIDATES`: maximum number of discovery candidates to classify per run. Defaults to `5`.
- `MAX_SUMMARIES_PER_RUN`: maximum number of new docket-entry summaries to generate per run. Defaults to `100`.
- `MAX_DOCKET_UPDATE_PASSES`: maximum number of docket-update/summarization passes per workflow job. Defaults to `5` in GitHub Actions.
- `FORCE_DISCOVERY`: set to `1`, `true`, or `yes` to force discovery even when `discovery_last_run_date` is already today.
- `USE_ANTHROPIC_CASE_SUMMARIES`: set to `1`, `true`, or `yes` to allow model-generated initial case summaries instead of deterministic summaries.

## Local Development

Install dependencies:

```bash
pip install -r requirements.txt
```

Run the full pipeline locally:

```bash
export COURTLISTENER_API_KEY=...
export ANTHROPIC_API_KEY=...

python scripts/run_docket_update_passes.py
python scripts/discover_cases.py
python scripts/validate_tracker_data.py
```

For production-equivalent ordering, run `scripts/run_docket_update_passes.py` before `scripts/discover_cases.py`. The scheduled workflow does that so tracked dockets get first claim on the shared CourtListener request quota. `scripts/update_dockets.py` and `scripts/summarize.py` remain available for targeted local debugging, but the pass runner is the normal catch-up entrypoint.

Validate the site renderer contract against a local checkout of the site repository:

```bash
python scripts/validate_site_renderer.py ../iptl-iu-site/tools/litigation-tracker/index.html
```

The generated JSON files in `data/` should be committed only when intentionally updating tracker state. If you are testing classifier behavior, use a small `MAX_DISCOVERY_CANDIDATES` value to avoid unnecessary API calls.

## Public Data Caveat

The tracker is an automated research aid. It is not legal advice, and docket summaries should be treated as public-facing abstracts of docket activity, not as authoritative statements of liability, merits, or procedural rights. The source of record remains the court docket and underlying filings.

## License

This project is licensed under the [MIT License](LICENSE).
