# Technical note

## Architecture and stack

Scholarship Intelligence is a local Python application using Requests for synchronous HTTP crawling, Scrapy source/link policies and HTML preparation, Pydantic for a common record schema, pypdf for text PDF preparation, SQLite for transactional persistence, and Streamlit for the interface. The same synchronous crawler runs from the terminal and dashboard worker. No language model, paid API or API key is used.

The pipeline starts from five provider/portal seeds across government, university, corporate CSR, international and NGO/trust sources. Each complete source run discovers opportunities, retrieves response bytes, groups programme-specific material, prepares documents, extracts facts, validates evidence, calculates confidence, stores results and refreshes the dashboard. Startup displays a checksummed view of genuine saved records immediately and restores the full evidence archive in the background, preserving original retrieval dates. Existing nonempty databases are not overwritten. A persisted run report supports recovery. Source failures are reported individually, so completed sources remain inspectable.

## Discovery and source classification

Seed configuration defines provider identity, authoritative hosts, relevant paths and programme grouping. Links discovered within those boundaries become crawl candidates. NSP catalogue filters and programme cards are enumerated. This discovers records beyond a list of manually entered individual scholarship URLs, although new website layouts can require additional source adapters.

The crawler obeys robots rules and request spacing, limits page depth/size, records failures and distinguishes observed coverage from an assertion of complete website coverage. Authoritative host configuration is an explicit trust input; it is not presented as automatic proof that any arbitrary internet host is authentic.

## Extraction and anti-hallucination

Saved source bytes receive content hashes and retrieval timestamps. HTML is prepared into located blocks, headings, lists, links and tables. Text PDFs preserve physical pages. Rules recognize relevant sections and literal field content: identity, benefits, eligibility, dates, application routing, documents, selection and renewal. Source-specific grouping prevents unrelated programme cards from being merged.

Claims carry evidence IDs and inspected snapshot identities. Missing or ambiguous facts remain unspecified or withheld. Filenames, generic portal dates and recipient years do not establish a current scholarship cycle. Conditional monetary rates are not simplified into a fabricated fixed grant. Eligibility text remains source prose; comprehensive machine-readable applicant eligibility evaluation is a limitation.

## Verification and confidence

Evidence validation checks hashes, bundle identity, real field paths, exact quoted substrings, source block/page locations, and application links. Independent verification checks whether the quotation supports the field in context and covers the expected relevant source passages. Reordered or duplicated quotations cannot substitute for missing text. A score is recomputed from pinned artifacts rather than trusted merely because a saved JSON file contains a high number.

The score weights official-source authenticity at 30%, supported applicable fields at 50%, and dated offering context at 20%. Verification requires at least 95 plus critical gates for supported core facts, routing, freshness and absence of unresolved conflicts/incomplete required material. Retrieval older than 30 days fails the freshness requirement. An official source alone is insufficient for whole-record verification. Scores are evidence-based rule outputs, not calibrated probabilities of real-world correctness.

Application lifecycle is evaluated separately from factual confidence. Supported dates can show active, expiring or expired windows; unknown dates keep availability unresolved. The application cycle is retained because it identifies the advertised round, whereas lifecycle describes the evaluation date.

## Persistence, updates and automation

SQLite retains programme identity, scholarship facts, source associations, snapshots, evidence, crawl runs and change history. Writes use transactions and foreign keys. Identical imports reuse existing identities. Repeated live runs compare source values and preserve old/new evidence, distinguishing a changed source from revised extraction rules. Unresolved updates remain proposals and do not silently erase accepted facts.

Repeated live runs use saved ETag/Last-Modified validators where supported. Valid 304 responses reuse hash-checked bytes; other responses are downloaded and compared by hash. Unchanged records reuse independently validated results only after checking artifact pins and a fingerprint of the extraction/validation code and seed configuration. Missing or changed receipts trigger processing again. Date-dependent freshness, confidence and expiry are recalculated; unreachable sources require re-verification rather than automatic expiry. Each run retains its HTTP event log and progress/completion report.

The dashboard's manual button starts the shared live pipeline in a background worker. The daily scheduler runs at 09:00 IST while the server process is alive, catches up once after a missed schedule, and prevents overlapping dashboard jobs. It is a local scheduler, not an always-on hosted service.

## Demonstration and limits

The optional official-notice replay retrieves genuine preserved revisions into an isolated database. NMMSS and institutional CSSS renewal notices demonstrate deadline changes. Chevening/DAAD published windows demonstrate historical active-to-expired evaluation. This does not claim two observed production crawls of the historical pages. It is date-sensitive and reports partial failures. Scanned replay material requires Windows English OCR and Poppler.

The prior retained dataset had 296 scholarships, five source types and four records at least 95. The assignment's ten-high-confidence-record minimum is not achieved. The release includes this genuine automatically extracted dataset and its supporting artifacts for saved-results startup, without manual assessments. New live runs generate their own evidence and counts. Remaining limits include scanned source PDFs, complex conditional tables, shared guidance, missing offering context and inaccessible websites. Unsupported records remain review required.
