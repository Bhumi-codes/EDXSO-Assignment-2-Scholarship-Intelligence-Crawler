# Scholarship Intelligence - Technical Note
Version 1.1.0 | 15 October 2026

## Architecture and purpose

- Find scholarship opportunities for Indian students and turn official website information into records that can be checked.
- Flow: discover links -> fetch pages -> extract facts -> check evidence -> calculate confidence -> store/update -> dashboard.
- Startup shows genuine saved crawl results. Source-check dates stay unchanged. Full evidence files prepare in the background.
- Crawl now starts a live background update. Daily updates run at 09:00 IST while the local dashboard is running.

## Technology choices

- Python 3.11: runs the workflow and extraction rules. No model/API key is needed.
- Requests: downloads pages and checks for changes. Scrapy/Parsel: supports link rules and HTML selection.
- pypdf: reads text PDFs and keeps physical page numbers. Pydantic: checks the record structure.
- SQLite: stores records, sources, evidence and history. Streamlit: shows the dashboard and crawl controls.
- uv: installs locked dependencies. unittest: runs regression tests. Windows OCR handles the scanned notice in the separate replay.

## Discovery and extraction

- Five starting sources: FFE, IIT Delhi, NSP, Chevening India and HDFC ECSS. These cover trust, university, government, international and corporate CSR funding.
- The crawler follows relevant links and portal catalogue entries. It discovers individual opportunities beyond the starting URLs.
- Source rules group pages by scholarship. Awards and other excluded programme types stay out of the main dashboard.
- Python rules read headings, paragraphs, lists and tables in HTML, and page text in PDFs.
- Fields include name, provider, benefits, eligibility, application link, opening/closing dates, documents, selection and renewal.
- Original bytes, URLs, retrieval times and content hashes are saved. Each important quote points to its source block or PDF page.
- Coverage depends on accessible links and source rules. The crawler cannot guarantee every page on a website is found.

## Verification and prevention of invented facts

- Check that the source belongs to the configured official provider and is linked to the scholarship.
- Check source hashes, record/bundle identity, exact quotations, field paths and block/page locations.
- Check whether the quoted passage supports the field in context and includes the relevant supporting wording.
- Missing facts stay unspecified. Conflicting or unsupported facts are withheld or flagged for review.
- Do not infer a current cycle from a filename, an unrelated date or a recent download. Do not turn a conditional benefit into an invented fixed amount.

## Confidence-score formula

- Each component uses a 0-100 scale. The score is calculated by Python rules, not supplied by a model.
- Confidence = 0.30 x A + 0.50 x F + 0.20 x C
- A - official-source authenticity (30%): 100 when the ownership check passes; otherwise 50.
- F - field support (50%): 100 x supported applicable fields / total applicable fields. An explicitly checked not-applicable field counts as supported. With no applicable fields, F is 0.
- Fields confirmed as not specified in the inspected source are left out of that fraction. They do not earn support points. Missing core fields still block verification.
- C - dated offering context (20%): 100 only when the source is fresh and a valid scholarship-specific application window, or an allowed current dated-cycle context, is supported. Otherwise C is 0.
- A source is fresh only when the required retrievals are within 30 days and freshness checks pass. A recent download alone does not establish an open application window.

## Rules for the VERIFIED label

- Confidence must be at least 95 and every critical check must pass.
- Name, provider, official source, benefits and eligibility must be supported. The application route must be supported or checked as not applicable.
- Unresolved conflicts, missing required supporting material, or failed freshness/context checks prevent verification.
- If a critical check fails, the final confidence is capped at 94 and the record remains REVIEW_REQUIRED.
- No score is forced upward to meet a target. The score measures rule-based evidence support; it is not a probability that every real-world fact is correct.

## Database and change detection

- Eight SQLite tables: programmes, scholarships, sources, scholarship_sources, source_snapshots, evidence, changes and crawl_runs.
- Programme records hold identity. Scholarship records hold offering facts, dates, status, confidence and review details.
- Sources and snapshots preserve URLs and source versions. Evidence links a field to a quotation and location. Changes keep old/new values, detection times and evidence references.
- Every live run checks listing pages and known sources. Conditional HTTP requests reuse saved bytes when the server confirms no change; otherwise downloaded hashes are compared.
- Unchanged content reuses validated extraction only if artifacts, source configuration and processing rules still match. New or changed content is processed again.
- Accepted updates preserve history. Unresolved updates remain review proposals. Failed source access does not automatically mean expired.
- Lifecycle is separate from verification: ACTIVE, EXPIRING_SOON, EXPIRED, REVIEW_REQUIRED or NO_LONGER_VERIFIABLE. Unknown dates do not mean applications are open.

## Results

- Genuine saved live run, completed 15 October 2026: 296 scholarship records across five source categories.
- Official-source identity confirmed: 296. Whole-record VERIFIED / confidence >=95: 4. REVIEW_REQUIRED: 292.
- Active or expiring-soon: 119. Expired: 9. Average confidence: 83.1. Model/API calls: 0.
- Final incremental-workflow release check: 157 automated tests passed. Results can change with source updates and the evaluation date.
- Official-notice replay, 15 October 2026 at 22:38 IST: 2 changed deadlines, 2 expired examples, all reported evidence checks passed, no failures and no production rows changed.
- Changed examples: NMMSS and institutional CSSS renewal instructions, each from 30 September to 31 October 2026. Later deadlines were unexpired on the check date.
- Expiry examples: Chevening India and DAAD HSK, evaluated across real historical windows. This is historical official-notice replay, not a newly observed production change today.

## Current limits

- The ten records at >=95 and fifteen whole-record verifications required by the assignment are not achieved. Official-source confirmation is a separate measure.
- Scanned normal-workflow PDFs, complex tables, shared guidance, missing dates and blocked websites can leave fields unresolved.
- Eligibility remains source wording rather than a complete applicant-decision engine. The scheduler requires the local server to stay running.
