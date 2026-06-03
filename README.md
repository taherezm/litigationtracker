# AI/IP Litigation Tracker

Automated litigation data pipeline for IP & Technology Law at IU. This repository owns case discovery, docket monitoring, summary generation, and data publication into the live website repository at `taherezm/iptl-iu-site`.

## Architecture

The project is split into a pipeline repo and a static site repo. This repo produces normalized JSON data in `data/cases.json`, `data/updates.json`, and `data/last_run.json`. The site repo serves those files from `tools/litigation-tracker/` through GitHub Pages, where the tracker page fetches them client-side.

The pipeline has three layers:

- `scripts/discover_cases.py`: searches CourtListener for recent technology, AI, and IP litigation using keyword queries, RSS signals, docket metadata, deterministic AI/IP term matching, and Anthropic-assisted classification where needed. New case records are normalized, deduplicated by docket number, assigned stable IDs, and written to `data/cases.json`.
- `scripts/update_dockets.py`: monitors every active case with a CourtListener docket ID. It requests new docket entries since the last run, skips already-known entry numbers, appends new entries to each case, and prepends activity records to `data/updates.json`.
- `scripts/summarize.py`: summarizes unsummarized docket entries, classifies significance, updates procedural posture, records key rulings, and marks resolved cases when appropriate.

External API calls use environment-provided credentials only. CourtListener supplies search, docket, and docket-entry data. Anthropic is used for legal relevance checks and plain-English docket summaries, with deterministic fallbacks so the pipeline can still complete during model or API instability.

## Update Flow

GitHub Actions runs `.github/workflows/scheduled_update.yml` on a five-day schedule and by manual dispatch. Each run:

1. Checks out this repository.
2. Installs Python dependencies.
3. Runs discovery, docket update, and summarization.
4. Commits changed `data/` files back to this repository.
5. Checks out `taherezm/iptl-iu-site` using `IPTL_SITE_TOKEN`.
6. Copies `cases.json` and `updates.json` into `tools/litigation-tracker/`.
7. Commits and pushes those data files to the site repository.

Because GitHub Pages serves the site repo from `main`, pushed tracker data becomes available at the live site without a separate deployment step.

Live tracker: https://taherezm.github.io/iptl-iu-site/tools/litigation-tracker/

## Data Model

`cases.json` is the canonical case index. Each case stores court metadata, docket identifiers, parties, claims, tags, current status, procedural posture, key rulings, docket entries, and a plain-language summary.

`updates.json` is the recent activity feed. It is ordered newest first and contains new docket activity keyed back to the relevant case.

`last_run.json` stores scheduler state: discovery counts, update counts, completion flags, and rejected docket IDs used to avoid repeatedly processing irrelevant search results.

## Reliability

The scripts are idempotent. Re-running the pipeline does not duplicate known cases or docket entries. CourtListener requests use retries, bounded backoff, and rate-limit handling. If CourtListener throttles a run, the workflow records partial progress and tries again on the next scheduled or manual run.

## Configuration

Required GitHub Actions secrets:

- `COURTLISTENER_API_KEY`: CourtListener API token.
- `ANTHROPIC_API_KEY`: Anthropic API key.
- `IPTL_SITE_TOKEN`: GitHub token with access to push into `taherezm/iptl-iu-site`.

The workflow runs automatically at `13:00 UTC` on the configured five-day schedule and can also be triggered manually from the Actions tab.

## Local Development

Install dependencies:

```bash
pip install -r requirements.txt
```

Run the pipeline locally with credentials in the environment:

```bash
export COURTLISTENER_API_KEY=...
export ANTHROPIC_API_KEY=...
python scripts/discover_cases.py
python scripts/update_dockets.py
python scripts/summarize.py
```

The generated JSON files in `data/` should be committed only when intentionally updating the tracker state.
