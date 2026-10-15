# Scholarship Intelligence Crawler

An automated scholarship crawler for Indian students. It discovers opportunities from official sources, extracts HTML/PDF content into a common schema, validates supporting evidence, calculates confidence, and maintains a searchable Streamlit dashboard.

The application uses Python rules. No API key, paid service, or language-model download is required.

## Quick start

Install **Python 3.11** and **uv**, then open the repository folder in a terminal:

```powershell
git clone https://github.com/Bhumi-codes/EDXSO-Assignment-2-Scholarship-Intelligence-Crawler.git
cd EDXSO-Assignment-2-Scholarship-Intelligence-Crawler
uv sync --locked
uv run python -m scholarship_intelligence start --no-crawl
```

If the repository is already downloaded, run only the last two commands from the folder containing `pyproject.toml`.

Open **http://127.0.0.1:8501**. Keep the terminal running. Stop the application with `Ctrl+C`.

The dashboard initially displays **296 genuine saved scholarship records**. Their original crawl and verification dates are preserved. On a fresh checkout, the supporting evidence archive prepares in the background; **Crawl now** becomes available when preparation finishes. Loading these records is not a new live crawl, and an existing nonempty database is never replaced.

For installation without uv on Windows:

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python -m scholarship_intelligence start --no-crawl
```

On macOS/Linux, use `.venv/bin/python` in place of `.venv\Scripts\python`. Windows with Python 3.11 is the tested environment. Internet access is required for dependency installation and live crawling. Saved records can be viewed offline once dependencies are installed.

## Working demonstration guide

### 1. Start and browse

Run the Quick start command, then inspect the six dashboard metrics: **Total discovered, Verified, Review required, Active, Expired, and Average confidence**.

Search for a scholarship and open its details. Inspect available benefits, eligibility, documents, application dates, official source, application link, confidence explanation, source quotations, and change history. Missing or unsupported values remain unspecified or withheld. Awards and other excluded programme types are omitted from the main scholarship view.

### 2. Run a live crawl

Select a source in the sidebar and click **Crawl now**. For a small source demonstration, select **Foundation for Excellence**. Select **All configured sources** for the full workflow.

The background worker performs:

```text
Discover links -> Crawl official sources -> Prepare HTML/PDF
-> Extract fields -> Check evidence -> Calculate confidence
-> Store or flag for review -> Refresh dashboard
```

Progress and completion appear in the sidebar. Continue browsing saved records while the update runs. Website response times and source size determine live-crawl duration; a full run can take tens of minutes. The terminal remains open to serve the dashboard after **Crawl finished** appears.

### 3. Run again and inspect updates

Click **Crawl now** again. Every live run rechecks listing pages and known sources:

- ETag/Last-Modified validators allow conditional requests. A valid HTTP 304 reuses hash-checked saved bytes.
- When validators are unavailable, the page is downloaded and its hash compared.
- Unchanged content reuses extraction and independently validated evidence only while artifact hashes, configuration and processing rules still match.
- New or changed content is extracted and validated again. Accepted changes retain old/new values, timestamps and evidence; unresolved changes remain review proposals.
- Failed source access is recorded for re-verification rather than treated as proof of expiry.
- Date-based expiry, freshness and confidence are recalculated.

An unchanged result is a valid outcome. A live provider may not publish a revision between two demonstration runs.

### 4. Historical official-notice replay

Open a second terminal in the repository root, keeping the dashboard running:

```powershell
uv run python -m scholarship_intelligence replay-official-notices
```

This separate workflow fetches preserved official notices, validates deadline revisions, and evaluates historical application windows. It does not modify main-dashboard records.

The retained successful run on **15 October 2026 at 22:38 IST** produced:

- **NMMSS:** closing date changed from 30 September to 31 October 2026.
- **CSSS renewal, Sacred Heart College instructions:** closing date changed from 30 September to 31 October 2026.
- **Chevening India and DAAD HSK:** two historical ACTIVE-to-EXPIRED evaluations.
- **2 changed examples, 2 expired examples, no failures, target_met=true.**

See the [successful replay report](data/samples/official-replay-result.json) for quotations, official URLs, hashes, source locations and validation results. CSSS demonstrates an institutional instruction revision, not independently established national policy change. This is **historical official-notice replay**, not a new source change observed between production crawls today. The expiry examples advance the evaluation date across real published windows; source values are unchanged.

The scanned CSSS notice requires **Poppler `pdftoppm` on PATH and Windows English OCR**. Confirm Poppler is accessible with `pdftoppm -v`. These are optional replay prerequisites; the normal HTML/text-PDF workflow does not require them. Replay results are date-sensitive and may be incomplete if sources fail or the later example deadlines have expired. Failures are reported explicitly.

New replay output is stored in `data/runtime/official-notice-replay/latest.json`, with per-run reports, evidence and an isolated SQLite database. The retained sample JSON references the original local runtime artifacts; the JSON alone does not contain every referenced source file.

## Command-line workflows

Run all configured sources without starting the dashboard:

```powershell
uv run python -m scholarship_intelligence workflow-all
```

Run one complete source:

```powershell
uv run python -m scholarship_intelligence workflow --seed-id iit_delhi
```

Recover a stopped source workflow from saved crawl snapshots:

```powershell
uv run python -m scholarship_intelligence workflow --seed-id iit_delhi --resume
```

Resume is recovery from saved content, not a fresh source check. Omit `--resume` to fetch sources again.

Configured seeds in `config/seeds.json` are `ffe`, `iit_delhi`, `nsp`, `chevening_india`, and `hdfc_ecss`. They cover NGO/trust, university, government, international and corporate CSR sources. Relevant links and catalogue entries are discovered beyond the initial seed URLs. Source rules and crawl budgets constrain coverage; inaccessible or unlinked content is not guaranteed to be found.

**Automatic daily crawl** runs at **09:00 IST while the local dashboard server is running**, with a catch-up after a missed schedule. It cannot wake a powered-off computer. Overlapping dashboard jobs are blocked.

## Technology stack

- **Python 3.11:** orchestration, deterministic extraction, evidence verification and confidence calculation.
- **Requests:** live HTTP retrieval and conditional requests.
- **Scrapy / Parsel:** link policies, HTML selection and document preparation.
- **pypdf:** text-PDF extraction with physical page references.
- **Pydantic:** common scholarship schema and validation.
- **SQLite:** transactional records, source associations, evidence and change history.
- **Streamlit:** dashboard, search, filters, crawl controls and record inspection.
- **uv:** locked dependency installation; `requirements.txt` also supports pip.
- **unittest:** isolated regression tests.
- **Windows OCR and Poppler:** optional scanned-notice processing in the historical replay.

## Database layout

The live database is **`data/runtime/scholarships.sqlite3`**. Its schema is [scholarship_intelligence/schema.sql](scholarship_intelligence/schema.sql), containing eight relational tables:

- **`programmes`:** continuing programme identity, scholarship name and provider.
- **`scholarships`:** offering cycle, benefits, eligibility, dates, documents, selection, renewal, lifecycle, verification, confidence and verification details.
- **`sources`:** source URLs, source types, authenticity and retrieval outcomes.
- **`scholarship_sources`:** links scholarships to primary, guideline, announcement, application or discovery sources.
- **`source_snapshots`:** retrieval timestamps, content hashes, and raw/prepared file paths for preserved source versions.
- **`evidence`:** field-level quotations, block/page locations, validation results and current/historical associations.
- **`changes`:** accepted old/new values, detection timestamps, reasons and old/new evidence references.
- **`crawl_runs`:** execution triggers, start/finish times, statuses and summaries.

One programme can have multiple scholarship offerings. Offerings link to multiple sources through `scholarship_sources`; sources have versioned snapshots. Evidence links an offering field to a specific snapshot. Changes retain references to old and new evidence. JSON columns hold structured benefits, eligibility and review details.

SQLite foreign keys and transactions protect relationships and updates. The schema enforces the minimum confidence and verification metadata for accepted `VERIFIED` rows. Application lifecycle is separate from verification: supported facts can describe an expired scholarship.

Raw/prepared source files, bundles, extraction packets and reports live beside the database under `data/runtime/`. Genuine saved records and their supporting artifacts are distributed in `data/samples/saved-results.zip.part01` through `part03`, with checksums in `saved-results.manifest.json`. Keep all parts together; the application restores them automatically. Generated runtime files and virtual environments are excluded from version control.

## Verification and confidence

Important claims must trace to official source evidence. Checks cover source association, immutable source hashes, exact quoted substrings, real field paths, source block/PDF page locations, field context and completeness of relevant cited passages. An official URL alone does not verify every scholarship fact.

```text
Confidence = 0.30 x official-source authenticity
           + 0.50 x supported applicable fields
           + 0.20 x dated offering context
```

Component scores are evaluated on a 0-100 scale. `VERIFIED` requires **confidence >=95 and all critical gates to pass**. Unsupported core identity, benefits or eligibility, missing required routing/supporting material, unresolved conflicts, or invalid freshness prevent verification. Source retrieval older than 30 days fails the freshness requirement.

There is no artificial score floor or quota bonus. Missing facts are not invented. Scores are deterministic evidence-based assessments, not calibrated probabilities of real-world correctness. Lifecycle statuses include `ACTIVE`, `EXPIRING_SOON`, `EXPIRED`, `REVIEW_REQUIRED`, and `NO_LONGER_VERIFIABLE`; unknown dates do not imply an open application window.

## Results

The retained genuine live validation run completed on **15 October 2026**, starting with an empty database. Its saved dashboard results were:

- **296 scholarship records**, with official-source identity confirmed for all 296.
- **4 verified / scoring at least 95**, and **292 review required**.
- **119 active or expiring-soon**, and **9 expired**.
- **83.1 average confidence**, rounded from 83.0588.
- **Five source categories** and **zero model/API calls**.
- **157 automated regression tests passed** in the final incremental-workflow release check.

The separate historical replay subsequently passed with **2 changed and 2 expired examples**, with no failures and no changes to production rows.

These are retained check results, not a guarantee of current availability or future crawl counts. Source freshness and evaluation dates can change dashboard values. The Total discovered metric counts stored scholarship records matching current filters; Active includes expiring-soon records.

**Known shortfall:** only four records meet whole-record verification requirements. The assignment targets of ten records at >=95 and fifteen verified scholarships remain unmet. Official-source identity confirmation is a separate measure and is not presented as whole-record verification.

Other limits include scanned PDFs without normal-workflow OCR, complex conditional tables, shared programme guidance, missing source information, inaccessible websites, and eligibility retained as source prose rather than a universal applicant-decision engine.

## Tests

```powershell
uv run python -m unittest discover -s tests -v
```

Tests cover extraction, evidence matching, PDF page validation, confidence gates, conditional HTTP retrieval, cache invalidation, source failure handling, saved-result integrity, date preservation, duplicate prevention, change history, rollback and scheduler controls. Synthetic fixtures remain isolated from application data. Tests do not certify every external website's accuracy or future accessibility.

## Technical note

The **three-page technical note PDF** is included at [docs/technical-note.pdf](docs/technical-note.pdf). It covers architecture, technology choices, discovery, extraction, verification, confidence methodology, anti-hallucination safeguards and change detection. An [editable Markdown version](docs/technical-note.md) is also available.

## Project structure

```text
scholarship_intelligence/    Application code and SQLite schema
config/seeds.json           Source configuration and grouping rules
.streamlit/                 Dashboard configuration
.github/workflows/          Automated test workflow
data/samples/               Genuine saved data, logs and replay result
docs/                       Technical note and release checks
tests/                      Isolated regression tests
pyproject.toml              Dependencies and project metadata
uv.lock                     Locked dependency versions
requirements.txt            pip-compatible dependency list
```

Runtime reports include `live-source-workflows.json`, `live-progress.json`, `crawl-control.json` and `pilot-*.jsonl` under `data/runtime/`.

If port 8501 is occupied, use:

```powershell
uv run python -m scholarship_intelligence start --no-crawl --port 8502
```
