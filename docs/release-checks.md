# Release checks — 1.1.0

Validated on Windows with Python 3.11.15. Locked dependencies were installed in a fresh environment. All **157 automated tests passed**, including HTTP conditional retrieval, hash comparison, extraction reuse, tampering rejection, failed-source handling, bootstrap integrity and original-date preservation.

The saved dataset comes from a complete genuine live workflow started with an empty database on 15 October 2026. It contains 296 dashboard scholarships: 4 verified, 292 review required, 119 active and 9 expired at validation time, with average confidence 83.1. Counts change with evaluation date and later live checks. Official source identity was confirmed for all 296; this does not make every record verified. The assignment's ten records at 95 or above requirement remains unmet.

SQLite integrity and foreign-key checks passed. Full source packets were independently checked before building the saved view. Record-referenced original source bytes, prepared documents, extraction packets and validation receipts are included in the checksummed archive parts. Loading the bundle preserves source-check dates and never replaces a nonempty database.

The immediate saved view loaded 296 records in approximately 3.3 seconds in the validation environment. A dashboard AppTest reported no application exceptions, displayed all six metrics, and confirmed Crawl now is disabled during evidence restoration. AppTest used a socket-free event loop because the restricted test environment blocked Windows loopback socket creation; this accommodation is confined to the external smoke test, not application code. A fresh-folder end-to-end startup then displayed all 296 records in 9.4 seconds; evidence preparation completed after 67.5 seconds with no error, and Crawl now became enabled. Preparation is disk-dependent and runs only once.

The five-source live baseline is retained; this release's conditional-request paths were tested with deterministic HTTP fixtures rather than repeating another full network crawl. Real HTTP 304 support depends on each website. Servers without validators still require a download and hash comparison. Extraction and context verification are reused only when content and the processing fingerprint match.

The release excludes model/API clients, manual import commands, simulated application data, development environments and scratch files. Test fixtures remain isolated from application records. Scheduled crawling requires the local dashboard server to stay running.
