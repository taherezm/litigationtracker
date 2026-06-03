# AI/IP Litigation Tracker

Automated litigation data pipeline for IP & Technology Law at IU. The system discovers federal cases involving artificial intelligence and intellectual property, monitors docket activity through CourtListener, summarizes new entries with legal-precision guardrails, and publishes normalized JSON data to the static website served from `taherezm/iptl-iu-site`.

Live tracker: upon site deployment 

## What This Repository Owns

This repository is the data and automation layer. It does not render the public UI directly. Instead, it maintains the canonical tracker state in:

- `data/cases.json`: the case index consumed by the website.
- `data/updates.json`: recent docket activity keyed back to cases.
- `data/last_run.json`: scheduler state, rate-limit state, discovery counters, and rejected docket cache.

The static site repository (`taherezm/iptl-iu-site`) owns the browser UI. The GitHub Actions workflow in this repository pushes updated `cases.json` and `updates.json` into `tools/litigation-tracker/` in the site repository, where GitHub Pages serves them as static assets. The public tracker page fetches those JSON files client-side.

## Pipeline Overview

The tracker runs as a three-stage ETL pipeline:

1. `scripts/discover_cases.py`
   Searches for new candidate cases, classifies AI/IP relevance, normalizes accepted candidates into case records, and writes them into `data/cases.json`.
2. `scripts/update_dockets.py`
   Polls CourtListener docket entries for every active tracked case, appends new entries, and prepends recent activity records into `data/updates.json`.
3. `scripts/summarize.py`
   Summarizes unsummarized docket entries, classifies their litigation significance, updates procedural posture, records key rulings, and marks resolved cases when appropriate.

Each stage reads and writes JSON directly. Writes are atomic: data is serialized to a temporary sibling file and then moved into place with `Path.replace()`.

## Discovery

Discovery combines deterministic search heuristics with model-assisted legal relevance classification.

### Candidate Sources

`discover_cases.py` collects candidates from two sources:

- CourtListener search API (`/api/rest/v4/search/`) with `type=d`, sorted by search score.
- Courthouse News RSS (`https://www.courthousenews.com/feed/`) as a secondary signal when the feed text contains litigation terms and a docket number pattern.

The CourtListener query set is intentionally broad. It covers generative AI, training data, LLMs, right of publicity, image generation, software patent disputes, scraping, open-source licensing, biometric/privacy terms, blockchain/NFT issues, trade secret language, and other technology/IP indicators.

By default, discovery stops after `DEFAULT_MAX_DISCOVERY_CANDIDATES = 5` candidates. The cap can be overridden with:

```bash
export MAX_DISCOVERY_CANDIDATES=20
```

The default cap is a rate-limit control. CourtListener queries are spaced by `COURTLISTENER_REQUEST_PAUSE_SECONDS = 4` plus jitter, and API failures use bounded exponential backoff.

### Search Window

Discovery uses `data/last_run.json` to decide the filing-date window:

- If fewer than five cases are already tracked, or no `last_run_date` exists, it searches the last 90 days.
- Otherwise, it searches from `last_run_date`.

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
  date_filed__gte=<last_run_date>
  order_by=-entry_number
  page_size=50
```

If `last_run_date` is missing, the fallback window is the previous five days.

### Entry Deduplication

New entries are deduplicated by normalized `entry_number`. If CourtListener returns an entry number already present in the case's `docket_entries`, it is skipped.

New entries are appended to the case:

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

### Resolution Signals

Some docket text is treated as requiring review. If the raw entry contains terms such as `JUDGMENT`, `DISMISSED WITH PREJUDICE`, `SETTLED`, `AFFIRMED`, `REVERSED`, or `MANDATE ISSUED`, the case status is moved to `needs_review`. The summarization stage may later classify the entry as `case_resolved` and mark the case `resolved`.

## Summarization and Legal Precision

`summarize.py` processes every docket entry that has raw text but no summary. It prompts Anthropic to return strict JSON:

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

`data/last_run.json` tracks run state across phases:

```json
{
  "cases_discovered": 5,
  "discovery_complete": false,
  "rejected_dockets": ["3:26-cv-04053"],
  "entries_updated": 48,
  "docket_update_complete": true,
  "summaries_generated": 48,
  "last_run_date": "2026-06-03"
}
```

`last_run_date` advances only when both discovery and docket update completed without rate-limit interruption. If either phase is incomplete, the summarizer writes a warning and leaves `last_run_date` unchanged so the next scheduled run can reprocess the missed window.

## Publication Flow

GitHub Actions runs `.github/workflows/scheduled_update.yml` every five days at `13:00 UTC` and also supports manual dispatch.

The job runs on Ubuntu with Python 3.11:

1. Check out this pipeline repository.
2. Install Python dependencies from `requirements.txt`.
3. Run `scripts/discover_cases.py`.
4. Run `scripts/update_dockets.py`.
5. Run `scripts/summarize.py`.
6. Validate generated tracker data.
7. Commit any changed files in `data/` back to this repository.
8. Check out `taherezm/iptl-iu-site` using `IPTL_SITE_TOKEN`.
9. Validate the site tracker renderer contract.
10. Copy `data/cases.json` and `data/updates.json` into `iptl-iu-site/tools/litigation-tracker/`.
11. Commit and push changed tracker data to the site repository.

Because the site repository is served by GitHub Pages from `main`, the copied JSON files become available to the public tracker after the site repo deploys.

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
- CourtListener search can fall back to unauthenticated requests on auth errors or persistent authenticated rate limits;
- Anthropic requests use bounded retries;
- malformed model JSON falls back to deterministic summaries or deterministic relevance checks;
- incomplete discovery or docket polling prevents `last_run_date` from advancing.

## Configuration

Required GitHub Actions secrets:

- `COURTLISTENER_API_KEY`: CourtListener API token used for search and docket-entry polling.
- `ANTHROPIC_API_KEY`: Anthropic API key used for relevance classification and docket-entry summarization.
- `IPTL_SITE_TOKEN`: GitHub token with permission to push tracker data into `taherezm/iptl-iu-site`.

Optional environment variables:

- `MAX_DISCOVERY_CANDIDATES`: maximum number of discovery candidates to classify per run. Defaults to `5`.
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

python scripts/discover_cases.py
python scripts/update_dockets.py
python scripts/summarize.py
python scripts/validate_tracker_data.py
```

Validate the site renderer contract against a local checkout of the site repository:

```bash
python scripts/validate_site_renderer.py ../iptl-iu-site/tools/litigation-tracker/index.html
```

The generated JSON files in `data/` should be committed only when intentionally updating tracker state. If you are testing classifier behavior, use a small `MAX_DISCOVERY_CANDIDATES` value to avoid unnecessary API calls.

## Public Data Caveat

The tracker is an automated research aid. It is not legal advice, and docket summaries should be treated as public-facing abstracts of docket activity, not as authoritative statements of liability, merits, or procedural rights. The source of record remains the court docket and underlying filings.
