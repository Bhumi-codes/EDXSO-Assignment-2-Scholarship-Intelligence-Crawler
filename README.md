# Scholarship Intelligence

A working Python crawler that discovers scholarship opportunities for Indian students, extracts facts from official HTML and PDF sources, checks source evidence, calculates confidence, and presents the results in a local dashboard.

No API keys, paid services, or language-model downloads are needed.

## Quick start

Install **Python 3.11** and [uv](https://docs.astral.sh/uv/getting-started/installation/). Download this repository or clone it, open the project folder in VS Code, and run these commands from that folder:

```powershell
uv sync --locked
uv run python -m scholarship_intelligence start
```

The dashboard opens at **http://127.0.0.1:8501** with genuine saved crawl results: **296 scholarships** from the five configured sources. Keep the terminal running; stop with `Ctrl+C`. The command also accepts `--no-crawl` for compatibility, but saved-results startup is now the default.

On a fresh checkout, the dashboard shows the saved records immediately while their full evidence files prepare in the background. This one-time preparation may take around a minute depending on disk speed; **Crawl now** becomes available when it finishes. The original source-check and verification dates are preserved. Loading saved results is not a live crawl. Existing nonempty databases are never replaced.

Press **Crawl now** to check the selected sources live in the background. You can browse existing records during the update. The interface reports progress and completion; the terminal remains running to serve the dashboard after the crawl finishes. Live checks still take time depending on source size, request spacing and website response times.

The same commands work in a macOS/Linux terminal. Python 3.11 on Windows is the tested environment. Internet access is needed for installation and live crawling; the bundled saved results can be browsed offline after dependencies are installed.

If `uv` is unavailable, use a standard virtual environment:

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python -m scholarship_intelligence start
```

On macOS/Linux, use `.venv/bin/python` instead of `.venv\Scripts\python`.

## Running the crawler

Every **Crawl now** and scheduled run performs live checks:

1. Revisit listing pages to discover new opportunities and check known source pages.
2. Send conditional GET requests when saved ETag or Last-Modified validators are available. A valid HTTP 304 reuses the saved body after checking its hash.
3. If validators are unavailable, download the page and compare its content hash.
4. Reuse unchanged extraction and independent evidence checks only when their saved artifacts, configuration and processing rules still match.
5. Extract and validate new or changed content, retaining old/new values and evidence through the existing update workflow.
6. Record inaccessible sources for re-verification rather than declaring them expired. Recalculate date-based lifecycle, freshness and confidence.
7. Save the run report and refresh the dashboard.

An unchanged page may still require downloading if the server does not support conditional requests. This saves repeated extraction work; it does not claim to detect changes without contacting sources. Code/configuration changes invalidate the relevant reuse receipts.

Run the complete workflow without starting the dashboard:

```powershell
uv run python -m scholarship_intelligence workflow-all
```

Run one complete source:

```powershell
uv run python -m scholarship_intelligence workflow --seed-id iit_delhi
```

Configured source IDs in `config/seeds.json`:

- `iit_delhi`: IIT Delhi; university.
- `nsp`: National Scholarship Portal; government.
- `hdfc_ecss`: HDFC Parivartan ECSS; corporate CSR.
- `ffe`: Foundation for Excellence; NGO/trust.
- `chevening_india`: Chevening India; international.

Seeds are starting points. The crawler follows relevant links and discovers individual opportunities; NSP also enumerates supported portal filters. Source adapters isolate catalogue cards and programme sections so that shared pages are not treated as a single scholarship. Awards and other excluded programme types are filtered out of the main dashboard.

Each source has a safety budget of 1,000 fetched pages. A run reports failures and skipped URLs; it cannot promise complete coverage of inaccessible, unlinked, private, or JavaScript-only content. Inspect `data/runtime/live-source-workflows.json` for the latest full report.

To recover a stopped source workflow using its saved live-crawl snapshots:

```powershell
uv run python -m scholarship_intelligence workflow --seed-id iit_delhi --resume
```

This recovery does not claim to fetch a new source revision. To observe new changes, run again without `--resume`.

To recheck only identities already stored, while still crawling live sources:

```powershell
uv run python -m scholarship_intelligence workflow-all --existing-only
```

## Dashboard and automation

The interface includes search, provider/source/application-status filters, source selection, **Crawl now**, and **Automatic daily crawl**. Summary cards show total stored scholarships, verified, review required, active, expired, and average confidence. Opening a scholarship shows available facts, dates, application link, scoring reasons, source evidence and change history.

Daily crawling runs at **09:00 IST while the local dashboard server is running**, with one catch-up after a missed schedule. It cannot wake a powered-off computer. Overlapping dashboard runs are blocked. This is a local demonstration application; it is not configured for public multi-user hosting.

The total-discovered card currently counts stored scholarship records matching the filters. Active includes expiring-soon records. Recently updated is calculated from accepted factual changes; initial imports and score-only recalculations do not count as changes.

## Evidence and confidence

The automatic flow is:

`Discover → Crawl → Prepare HTML/PDF → Extract with Python → Validate evidence → Score → Store/update → Dashboard`

Original response bytes, source URL, retrieval timestamp and content hash are retained. HTML quotations reference source blocks; PDF quotations reference physical pages. Separate PDFs are not required when official HTML contains the evidence. Unsupported facts remain unspecified or withheld.

Confidence uses three components:

- **30% official-source authenticity:** configured provider ownership and source association.
- **50% supported fields:** independent checks of important extracted values, field context, exact quotations and completeness of cited passages.
- **20% dated offering context:** a supported student application window, or another explicitly supported dated programme context.

`Confidence = 0.30 × authenticity + 0.50 × field support + 0.20 × dated context`

Only records scoring **at least 95**, with every critical gate satisfied, receive `VERIFIED`. Unresolved source conflicts, unsupported core identity/benefits/eligibility, missing application routing, incomplete required supporting material, or invalid freshness prevent verification. Source freshness requires retrieval within 30 days. Optional unspecified fields do not become invented values or automatic passes. There is no artificial confidence floor or bonus to meet the assignment quota.

Application status is separate: a factually supported scholarship may already be expired. `ACTIVE`, `EXPIRING_SOON`, `EXPIRED`, and `REVIEW_REQUIRED` follow supported source dates/context. Unknown dates never imply that an application is open. Offering cycle identifies the advertised round; application status describes its window at the evaluation date.

## Changes and expiry demonstration

Normal repeated crawls preserve raw revisions and report old/new extracted values with evidence references. Source changes are distinguished from extraction-rule changes. Unresolved revisions remain review proposals; previously accepted facts are preserved.

The separate historical replay command is:

```powershell
uv run python -m scholarship_intelligence replay-official-notices
```

It fetches genuine official notices and demonstrates two deadline revisions (NMMSS and CSSS renewal instructions from Sacred Heart College), plus historical active-to-expired evaluations for Chevening India and DAAD HSK. It uses an isolated database under `data/runtime/official-notice-replay/` and never adds examples to the main database.

This is **historical official-notice replay**, not a change observed between two production crawls today. The expiry examples advance the evaluation date across real published windows; they do not invent an earlier crawl. A changed example counts only while its later deadline remains unexpired on the actual run date. The CSSS case demonstrates institutional renewal instructions, not an independently established national policy revision. Source failures or expired example deadlines produce an incomplete result and a nonzero exit code.

The scanned notice in this optional replay requires **Windows English OCR** and **Poppler `pdftoppm` on PATH**. Install these before running the replay. The normal HTML/text-PDF workflow works without them. Replay output is in `data/runtime/official-notice-replay/latest.json`, with per-run evidence and SQLite history alongside it.

## Data and repository layout

- `scholarship_intelligence/`: crawler, extraction, validation, confidence, persistence, scheduling and dashboard code.
- `scholarship_intelligence/schema.sql`: SQLite schema.
- `config/seeds.json`: live source configuration and grouping rules.
- `tests/`: isolated automated fixtures and regression tests; test data never enters the application database.
- `data/samples/live-crawl-logs/`: five unmodified genuine HTTP event logs and their checksum manifest.
- `data/samples/saved-results.zip.part*`: automatically loaded saved database, record-referenced raw/prepared source snapshots, extraction packets, validation receipts and genuine crawl logs. Keep all parts; do not unpack or run them separately. Each part is below GitHub's ordinary file-size limit.
- `data/samples/saved-results.manifest.json`: checksums for the archive parts and every restored artifact.
- `data/samples/saved-view.json.gz`: checksummed dashboard view generated from the independently validated saved records. Freshness and expiry are evaluated again when displayed.
- `data/samples/sample-scholarships.json`: twenty automatically extracted real examples, separate from production data.
- `data/samples/validation-run.json`: measurements and source coverage from a fresh empty-database validation run.
- `docs/release-checks.md`: completed release checks.
- `data/runtime/`: newly generated database, raw/prepared snapshots, candidates, assessments and reports. Ignored by Git.
- `docs/technical-note.md`: architecture, extraction, verification and known limitations.

The submission excludes old manually authored records, manual FFE assessments, Gemini code, API queues, synthetic demonstration code, credentials, virtual environments, locks, backups and development scratch files. A normal live crawl of the FFE website remains enabled.

## Validation and limitations

Run the automated suite:

```powershell
uv run python -m unittest discover -s tests -v
```

Tests also cover conditional HTTP requests, verified cache reuse, changed-content invalidation, saved-results integrity, date preservation and failed-source handling. Tests cover source grouping, exact evidence matching, PDF page validation, date/benefit extraction, scoring gates, duplicate prevention, changes, transaction rollback and scheduler controls. Synthetic fixtures are deliberately confined to temporary test directories. Tests exercise code and do not certify the accuracy or accessibility of every live website.

The bundled genuine crawl completed on **15 October 2026** and contains **296 scholarships, 296 official-source confirmations, and 4 records at ≥95%**. These are saved crawl results, not a guarantee of current availability or the next live run. Freshness and expiry can change the displayed results over time. The assignment's **10+ records at ≥95% requirement remains unmet**. Official-source confirmation must not be confused with whole-record `VERIFIED` status.

Known extraction limits include scanned PDFs without normal-workflow OCR, conditional amount tables, shared multi-programme guidance, incomplete source information, and literal eligibility prose rather than a universal applicant-decision engine. Incomplete records remain review required. Counts, deadlines and access restrictions can change between runs.

If a source times out or returns 403/503, inspect the report, then retry the relevant source. Never raise a confidence score to compensate for inaccessible evidence. If port 8501 is occupied, use `start --no-crawl --port 8502`.

## GitHub submission

Commit this folder's source, configuration, lockfile, requirements, documentation, tests and all `data/samples` assets (including the three archive parts). Keep the provided `.gitignore`; generated runtime data and environments are excluded. No environment file or secret configuration is required. The reviewer can follow Quick start from a fresh checkout.
