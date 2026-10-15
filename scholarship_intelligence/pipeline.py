"""Live discovery, deterministic extraction, evidence review and storage."""

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import re
import sqlite3
from uuid import uuid4

from scholarship_intelligence.bundles import create_bundle, load_bundle, document_checks
from scholarship_intelligence.confidence import score_candidate
from scholarship_intelligence.evidence_validation import validate_evidence
from scholarship_intelligence.models import ScholarshipExtraction
from scholarship_intelligence.preparation import prepare_snapshot
from scholarship_intelligence.snapshots import RawSnapshot
from scholarship_intelligence.scholarship_storage import save_reviewed_candidate
from scholarship_intelligence.semantic_assessment import (
    CHECK_DEFINITIONS, CheckResult, SemanticAssessment, candidate_fingerprint,
)
from scholarship_intelligence.settings import DATABASE_PATH, PROJECT_ROOT
from scholarship_intelligence.storage import connect_database, transaction


NSP_AVAILABILITY_URL = "https://scholarships.gov.in/All-Scholarships"


def nsp_application_context(record, content, retrieved_at):
    """Independent card-scoped check: programme title AND existing guideline link.

    Never treat adjacent schemes' dates or institute verification as the student
    deadline. This verifies availability only, not disputed eligibility facts.
    """
    from lxml import html
    from urllib.parse import urljoin
    def key(value):
        value = re.sub(r"\s*\(Merit Based Scheme\)\s*", "", value, flags=re.I)
        return re.sub(r"[^a-z0-9]", "", value.casefold())
    when = datetime.fromisoformat(retrieved_at)
    if when.tzinfo is None or when.utcoffset() is None:
        raise ValueError("Availability retrieval time must include timezone.")
    document = html.fromstring(content)
    sources = {entry["url"] for entry in record["sources"] if entry["url"].startswith("https://scholarships.gov.in/public/")}
    matches = []
    for heading in document.xpath("//h6"):
        title = " ".join(heading.text_content().split())
        if key(title) != key(record["name"]):
            continue
        card = heading.getparent()
        links = {urljoin(NSP_AVAILABILITY_URL, anchor.get("href", "")) for anchor in card.xpath(".//a[@href]")}
        matched_links = sorted(links & sources)
        if not matched_links or record["provider_name"] != "University Grants Commission":
            continue
        dates, evidence = {}, []
        for span in card.xpath(".//span"):
            quote = " ".join(span.text_content().split())
            match = re.fullmatch(r"(Scheme Open from|Student Application Open till)\s*:\s*(\d{2}-\d{2}-\d{4})", quote, re.I)
            if not match:
                continue
            field = "opening_date" if match[1].casefold() == "scheme open from" else "closing_date"
            if field in dates:
                raise ValueError("Conflicting or duplicate student date labels in scheme card.")
            dates[field] = datetime.strptime(match[2], "%d-%m-%Y").date().isoformat()
            evidence.append({"field": field, "quote": quote, "source_xpath": span.getroottree().getpath(span)})
        if set(dates) != {"opening_date", "closing_date"} or dates["opening_date"] > dates["closing_date"]:
            raise ValueError("Complete ordered student dates absent in the matching scheme card.")
        from scholarship_intelligence.confidence import cycle_years
        if record.get("cycle") and dates["closing_date"][:4] not in cycle_years(record["cycle"]):
            raise ValueError("Known stored offering cycle differs from this listing; offering identity review required.")
        matches.append({"version": "programme-availability-v1", "source_url": NSP_AVAILABILITY_URL,
                        "source_sha256": sha256(content).hexdigest(), "retrieved_at": retrieved_at,
                        "scholarship_id": record["scholarship_id"], "programme_title": title,
                        "matched_guideline_urls": matched_links, **dates, "evidence": evidence,
                        "stage": "student_application", "availability_support": "verified",
                        "scope": "Official scheme card student window; eligibility conflicts and whole-record verification remain separate."})
    if len(matches) != 1:
        raise ValueError("A unique programme title and existing official guideline/FAQ association is required.")
    return matches[0]


def refresh_availability(*, apply=False, database_path=DATABASE_PATH, project_root=PROJECT_ROOT,
                         saved_content=None, retrieved_at=None):
    """Fetch one configured official listing and associate only supported records.

    saved_content/retrieved_at are for offline source replay and regression tests.
    """
    from scholarship_intelligence.dashboard import load_records
    from scholarship_intelligence.snapshots import save_raw_snapshot
    root = Path(project_root).resolve()
    records = load_records(database_path, project_root=root, include_source_assessments=False)
    targets = [record for record in records if record["provider_name"] == "University Grants Commission"
               and any(entry["url"].startswith("https://scholarships.gov.in/public/") for entry in record["sources"])]
    if not targets:
        return {"operation": "availability_refresh", "records_checked": 0, "database_rows_changed": 0, "api_calls_made": 0}
    live = saved_content is None
    if live:
        import requests
        response = requests.get(NSP_AVAILABILITY_URL, timeout=30, allow_redirects=False,
                                headers={"User-Agent": "ScholarshipIntelligenceCrawler/0.1"})
        if response.status_code != 200 or "text/html" not in response.headers.get("Content-Type", ""):
            raise ValueError("Official availability source did not return HTML 200.")
        saved_content, retrieved_at = response.content, datetime.now(timezone.utc).isoformat()
    snapshot = save_raw_snapshot(NSP_AVAILABILITY_URL, saved_content, datetime.fromisoformat(retrieved_at),
                                 "text/html", project_root=root)
    prepared = prepare_snapshot(snapshot, 200, project_root=root)
    bundle = create_bundle([prepared.prepared_file_path], "NSP official student application windows", project_root=root)
    receipt = asdict(snapshot)
    receipt.pop("reused", None)
    receipt_bytes = (json.dumps(receipt, sort_keys=True) + "\n").encode()
    receipt_path = root / "data/runtime/diagnostics" / ("availability-" + sha256(receipt_bytes).hexdigest() + ".json")
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    if receipt_path.exists() and receipt_path.read_bytes() != receipt_bytes:
        raise ValueError("Availability retrieval receipt changed.")
    if not receipt_path.exists():
        receipt_path.write_bytes(receipt_bytes)
    results, updates = [], []
    for record in targets:
        try:
            context = nsp_application_context(record, saved_content, snapshot.retrieved_at)
        except ValueError as error:
            results.append({"scholarship_id": record["scholarship_id"], "stage": "review_required", "reason": str(error)})
            continue
        packet = {**context, "bundle_id": bundle.bundle_id, "receipt_path": receipt_path.relative_to(root).as_posix(),
                  "receipt_sha256": sha256(receipt_bytes).hexdigest()}
        details = dict(record["details"])
        if details.get("application_context") != packet:
            details["application_context"] = packet
            updates.append((json.dumps(details, ensure_ascii=False), record["scholarship_id"], record["verification_details_json"]))
        results.append(context)
    if apply:
        with connect_database(database_path) as connection, transaction(connection):
            for value, identity, baseline in updates:
                if connection.execute("UPDATE scholarships SET verification_details_json=? WHERE scholarship_id=? AND verification_details_json=?",
                                      (value, identity, baseline)).rowcount != 1:
                    raise ValueError("Stored review changed during availability refresh; rolled back.")
    return {"operation": "availability_refresh", "records_checked": len(targets), "results": results,
            "live_crawl_performed": live, "http_requests_made": int(live), "api_calls_made": 0,
            "database_rows_changed": len(updates) if apply else 0, "dry_run": not apply}


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key in a saved artifact.")
        result[key] = value
    return result


def _reject_constant(value):
    raise ValueError("Nonfinite JSON numbers are not permitted.")


def _decode(content):
    result = json.loads(content, object_pairs_hook=_unique_object, parse_constant=_reject_constant)
    if not isinstance(result, dict):
        raise ValueError("Saved artifact must contain a JSON object.")
    return result


def _inside(root, supplied, directory):
    path = Path(supplied)
    if path.is_absolute():
        relocation = root / "config/project.json"
        if relocation.is_file():
            for original in _decode(relocation.read_bytes()).get("legacy_project_roots", []):
                old = Path(original).resolve()
                if path.resolve().is_relative_to(old):
                    path = root / path.resolve().relative_to(old)
                    break
    path = (root / path).resolve()
    if not path.is_relative_to((root / directory).resolve()):
        raise ValueError("Input file is outside its project storage directory.")
    return path


def _select(directory, supplied, pattern, root):
    if supplied is not None:
        return _inside(root, supplied, "data/runtime/extractions")
    matches = list(directory.glob(pattern))
    if len(matches) != 1:
        raise ValueError(f"Select a file explicitly: {pattern} has {len(matches)} matches.")
    return _inside(root, matches[0], "data/runtime/extractions")


@dataclass(frozen=True)
class PreparedReviewImport:
    candidate: ScholarshipExtraction
    assessment: SemanticAssessment
    bundle_path: Path
    generation_config_hash: str
    retrieval_times: dict
    source_types: dict
    artifact_paths: dict
    project_root: Path
    extraction_method: str = "python_rules"

    def summary(self):
        score = score_candidate(self.candidate, self.assessment)
        return {
            "bundle_id": self.candidate.bundle_id,
            "documents": len(self.retrieval_times),
            "extraction_method": self.extraction_method,
            "verification_status": score.verification_status,
            "confidence_score": score.to_dict()["confidence_score"],
            "withheld_fields": list(self.assessment.withheld_fields),
            "blockers": list(score.blockers),
            "api_calls_made": 0,
        }


def prepare_review_import(
    review_directory, pilot_file, *, assessment_file=None,
    confidence_file=None, project_root=PROJECT_ROOT,
):
    """Validate one package, its original response and crawl provenance."""
    root = Path(project_root).resolve()
    directory = _inside(root, review_directory, "data/runtime/extractions")
    candidate_path = _inside(root, directory / "candidate.json", "data/runtime/extractions")
    review_path = _inside(root, directory / "review.json", "data/runtime/extractions")
    assessment_path = _select(directory, assessment_file, "semantic-assessment-*.json", root)
    confidence_path = _select(directory, confidence_file, "confidence-*.json", root)
    candidate_bytes = candidate_path.read_bytes()
    candidate = ScholarshipExtraction.model_validate(_decode(candidate_bytes))
    assessment_bytes = assessment_path.read_bytes()
    assessment_data = _decode(assessment_bytes)
    if assessment_data.get("candidate_sha256") != sha256(candidate_bytes).hexdigest():
        raise ValueError("Assessment is not bound to these candidate bytes.")
    assessment = SemanticAssessment.model_validate(assessment_data["assessment"])
    calculated = score_candidate(candidate, assessment)
    confidence = _decode(confidence_path.read_bytes())
    if confidence.get("candidate_sha256") != sha256(candidate_bytes).hexdigest():
        raise ValueError("Confidence report is not bound to this candidate.")
    if confidence.get("assessment_sha256") != sha256(assessment_bytes).hexdigest():
        raise ValueError("Confidence report is not bound to this assessment.")
    if any(confidence.get(key) != value for key, value in calculated.to_dict().items()):
        raise ValueError("Saved confidence differs from the current scoring policy.")

    review = _decode(review_path.read_bytes())
    method = review.get("extraction_method")
    if method == "python_rules":
        if any(key in review for key in ("original_response_path", "original_request_path", "original_generation_config_hash")):
            raise ValueError("Python review must not claim API provenance.")
        origin_key = "python_provenance"
        origin_path = _inside(root, review[origin_key + "_path"], "data/runtime/extractions")
        if sha256(origin_path.read_bytes()).hexdigest() != review[origin_key + "_sha256"]:
            raise ValueError("Python provenance changed after review.")
        original_candidate, origin = _python_draft(origin_path.parent, root)
        if origin["method"] != method:
            raise ValueError("Review extraction method differs from its pinned provenance.")
        if original_candidate.bundle_id != candidate.bundle_id or original_candidate.snapshot_ids != candidate.snapshot_ids:
            raise ValueError("Python review changed bundle/snapshot identity.")
        if candidate != original_candidate and review.get("candidate_correction_selected") is not True:
            raise ValueError("Python correction must be explicitly recorded.")
        generation_hash = review[origin_key + "_sha256"]  # Legacy parameter carries extraction identity.
        origin_artifacts = {origin_key: origin_path, "original_" + method + "_candidate": origin_path.parent / "candidate.json"}
    else:
        raise ValueError("Unknown extraction provenance method.")

    bundle_path = root / "data/runtime/bundles" / f"{candidate.bundle_id}.json"
    report = validate_evidence(candidate, bundle_path, project_root=root)
    if not report.evidence_checks_passed:
        raise ValueError("Reviewed candidate failed source evidence validation.")
    bundle = load_bundle(bundle_path, project_root=root)
    pilot_path = _inside(root, pilot_file, "data/runtime")
    events = [_decode(line) for line in pilot_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    retrieval_times, source_types = {}, {}
    for ref in bundle.documents:
        matches = []
        for event in events:
            if event.get("event") != "fetched" or event.get("http_status") != 200:
                continue
            snapshot = event.get("snapshot")
            if not isinstance(snapshot, dict):
                raise ValueError("Fetched crawl event lacks snapshot metadata.")
            if all(snapshot.get(key) == getattr(ref, key) for key in ("source_url", "content_hash", "raw_file_path")):
                when = datetime.fromisoformat(snapshot["retrieved_at"])
                if when.tzinfo is None or when.utcoffset() is None:
                    raise ValueError("Crawl retrieval timestamp lacks a timezone.")
                matches.append((when, event["source_category"]))
        if not matches:
            raise ValueError("No successful crawl event supports a bundled snapshot.")
        if len({category for _, category in matches}) != 1:
            raise ValueError("Crawl events disagree on source category.")
        when, category = max(matches, key=lambda entry: entry[0])
        retrieval_times[ref.snapshot_id] = when
        if ref.source_url in source_types and source_types[ref.source_url] != category:
            raise ValueError("Bundled source classifications conflict.")
        source_types[ref.source_url] = category
    artifacts = {
        name: path.relative_to(root).as_posix() for name, path in {
            "candidate": candidate_path, "assessment": assessment_path,
            "confidence": confidence_path, "review": review_path,
            **origin_artifacts,
        }.items()
    }
    return PreparedReviewImport(
        candidate, assessment, bundle_path, generation_hash,
        retrieval_times, source_types, artifacts, root, method,
    )


def persist_review_import(prepared: PreparedReviewImport, *, database_path=DATABASE_PATH):
    """Save a prepared package atomically; exact repeats reuse its record."""
    database = Path(database_path).resolve()
    if not database.is_file():
        raise FileNotFoundError("Initialize the database before importing.")
    score = score_candidate(prepared.candidate, prepared.assessment)
    with connect_database(database) as connection:
        with transaction(connection):
            run_id = connection.execute(
                "INSERT INTO crawl_runs(trigger, started_at, summary_json) VALUES (?, ?, ?)",
                ("command", datetime.now(timezone.utc).isoformat(), json.dumps({
                    "operation": "reviewed_import", "extraction_method": prepared.extraction_method, "live_crawl_performed": False,
                    "api_calls_made": 0,
                })),
            ).lastrowid
            stored = save_reviewed_candidate(
                connection, prepared.candidate, prepared.assessment, prepared.bundle_path,
                run_id=run_id, generation_config_hash=prepared.generation_config_hash,
                retrieval_times=prepared.retrieval_times, source_types=prepared.source_types,
                artifact_paths=prepared.artifact_paths, project_root=prepared.project_root,
            )
            row = connection.execute("SELECT verification_status, confidence_score, eligibility_json FROM scholarships WHERE scholarship_id = ?", (stored.scholarship_id,)).fetchone()
            expected_score = None if score.confidence_score is None else float(score.confidence_score)
            if row["verification_status"] != score.verification_status or row["confidence_score"] != expected_score:
                raise ValueError("Stored verification differs from current assessment.")
            if "eligibility" in prepared.assessment.withheld_fields and row["eligibility_json"] is not None:
                raise ValueError("Stored accepted eligibility contains a withheld claim.")
            if connection.execute("PRAGMA foreign_key_check").fetchall():
                raise ValueError("Database has foreign-key violations.")
            outcome = {
                **asdict(stored), "run_id": run_id, "extraction_method": prepared.extraction_method,
                "verification_status": score.verification_status,
                "confidence_score": expected_score, "api_calls_made": 0,
            }
            connection.execute(
                "UPDATE crawl_runs SET status = 'COMPLETED', finished_at = ?, summary_json = ? WHERE run_id = ?",
                (datetime.now(timezone.utc).isoformat(), json.dumps({
                    **outcome, "operation": "reviewed_import", "live_crawl_performed": False,
                }), run_id),
            )
    return outcome


def inspect_scholarship(scholarship_id: int, *, database_path=DATABASE_PATH, project_root=PROJECT_ROOT):
    """Read a stored result without creating a database or changing rows."""
    if type(scholarship_id) is not int or scholarship_id < 1:
        raise ValueError("Scholarship ID must be a positive integer.")
    database = Path(database_path).resolve()
    connection = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        row = connection.execute(
            "SELECT s.*, p.name, p.provider_name FROM scholarships s "
            "JOIN programmes p ON p.programme_id=s.programme_id WHERE scholarship_id=?",
            (scholarship_id,),
        ).fetchone()
        if row is None:
            raise LookupError("Scholarship ID does not exist.")
        details = _decode(row["verification_details_json"])
        evidence = connection.execute(
            "SELECT count(*), count(DISTINCT json_extract(check_result_json,'$.local_evidence_id')) "
            "FROM evidence WHERE scholarship_id=?", (scholarship_id,),
        ).fetchone()
        sources = connection.execute(
            "SELECT DISTINCT src.url FROM sources src JOIN scholarship_sources ss "
            "ON ss.source_id=src.source_id WHERE ss.scholarship_id=? ORDER BY src.url",
            (scholarship_id,),
        ).fetchall()
        from scholarship_intelligence.dashboard import apply_source_assessment, apply_availability_context
        display = {**dict(row), "details": details, "sources": [{"url": source[0]} for source in sources]}
        apply_source_assessment(display, project_root=project_root)
        apply_availability_context(display, project_root=project_root)
        return {
            "availability_verified": display.get("availability_verified", False),
            "application_context": display.get("application_context"),
            "application_mode": display.get("application_mode"),
            "evidence_confidence_score": display.get("source_score", {}).get("confidence_score"),
            "evidence_confidence_policy": display.get("source_score", {}).get("policy_version"),
            "application_status": display["lifecycle_status"],
            "opening_date": display["opening_date"], "closing_date": display["closing_date"],
            "source_assessment_error": display.get("source_assessment_error"),
            "scholarship_id": row["scholarship_id"], "programme_id": row["programme_id"],
            "name": row["name"], "provider": row["provider_name"],
            "verification_status": display["verification_status"], "lifecycle_status": display["lifecycle_status"],
            "historical_verification_status": row["verification_status"],
            "confidence_score": display["confidence_score"],
            "historical_verification_confidence": row["confidence_score"], "last_verified_at": display["last_verified_at"],
            "historical_last_verified_at": row["last_verified_at"],
            "accepted_eligibility_present": row["eligibility_json"] is not None,
            "draft_eligibility_present": "eligibility" in details.get("draft_claims", {}),
            "field_evidence_rows": evidence[0], "distinct_evidence_entries": evidence[1],
            "extraction_provenance": details.get("extraction_provenance", {"method": "unknown"}),
            "sources": [source[0] for source in sources],
            "blockers": [item for item in display.get("source_score", {}).get("blockers", details.get("blockers", []))
                         if not (display.get("availability_verified") and item.startswith("offering_cycle_context:"))],
            "historical_review_blockers": details.get("blockers", []),
            "source_scope": display.get("source_score", {}).get("scope"),
            "source_freshness": display.get("source_score", {}).get("source_freshness"),
            "source_field_assessments": display.get("source_score", {}).get("field_assessments", {}),
            "eligibility_decisions_supported": False,
            "api_calls_made": 0,
        }
    finally:
        connection.close()


def prepare_crawl_bundle(
    pilot_file, source_urls, offering_hint, cycle_hint=None, *,
    project_root=PROJECT_ROOT, source_hashes=None, allow_support_failures=False,
):
    """Prepare explicitly selected saved HTML/PDF sources for one offering.

    Select the most recent successful fetch of each exact URL in the supplied
    crawl report. The caller groups pages by offering; unrelated programmes
    must be separate bundles. Validate every selected raw file before writing
    prepared content. No crawl, API request or database write is performed.
    """
    root = Path(project_root).resolve()
    if isinstance(source_urls, (str, bytes)):
        raise ValueError("Supply a list of source URLs, not one string.")
    urls = list(source_urls)
    if not urls or any(not isinstance(url, str) or not url.strip() for url in urls):
        raise ValueError("Select at least one nonempty source URL.")
    if len(urls) != len(set(urls)):
        raise ValueError("Select each source URL only once.")
    if not isinstance(offering_hint, str) or not offering_hint.strip():
        raise ValueError("An offering hint is required.")
    if cycle_hint is not None and (
        not isinstance(cycle_hint, str) or not cycle_hint.strip()
    ):
        raise ValueError("Cycle hint must be nonempty or None.")
    pilot_path = _inside(root, pilot_file, "data/runtime")
    selected = {url: [] for url in urls}
    for line in pilot_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        event = _decode(line)
        if event.get("event") != "fetched" or event.get("http_status") != 200:
            continue
        metadata = event.get("snapshot")
        if not isinstance(metadata, dict):
            raise ValueError("Fetched crawl event lacks snapshot metadata.")
        url = metadata.get("source_url")
        if not isinstance(url, str) or url not in selected:
            continue
        if source_hashes and metadata.get("content_hash") != source_hashes.get(url):
            continue
        snapshot = RawSnapshot(**{
            name: metadata[name] for name in RawSnapshot.__dataclass_fields__
        })
        when = datetime.fromisoformat(snapshot.retrieved_at)
        if when.tzinfo is None or when.utcoffset() is None:
            raise ValueError("Crawl retrieval timestamp lacks a timezone.")
        selected[url].append((when, snapshot))

    snapshots = []
    for url, matches in selected.items():
        if not matches:
            raise ValueError(f"No successful crawl event for selected URL: {url}")
        latest_time = max(when for when, _ in matches)
        latest = [snapshot for when, snapshot in matches if when == latest_time]
        identities = {
            (snapshot.content_hash, snapshot.raw_file_path, snapshot.content_type)
            for snapshot in latest
        }
        if len(identities) != 1:
            raise ValueError("Conflicting snapshots share the latest retrieval time.")
        snapshot = latest[0]
        raw_path = _inside(root, snapshot.raw_file_path, "data/runtime/snapshots/raw")
        content = raw_path.read_bytes()
        if sha256(content).hexdigest() != snapshot.content_hash:
            raise ValueError("Raw snapshot failed its integrity check.")
        content_type = (snapshot.content_type or "").lower()
        is_pdf = content.lstrip().startswith(b"%PDF-") or "application/pdf" in content_type
        if is_pdf and not content.lstrip().startswith(b"%PDF-"):
            raise ValueError("Selected PDF response lacks a PDF header.")
        if not is_pdf and content_type and not any(
            kind in content_type for kind in ("text/html", "application/xhtml+xml")
        ):
            raise ValueError("Selected response does not declare HTML content.")
        snapshots.append(snapshot)

    prepared, failures = [], []
    for index, snapshot in enumerate(snapshots):
        try:
            prepared.append(prepare_snapshot(snapshot, 200, project_root=root))
        except (ValueError, OSError) as error:
            if not allow_support_failures or index == 0:
                raise
            failures.append({"url": snapshot.source_url, "reason": "preparation_failed", "error": str(error)})
    bundle = create_bundle(
        [document.prepared_file_path for document in prepared],
        offering_hint, cycle_hint, project_root=root,
    )
    return {
        "stage": "bundle_ready",
        "bundle_id": bundle.bundle_id,
        "bundle_file_path": bundle.bundle_file_path,
        "documents": len(bundle.documents),
        "support_failures": failures,
        "prepared_file_paths": [document.prepared_file_path for document in prepared],
        "source_urls": urls,
        "pilot_file": pilot_path.relative_to(root).as_posix(),
        "live_crawl_performed": False,
        "api_calls_made": 0,
        "database_rows_changed": 0,
    }




def crawl_coverage(events, seed_id=None):
    """Report discovered coverage, without claiming the whole website is complete."""
    from urllib.parse import urlsplit
    rows = [row for row in events if seed_id is None or row.get("seed_id") == seed_id]
    fetched = {row.get("request_key", row["snapshot"]["source_url"]): row for row in rows
               if row.get("event") == "fetched" and isinstance(row.get("snapshot"), dict)}
    failures = {row["url"] for row in rows if row.get("event") in {"request_failed", "snapshot_failed"} and isinstance(row.get("url"), str)} - set(fetched)
    discoveries = [row for row in rows if row.get("event") in {"discovery", "redirect"}]
    skipped = sorted({row.get("scheduled_url") or row.get("url") for row in discoveries
                      if row.get("decision") == "page_limit" and isinstance(row.get("scheduled_url") or row.get("url"), str)} - set(fetched) - failures)
    detail = lambda url: bool(re.fullmatch(r"/scholarships-awards/[0-9]+/?", urlsplit(url).path))
    detail_urls = {url for url in fetched if detail(url)} | {
        row.get("scheduled_url") or row.get("url") for row in discoveries
        if isinstance(row.get("scheduled_url") or row.get("url"), str)
        and detail(row.get("scheduled_url") or row.get("url"))}
    return {"fetched_pages": len(fetched),
            "successful_pages": sum(row.get("http_status") == 200 for row in fetched.values()),
            "failed_pages": sum(row.get("http_status") != 200 for row in fetched.values()) + len(failures),
            "request_or_snapshot_failures": sorted(failures),
            "discovered_detail_urls": len(detail_urls),
            "successful_detail_pages": sum(detail(url) and row.get("http_status") == 200 for url, row in fetched.items()),
            "budget_skipped_count": len(skipped), "budget_skipped_urls": skipped,
            "status": "budget_limited" if skipped else "no_budget_backlog",
            "scope": "Observed crawl links only; this does not establish complete website coverage or current application availability."}


def crawl_sources(*, seed_id=None, max_pages=1000, continue_from=None, project_root=PROJECT_ROOT):
    """One synchronous live crawl: scoped frontier, robots and immutable bytes.

    No child process or Windows event-loop dependency; the same crawler runs
    inside a terminal or the desktop sandbox. Every actual HTTP fetch is audited.
    """
    from collections import deque
    from urllib.parse import urlsplit, urljoin, urldefrag
    from urllib.robotparser import RobotFileParser
    import requests
    import time
    from scholarship_intelligence.snapshots import save_raw_snapshot
    from scholarship_intelligence.spider import ScholarshipSpider
    from lxml import html
    root = Path(project_root).resolve()
    seeds = _decode((root/'config/seeds.json').read_bytes())['seeds']
    seeds = [seed for seed in seeds if seed_id is None or seed['seed_id']==seed_id]
    if not seeds or not 1 <= max_pages <= 1000:
        raise ValueError('Unknown seed or invalid page budget.')
    runtime=root/'data/runtime';runtime.mkdir(parents=True,exist_ok=True)
    lock=runtime/'crawl.lock'
    import os
    with lock.open('x') as stream:stream.write(json.dumps({'pid':os.getpid(),'seed_id':seed_id,'started_at':datetime.now(timezone.utc).isoformat()}))
    report=runtime/('pilot-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-'+uuid4().hex[:8]+'.jsonl')
    count=0
    from scholarship_intelligence.http_cache import ResponseCache
    cache = ResponseCache(root)
    try:
        prior=[]
        if continue_from:
            prior=[_decode(line) for line in _inside(root,continue_from,'data/runtime').read_text(encoding='utf-8').splitlines() if line.strip()]
        with report.open('x',encoding='utf-8') as output:
            def emit(event):
                output.write(json.dumps(event,ensure_ascii=False)+'\n');output.flush()
                if event.get('event') == 'fetched':
                    from scholarship_intelligence.progress import report_progress
                    report_progress(root, event.get('seed_id'), 'Checking live pages', url=event['snapshot']['source_url'])
            for event in prior:emit(event)
            for seed in seeds:
                queue=deque([] if prior else [(seed['start_url'],None,0,None)]+[(url,seed['start_url'],1,None) for url in seed.get('support_urls',[])+seed.get('context_urls',[])])
                if not prior:
                    queue.extend((url, seed['start_url'], 1, None) for url in cache.known(seed['seed_id']))
                if prior:
                    queue.extend((event.get('scheduled_url') or event['url'],event.get('parent_url'),event.get('navigation_depth',1),event.get('form_data')) for event in prior if event.get('decision')=='page_limit' and event.get('seed_id')==seed['seed_id'])
                seen={event['snapshot']['source_url'] for event in prior if event.get('event')=='fetched' and event.get('seed_id')==seed['seed_id']}
                robots={};last={};scheduled=0
                session=requests.Session();session.headers['User-Agent']='ScholarshipIntelligenceCrawler/0.1'
                while queue:
                    url,parent,depth,form=queue.popleft()
                    if urlsplit(url).scheme not in {'http','https'}:
                        continue
                    url=requests.Request('GET',urldefrag(url)[0]).prepare().url
                    method='POST' if form else 'GET'
                    request_key=url+'::'+json.dumps(form,sort_keys=True) if form else url
                    host=urlsplit(url).hostname
                    reason='scheduled'
                    if host not in seed['allowed_hosts']:reason='external_host'
                    elif request_key in seen:reason='already_scheduled'
                    elif depth>seed.get('max_depth',6):reason='depth_limit'
                    elif scheduled>=max_pages:reason='page_limit'
                    emit({'event':'discovery','seed_id':seed['seed_id'],'parent_url':parent,'url':url,'scheduled_url':url if reason=='scheduled' else None,'navigation_depth':depth,'decision':reason,'request_key':request_key,'request_method':method,'form_data':form})
                    if reason!='scheduled':continue
                    seen.add(request_key);scheduled+=1
                    if host not in robots:
                        policy=RobotFileParser();robots_url=urlsplit(url).scheme+'://'+urlsplit(url).netloc+'/robots.txt'
                        try:
                            response=session.get(robots_url,timeout=25,allow_redirects=False)
                            policy.parse(response.text.splitlines() if response.status_code==200 else [] )
                            if response.status_code in (401,403) or response.status_code>=500:policy.disallow_all=True
                            emit({'event':'robots','seed_id':seed['seed_id'],'url':robots_url,'http_status':response.status_code})
                        except requests.RequestException as error:
                            policy.disallow_all=True;emit({'event':'robots_error','seed_id':seed['seed_id'],'url':robots_url,'error_type':type(error).__name__})
                        robots[host]=policy
                    if not robots[host].can_fetch(session.headers['User-Agent'],url):
                        cache.failure(request_key,url,seed['seed_id'],'RobotsDenied');emit({'event':'request_failed','seed_id':seed['seed_id'],'url':url,'parent_url':parent,'error_type':'RobotsDenied'});continue
                    time.sleep(max(0,seed.get('download_delay',1.0)-(time.monotonic()-last.get(host,0))))
                    try:
                        response, snapshot, network_status, cache_state = cache.fetch(session, method, url, form, request_key, seed['seed_id'])
                        last[host]=time.monotonic();count+=1
                        role='document' if response.content.lstrip().startswith(b'%PDF-') else 'seed' if url==seed['start_url'] else 'guideline' if re.search('eligibility|faq|guidance',url,re.I) else 'candidate_detail'
                        emit({'event':'fetched','seed_id':seed['seed_id'],'source_category':seed.get('source_category','university'),'parent_url':parent,'http_status':response.status_code,'page_role':role,'navigation_depth':depth,'snapshot':asdict(snapshot),'request_key':request_key,'request_method':method,'form_data':form,'network_status':network_status,'cache_state':cache_state,'response_validators':cache.entries.get(request_key,{}).get('headers',{})})
                        if response.status_code in (301,302,303,307,308):
                            destination=urljoin(url,response.headers.get('Location',''))
                            emit({'event':'redirect','seed_id':seed['seed_id'],'parent_url':url,'url':destination,'decision':'scheduled'})
                            queue.appendleft((destination,parent,depth,None));continue
                        if response.status_code!=200 or role=='document':continue
                        page=html.fromstring(response.content)
                        # Enumerate only the site's read-only scholarship catalogue
                        # filters, using real action URLs and actual option values.
                        if seed.get('extraction_profile')=='nsp_cards_v1' and url==seed['start_url']:
                            for search in page.xpath('//form[@method="post"]'):
                                action=urljoin(url,search.get('action',''))
                                if urlsplit(action).path.strip('/') not in {'centralsOrsponsoredOrstate','allschemesdepartment','allschemesdomicile'}:
                                    continue
                                hidden={item.get('name'):item.get('value','') for item in search.xpath('.//input[@type="hidden"]') if item.get('name')}
                                for select in search.xpath('.//select[@name]'):
                                    for option in select.xpath('.//option[@value]'):
                                        value=option.get('value','')
                                        if value and value not in {'0','-1'}:
                                            queue.append((action,url,1,{**hidden,select.get('name'):value}))
                        if len(page.text_content().strip())<100 and page.xpath('//script[@type="module"]'):
                            emit({'event':'source_incomplete','seed_id':seed['seed_id'],'url':url,'reason':'JavaScript shell requires a supported rendering adapter; HTTP 200 is not complete source content.'})
                        for anchor in page.xpath('//a[@href]'):
                            href=anchor.get('href','');label=' '.join(anchor.text_content().split())
                            if not href or href.startswith(('#','mailto:','tel:','javascript:')) or href=='null' or 'not_available.pdf' in href:continue
                            target=requests.Request('GET',urljoin(url,urldefrag(href)[0])).prepare().url
                            patterns=seed.get('crawl_patterns')
                            relevant=any(re.fullmatch(pattern,urlsplit(target).path,re.I) for pattern in patterns) if patterns else ScholarshipSpider.link_priority(seed,target,label)>0
                            if relevant and urlsplit(target).hostname in seed['allowed_hosts']:
                                emit({'event':'discovery','seed_id':seed['seed_id'],'parent_url':url,'url':target,'scheduled_url':target,'link_text':label,'decision':'discovered','navigation_depth':depth+1})
                                queue.append((target,url,depth+1,None))
                    except (requests.RequestException,ValueError) as error:
                        cache.failure(request_key, url, seed['seed_id'], type(error).__name__)
                        emit({'event':'request_failed','seed_id':seed['seed_id'],'url':url,'parent_url':parent,'error_type':type(error).__name__,'error':str(error)})
                session.close()
        events=[_decode(line) for line in report.read_text(encoding='utf-8').splitlines() if line.strip()]
        return {'stage':'crawl_finished','pilot_file':report.relative_to(root).as_posix(),'live_crawl_performed':bool(count),'http_requests_made':count,'api_calls_made':0,'scholarship_rows_changed':0,'coverage':crawl_coverage(events,seed_id),'fetched_events':sum(event.get('event')=='fetched' for event in events)}
    finally:
        lock.unlink(missing_ok=True)


def run_live_workflows(seed_ids=None, *, max_pages=1000, existing_only=False, database_path=DATABASE_PATH, project_root=PROJECT_ROOT):
    """One live run per selected seed, with independent failure reporting."""
    root = Path(project_root).resolve()
    configured = _decode((root / 'config/seeds.json').read_bytes())['seeds']
    requested = seed_ids or [seed['seed_id'] for seed in configured if seed.get('live_workflow')]
    if len(requested) != len(set(requested)) or any(key not in {seed['seed_id'] for seed in configured} for key in requested):
        raise ValueError('Select distinct configured seed IDs.')
    result = {'operation': 'live_source_workflows', 'live_crawl_performed': True, 'api_calls_made': 0,
              'started_at': datetime.now(timezone.utc).isoformat(), 'sources': []}
    for key in requested:
        try:
            report = run_seed_workflow(key, limit=1000, max_pages=max_pages, reextract=False, existing_only=existing_only,
                                       database_path=database_path, project_root=root)
            result['sources'].append(report)
        except (ValueError, LookupError, RuntimeError, OSError, sqlite3.Error) as error:
            result['sources'].append({'seed_id': key, 'stage': 'failed', 'error_type': type(error).__name__, 'error': str(error)})
    result['finished_at'] = datetime.now(timezone.utc).isoformat()
    result['stage'] = 'completed_with_review' if any(item.get('attention') or item.get('stage') == 'failed' or item.get('counts', {}).get('failed') for item in result['sources']) else 'dashboard_updated'
    return result




def _python_draft(directory, root):
    origin_name = "python-provenance.json"
    origin = _decode((directory / origin_name).read_bytes())
    required = {"version", "method", "author", "author_kind", "created_at", "bundle_id",
                "snapshot_ids", "candidate_sha256", "candidate_fingerprint", "api_calls_made"}
    valid_origin = origin.get("version") == "python-extraction-v1" and origin.get("method") == "python_rules"
    if set(origin) != required or not valid_origin:
        raise ValueError("Invalid Python provenance format.")
    allowed_authors = {"software"}
    if origin["author_kind"] not in allowed_authors or not isinstance(origin["author"], str) or not origin["author"].strip():
        raise ValueError("Python extraction requires an identified author and author kind.")
    if type(origin["api_calls_made"]) is not int or origin["api_calls_made"] != 0:
        raise ValueError("Python extraction cannot claim provider API calls.")
    when = datetime.fromisoformat(origin["created_at"])
    if when.tzinfo is None or when.utcoffset() is None:
        raise ValueError("Python provenance timestamp requires a timezone.")
    for name in ("request.json", "response.json", "interaction.json"):
        if (directory / name).exists():
            raise ValueError("Python draft cannot contain API request/response artifacts.")
    content = (directory / "candidate.json").read_bytes()
    candidate = ScholarshipExtraction.model_validate(_decode(content))
    if (sha256(content).hexdigest() != origin["candidate_sha256"]
            or candidate_fingerprint(candidate) != origin["candidate_fingerprint"]):
        raise ValueError("Original Python candidate changed after registration.")
    if candidate.bundle_id != origin["bundle_id"] or candidate.snapshot_ids != origin["snapshot_ids"]:
        raise ValueError("Python provenance has different source identities.")
    bundle = load_bundle(root / "data/runtime/bundles" / f"{candidate.bundle_id}.json", project_root=root)
    if candidate.snapshot_ids != [ref.snapshot_id for ref in bundle.documents]:
        raise ValueError("Python candidate snapshots differ from its bundle.")
    return candidate, origin




def _saved_draft(draft_directory, candidate_file=None, *, project_root=PROJECT_ROOT):
    """Recheck the immutable Python candidate and its source identities."""
    root = Path(project_root).resolve()
    directory = _inside(root, draft_directory, "data/runtime/extractions")
    original, origin = _python_draft(directory, root)
    candidate = original
    if candidate_file is not None:
        candidate = ScholarshipExtraction.model_validate(_decode(
            _inside(root, candidate_file, "data/runtime/extractions").read_bytes()))
    if candidate.bundle_id != original.bundle_id or candidate.snapshot_ids != original.snapshot_ids:
        raise ValueError("Candidate correction has different source identities.")
    report = validate_evidence(candidate, root / "data/runtime/bundles" / f"{candidate.bundle_id}.json", project_root=root)
    origin_path = directory / "python-provenance.json"
    return candidate, report, {"extraction_method": origin["method"]}, origin_path, origin_path.read_bytes()


def check_saved_draft(draft_directory, *, candidate_file=None, project_root=PROJECT_ROOT):
    """Recheck saved output locally, including previously rejected evidence paths."""
    candidate, report, _, _, _ = _saved_draft(
        draft_directory, candidate_file, project_root=project_root,
    )
    return {
        **report.to_dict(), "scholarship_name": candidate.scholarship_name.value,
        "api_calls_made": 0, "database_rows_changed": 0,
    }


def assessment_template(draft_directory, *, candidate_file=None, project_root=PROJECT_ROOT):
    """Return review data; unresolved semantic decisions remain pending."""
    from scholarship_intelligence.models import Claim

    candidate, report, _, _, _ = _saved_draft(
        draft_directory, candidate_file, project_root=project_root,
    )
    checks = []
    for check_id in CHECK_DEFINITIONS:
        if check_id == "source_integrity":
            status, reason = "pass", "Saved raw and prepared content passed bundle integrity checks."
        elif check_id == "evidence_integrity":
            status = "pass" if report.evidence_checks_passed else "fail"
            reason = "Deterministic quote, location, field-path and URL checks " + ("passed." if status == "pass" else "failed; inspect the evidence report.")
        else:
            status, reason = "pending", "Source-context review has not been completed for this check."
        checks.append(CheckResult(check_id=check_id, status=status, reason=reason))
    assessment = SemanticAssessment(
        bundle_id=candidate.bundle_id, snapshot_ids=tuple(candidate.snapshot_ids),
        candidate_fingerprint=candidate_fingerprint(candidate),
        assessed_at=datetime.now(timezone.utc), evaluator="pending_operator_review",
        checks=tuple(checks),
        withheld_fields=tuple(name for name in type(candidate).model_fields
                              if isinstance(getattr(candidate, name), Claim)
                              and getattr(candidate, name).value is not None),
    )
    return assessment.model_dump(mode="json")


def create_review_package(
    draft_directory, pilot_file, assessment_file, *, candidate_file=None,
    project_root=PROJECT_ROOT,
):
    """Package explicit review decisions for the existing import/update path.

    This computes reports from supplied review decisions; it does not invent
    semantic passes, edit claims, call an API or store scholarship rows.
    """
    root = Path(project_root).resolve()
    candidate, evidence, request, response_path, response_bytes = _saved_draft(
        draft_directory, candidate_file, project_root=root,
    )
    if not evidence.evidence_checks_passed:
        raise ValueError("Candidate still has evidence issues; review packaging is blocked.")
    assessment_path = _inside(root, assessment_file, "data/runtime/extractions")
    data = _decode(assessment_path.read_bytes())
    assessment = SemanticAssessment.model_validate(data.get("assessment", data))
    score = score_candidate(candidate, assessment)
    if assessment.evaluator == "pending_operator_review":
        raise ValueError("Complete source review and identify the evaluator before packaging.")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    directory = response_path.parent / f"reviewed-{stamp}-{uuid4().hex[:8]}"
    candidate_bytes = (json.dumps(candidate.model_dump(mode="json"), ensure_ascii=False,
                                  indent=2, allow_nan=False) + "\n").encode("utf-8")
    candidate_hash = sha256(candidate_bytes).hexdigest()
    assessment_bytes = (json.dumps({"candidate_sha256": candidate_hash,
                                   "assessment": assessment.model_dump(mode="json")},
                                  ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")
    confidence = {
        "candidate_sha256": candidate_hash,
        "assessment_sha256": sha256(assessment_bytes).hexdigest(), **score.to_dict(),
    }
    if request.get("extraction_method") == "python_rules":
        method = request["extraction_method"]
        origin_key = "python_provenance"
        review = {"extraction_method": method, origin_key + "_path": response_path.relative_to(root).as_posix(),
                  origin_key + "_sha256": sha256(response_bytes).hexdigest(),
                  "reviewer": assessment.evaluator, "reviewed_at": assessment.assessed_at.isoformat(),
                  "candidate_correction_selected": candidate_file is not None}
    else:
        raise ValueError("Only Python extraction provenance is supported.")
    # Check crawl provenance and all existing import rules before publishing a
    # complete package. Partial output is removed if validation rejects it.
    directory.mkdir(parents=True, exist_ok=False)
    try:
        (directory / "candidate.json").write_bytes(candidate_bytes)
        (directory / "semantic-assessment-review.json").write_bytes(assessment_bytes)
        for name, value in (("review.json", review), ("confidence-review.json", confidence),
                            ("evidence-validation.json", evidence.to_dict())):
            (directory / name).write_text(json.dumps(value, ensure_ascii=False, indent=2,
                                                   allow_nan=False) + "\n", encoding="utf-8")
        prepared = prepare_review_import(directory, pilot_file, project_root=root)
    except BaseException:
        for name in ("candidate.json", "semantic-assessment-review.json", "review.json",
                     "confidence-review.json", "evidence-validation.json"):
            (directory / name).unlink(missing_ok=True)
        directory.rmdir()
        raise
    return {
        **prepared.summary(), "stage": "review_package_ready",
        "review_directory": directory.relative_to(root).as_posix(),
        "database_rows_changed": 0,
    }



def _seed_offerings(seed, events, *, project_root=PROJECT_ROOT):
    """Small source rules feed the shared workflow; no model chooses URLs."""
    from urllib.parse import urlsplit
    latest = {}
    parents = {}
    supporting_links = {}
    for event in events:
        if event.get("seed_id") != seed["seed_id"]:
            continue
        if event.get("event") == "discovery":
            target = event.get("scheduled_url") or event.get("url")
            parents.setdefault(target, set()).add(event.get("parent_url"))
            context = str(event.get("link_text", "")) + " " + str(event.get("url", ""))
            if re.search(r"\b(?:applications?|apply|deadlines?|notices?|announcements?|guidelines?|renewal|extensions?)\b|\.pdf(?:$|[?#])", context, re.I):
                supporting_links.setdefault(event.get("parent_url"), {})[target] = event
        if event.get("event") != "fetched":
            continue
        snapshot = event.get("snapshot")
        if not isinstance(snapshot, dict):
            raise ValueError("Fetched event lacks snapshot metadata.")
        when = datetime.fromisoformat(snapshot["retrieved_at"])
        if when.tzinfo is None or when.utcoffset() is None:
            raise ValueError("Crawl timestamp lacks a timezone.")
        url = snapshot["source_url"]
        old = latest.get(url)
        if old is None or when > old[0]:
            latest[url] = (when, event)
        elif when == old[0] and snapshot["content_hash"] != old[1]["snapshot"]["content_hash"]:
            raise ValueError("Conflicting latest snapshots for one URL.")
    good = {url: event for url, (_, event) in latest.items() if event.get("http_status") == 200}
    from scholarship_intelligence.source_profiles import discover_scopes
    if seed.get("workflow", {}).get("cards"):
        root = Path(project_root).resolve()
        entries = [event for event in events if event.get("event") == "fetched" and event.get("http_status") == 200
                   and event.get("seed_id") == seed["seed_id"] and (event["snapshot"]["source_url"] == seed["start_url"]
                   or event.get("request_method") == "POST")]
        offerings, seen_scopes = [], {}
        all_scopes = []
        for entry in entries:
            scopes = discover_scopes((root / entry["snapshot"]["raw_file_path"]).read_bytes(), seed)
            all_scopes.extend((scope, entry) for scope in scopes)
        support_owners = {}
        for scope, entry in all_scopes:
            for url in scope["support_urls"]:
                support_owners.setdefault(url, set()).add(scope["offering_key"])
        for scope, entry in all_scopes:
            if scope["offering_key"] in seen_scopes:
                previous_scope = seen_scopes[scope["offering_key"]]
                if scope.get("card_text_sha256") != previous_scope.get("card_text_sha256") or scope["provider"] != previous_scope["provider"]:
                    previous_scope.setdefault("catalogue_conflicts", []).append(entry["snapshot"]["source_url"])
                continue
            seen_scopes[scope["offering_key"]] = scope
            listing = entry["snapshot"]["source_url"]
            sources = [listing] + [url for url in scope["support_urls"] if url in good and url != listing]
            missing = [url for url in scope["support_urls"] if url not in good]
            scope["unfetched_support_urls"] = missing
            scope["shared_support_urls"] = sorted(url for url in scope["support_urls"] if len(support_owners[url]) > 1)
            offerings.append({"source_url": listing, "offering_key": scope["offering_key"], "source_scope": scope,
                              "source_urls": sources, "supporting_source_review": [{"url": url, "reason": "not_successfully_fetched"} for url in missing],
                              "source_hashes": {**{url: good[url]["snapshot"]["content_hash"] for url in sources if url != listing},
                                                listing: entry["snapshot"]["content_hash"]}})
        return offerings
    rule = seed.get("workflow", {})
    pattern = rule.get("detail_path_pattern")
    if seed["seed_id"] == "iit_delhi":
        pattern = r"/scholarships-awards/[0-9]+/?"
    if pattern:
        details = sorted(url for url, event in good.items()
                         if event.get("page_role") == "candidate_detail"
                         and urlsplit(url).hostname in seed["allowed_hosts"]
                         and re.fullmatch(pattern, urlsplit(url).path))
    elif rule.get("entry_is_offering") or seed["seed_id"] in {"ffe", "chevening_india"}:
        details = [seed["start_url"]] if seed["start_url"] in good else []
    else:
        raise ValueError("This seed needs an offering-grouping rule before batch extraction; start with iit_delhi.")
    offerings = []
    for url in details:
        # Only directly linked, exclusively associated support is grouped.
        # A shared document/listing is not silently attached to every offering.
        support = sorted(target for target, event in good.items()
                         if event.get("page_role") in {"document", "guideline", "announcement", "application"}
                         and parents.get(target, set()) == {url}
                         and urlsplit(target).hostname in seed["allowed_hosts"])
        unresolved = []
        for target, link in supporting_links.get(url, {}).items():
            if target in support or target == url or target in details:
                continue
            reason = "shared_link_requires_offering_association" if len(parents.get(target, set())) > 1 else "not_successfully_fetched_or_not_a_support_page"
            unresolved.append({"url": link.get("url") or target, "link_text": link.get("link_text", ""),
                               "reason": reason, "crawl_decision": link.get("decision")})
        sources = [url] + list(dict.fromkeys([target for target in support if target != url] + [target for target in seed.get("support_urls", []) if target in good and target != url]))
        identity = {}
        if seed.get("extraction_profile") == "official_sections_v1":
            from lxml import html
            page = html.fromstring((Path(project_root) / good[url]["snapshot"]["raw_file_path"]).read_bytes())
            headings = page.xpath('//main//h1') or page.xpath('//h1')
            if headings:
                from scholarship_intelligence.source_profiles import programme_key
                identity["offering_key"] = url + "::" + sha256(programme_key(' '.join(headings[0].text_content().split())).encode()).hexdigest()[:16]
        offerings.append({**identity, "source_url": url, "source_urls": sources, "supporting_source_review": unresolved,
                          "source_hashes": {target: good[target]["snapshot"]["content_hash"] for target in sources}})
    return offerings


def _workflow_stored(database, root):
    from contextlib import closing
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as connection:
        rows = connection.execute("SELECT scholarship_id, verification_details_json FROM scholarships").fetchall()
    result = []
    for scholarship_id, content in rows:
        details = _decode(content)
        identity = details.get("storage_identity", {})
        bundle_id = identity.get("bundle_id")
        if not isinstance(bundle_id, str) or re.fullmatch(r"[0-9a-f]{64}", bundle_id) is None:
            continue
        bundle = load_bundle(root / "data/runtime/bundles" / (bundle_id + ".json"), project_root=root)
        for artifact in details.get("artifacts", {}).values():
            path = _inside(root, artifact["path"], "data/runtime/extractions")
            if sha256(path.read_bytes()).hexdigest() != artifact["sha256"]:
                raise ValueError("Stored review artifact changed; inspect it before continuing.")
        result.append((scholarship_id, {ref.source_url: ref.content_hash for ref in bundle.documents}))
    return result


@document_checks()
def run_seed_workflow(seed_id, *, pilot_file=None, resume=False, dry_run=False,
                      limit=30, reextract=False, max_pages=30, continue_crawl=False, existing_only=False,
                      database_path=DATABASE_PATH, project_root=PROJECT_ROOT):
    """Crawl/prepare/extract/check/store one seed, using Python only.

    The persisted batch report supports resumable deterministic extraction.
    """
    from contextlib import closing
    import os
    import sys
    from scholarship_intelligence.python_extraction import (
        extract_bundle, assess_candidate, save_draft, RULE_VERSION,
    )
    root, database = Path(project_root).resolve(), Path(database_path).resolve()
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise ValueError("Offering limit must be between 1 and 1000.")
    if resume and pilot_file is not None:
        raise ValueError("Select --resume or --pilot, not both.")
    if type(max_pages) is not int or not 1 <= max_pages <= 1000:
        raise ValueError("Crawl page budget must be between 1 and 1000.")
    if continue_crawl and (dry_run or not (resume or pilot_file is not None)):
        raise ValueError("Live crawl continuation requires --resume or --pilot and cannot use --dry-run.")
    if not database.is_file():
        raise FileNotFoundError("Initialize the database before running the workflow.")
    seed = next((item for item in _decode((root / "config/seeds.json").read_bytes())["seeds"] if item["seed_id"] == seed_id), None)
    if seed is None:
        raise ValueError("Unknown seed ID.")
    if seed_id not in {"iit_delhi", "ffe", "chevening_india"} and not seed.get("workflow"):
        raise ValueError("Offering grouping is not configured for this seed; use iit_delhi first.")
    if resume:
        with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as connection:
            prior = connection.execute("SELECT summary_json FROM crawl_runs ORDER BY run_id DESC").fetchall()
        previous = next((data for (content,) in prior if (data := _decode(content)).get("operation") == "seed_workflow" and data.get("seed_id") == seed_id and data.get("pilot_file")), None)
        if previous is None:
            raise ValueError("No previous workflow crawl found for this seed.")
        pilot_file = previous["pilot_file"]
    if dry_run and pilot_file is None:
        raise ValueError("Preview requires --pilot or --resume; it does not crawl.")
    report = {"operation": "seed_workflow", "seed_id": seed_id, "extraction_method": "python_rules",
        "rule_version": RULE_VERSION, "dry_run": dry_run, "live_crawl_performed": False,
        "api_calls_made": 0, "generation_requests_made": 0, "retrieval_requests_made": 0,
        "scholarship_rows_changed": 0, "offerings": [], "counts": {}}
    lock, locked, run_id = root / "data/runtime/workflow.lock", False, None

    def save(status="RUNNING"):
        counts = {}
        for row in report["offerings"]:
            counts[row["stage"]] = counts.get(row["stage"], 0) + 1
        report["counts"] = counts
        report["scholarship_rows_changed"] = sum(bool(row.get("inserted")) for row in report["offerings"])
        if run_id is not None:
            with connect_database(database) as connection:
                with transaction(connection):
                    connection.execute("UPDATE crawl_runs SET status=?, finished_at=?, summary_json=? WHERE run_id=?",
                        (status, None if status == "RUNNING" else datetime.now(timezone.utc).isoformat(), json.dumps(report, ensure_ascii=False), run_id))

    try:
        if not dry_run:
            with lock.open("x", encoding="utf-8") as stream:
                stream.write(json.dumps({"pid": os.getpid(), "seed_id": seed_id, "method": "python_rules"}))
            locked = True
            with connect_database(database) as connection:
                with transaction(connection):
                    run_id = connection.execute("INSERT INTO crawl_runs(trigger, started_at, summary_json) VALUES ('command', ?, ?)",
                        (datetime.now(timezone.utc).isoformat(), json.dumps(report))).lastrowid
            report["run_id"] = run_id
        if pilot_file is None or continue_crawl:
            print("Continuing crawl: " + seed_id if continue_crawl else "Crawling seed: " + seed_id, file=sys.stderr, flush=True)
            crawl = crawl_sources(seed_id=seed_id, max_pages=max_pages,
                                  continue_from=pilot_file if continue_crawl else None, project_root=root)
            pilot_file = crawl["pilot_file"]
            report["live_crawl_performed"] = crawl["live_crawl_performed"]
            report["crawl_page_budget"] = max_pages
        pilot = _inside(root, pilot_file, "data/runtime")
        report["pilot_file"] = pilot.relative_to(root).as_posix()
        events = [_decode(line) for line in pilot.read_text(encoding="utf-8").splitlines() if line.strip()]
        report["coverage"] = crawl_coverage(events, seed_id)
        offerings = _seed_offerings(seed, events, project_root=root)
        report.update(discovered_offerings=len(offerings), selected_offerings=min(limit, len(offerings)))
        if not offerings:
            report["attention"] = "No fetched pages matched the scholarship grouping rule; inspect the crawl report."
        stored = _workflow_stored(database, root)
        from scholarship_intelligence.source_profiles import programme_key
        with closing(sqlite3.connect(database.as_uri()+"?mode=ro",uri=True)) as connection:
            stored_identity = connection.execute("SELECT s.scholarship_id,p.name,s.verification_details_json FROM scholarships s JOIN programmes p ON p.programme_id=s.programme_id").fetchall()
            stored_names = {identity: name for identity, name, details in stored_identity}
            stored_details = {identity: _decode(details) for identity, name, details in stored_identity}
            from scholarship_intelligence.models import classify_offering
            stored_types = {identity: classify_offering(name, {**_decode(details).get("accepted_claims", {}), **_decode(details).get("draft_claims", {})})["offering_type"] for identity, name, details in stored_identity}
        def matches(row, hashes, identity):
            scope = row.get("source_scope")
            return (not scope or programme_key(stored_names[identity]) == programme_key(scope["name"])) and bool(set(row["source_urls"]) & set(hashes))

        if existing_only:
            discovered = len(offerings)
            offerings = [item for item in offerings if any(matches(item, hashes, identity) for identity, hashes in stored)]
            report.update(existing_only=True, crawl_discovered_offerings=discovered,
                          excluded_new_offerings=discovered-len(offerings),
                          discovered_offerings=len(offerings), selected_offerings=min(limit, len(offerings)))
        report["offerings"] = [{**item, "stage": "queued"} for item in offerings[:limit]]
        save()
        with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as connection:
            reuse_reports = connection.execute('SELECT summary_json FROM crawl_runs WHERE run_id != ? ORDER BY run_id DESC', (run_id,)).fetchall()
        reusable_rows = [old for (content,) in reuse_reports for data in [_decode(content)]
                         if data.get('operation') == 'seed_workflow' and data.get('seed_id') == seed_id
                         for old in data.get('offerings', [])]
        for index, row in enumerate(report["offerings"], 1):
            try:
                exact = [identity for identity, hashes in stored if hashes == row["source_hashes"] and matches(row, hashes, identity)]
                if len(exact) > 1:
                    raise ValueError("Multiple stored offerings share this source bundle; resolve their identity first.")
                if exact and not reextract and not dry_run:
                    from scholarship_intelligence.reuse import refresh
                    packet = stored_details[exact[0]].get('python_source_assessment')
                    try:
                        if not packet or packet.get('source_scope') != row.get('source_scope'):
                            raise ValueError('The previous source scope is different.')
                        updated = refresh(packet, seed, events, pilot, root)
                        value = dict(stored_details[exact[0]])
                        value['python_source_assessment'] = updated
                        with connect_database(database) as connection:
                            with transaction(connection):
                                connection.execute('UPDATE scholarships SET verification_details_json=? WHERE scholarship_id=?',
                                                   (json.dumps(value, ensure_ascii=False), exact[0]))
                        stored_details[exact[0]] = value
                        row.update(stage='unchanged', scholarship_id=exact[0], reused_validated=True,
                                   candidate_path=updated['candidate_path'], candidate_sha256=updated['candidate_sha256'],
                                   bundle_id=updated['bundle_id'], offering_type=stored_types[exact[0]],
                                   name=stored_names[exact[0]], lifecycle_status=updated['lifecycle_status'])
                        continue
                    except (ValueError, OSError, KeyError, TypeError):
                        pass  # Missing/invalid proof or rule change requires full extraction.

                if not reextract and not dry_run:
                    from scholarship_intelligence.reuse import fingerprint
                    excluded = next((old for old in reusable_rows
                        if old.get('offering_type') is not None and (old.get('offering_type') != 'scholarship' or (exact and not stored_details[exact[0]].get('python_source_assessment')))
                        and old.get('extraction_cache_fingerprint') == fingerprint(seed)
                        and old.get('offering_key', old['source_url']) == row.get('offering_key', row['source_url'])
                        and old.get('source_hashes') == row['source_hashes']), None)
                    if excluded:
                        try:
                            target = _inside(root, excluded['candidate_path'], 'data/runtime/extractions')
                            if sha256(target.read_bytes()).hexdigest() != excluded['candidate_sha256']:
                                raise ValueError('Excluded draft changed.')
                            load_bundle(root / 'data/runtime/bundles' / (excluded['bundle_id'] + '.json'), project_root=root)
                            row.update({k: v for k, v in excluded.items() if k not in {'source_hashes', 'source_urls', 'source_scope', 'source_url'}})
                            row.update(stage='unchanged', reused_extraction=True, inserted=False)
                            continue
                        except (OSError, KeyError, ValueError):
                            pass
                if dry_run:
                    row["stage"] = "ready"
                    continue
                print(f"Python extraction {index}/{len(report['offerings'])}: {(row.get('source_scope') or {}).get('name', row['source_url'])}", file=sys.stderr, flush=True)
                row["stage"] = "processing"
                from scholarship_intelligence.progress import report_progress
                report_progress(root, seed_id, 'Extracting changed/new records', current=index, total=len(report['offerings']))
                save()
                # Reuse a previously pinned prepared bundle only for the exact
                # same programme and source bytes. Rule upgrades need extraction,
                # not repeated parsing of identical large shared PDFs.
                reusable = next((old for old in reusable_rows
                                 if old.get("offering_key", old["source_url"]) == row.get("offering_key", row["source_url"])
                                 and old.get("source_hashes") == row["source_hashes"] and old.get("bundle_id")), None) if resume else None
                if reusable:
                    manifest_path = root / "data/runtime/bundles" / (reusable["bundle_id"] + ".json")
                    manifest = load_bundle(manifest_path, project_root=root)
                    if any(row["source_hashes"].get(ref.source_url) != ref.content_hash for ref in manifest.documents):
                        raise ValueError("Prepared bundle does not match this programme's current source bytes.")
                    prepared = {"bundle_file_path": manifest_path.relative_to(root).as_posix(),
                                "support_failures": reusable.get("supporting_source_review", [])}
                else:
                    prepared = prepare_crawl_bundle(pilot, row["source_urls"], seed_id + ":" + row.get("offering_key", row["source_url"]), project_root=root,
                        source_hashes=row["source_hashes"], allow_support_failures=True)
                row.setdefault("supporting_source_review", []).extend(prepared.get("support_failures", []))
                bundle_file = root / prepared["bundle_file_path"]
                candidate, details = extract_bundle(bundle_file, {**seed, "source_scope": row.get("source_scope")}, row["source_url"], project_root=root)
                details["supporting_source_review"] = row.get("supporting_source_review", [])
                directory = save_draft(candidate, details, project_root=root)
                checked = check_saved_draft(directory, project_root=root)
                evidence = validate_evidence(candidate, bundle_file, project_root=root)
                origin = _decode((directory / "python-provenance.json").read_bytes())
                assessment = assess_candidate(candidate, details, evidence, seed, assessed_at=datetime.fromisoformat(origin["created_at"]))
                score = score_candidate(candidate, assessment)
                row.update(stage="review_required", name=candidate.scholarship_name.value,
                    offering_type=details["offering_classification"]["offering_type"],
                    offering_classification=details["offering_classification"],
                    provider=candidate.provider.value, bundle_id=candidate.bundle_id,
                    draft_directory=directory.relative_to(root).as_posix(),
                    candidate_path=(directory / "candidate.json").relative_to(root).as_posix(),
                    candidate_sha256=sha256((directory / "candidate.json").read_bytes()).hexdigest(),
                    extraction_details=details, evidence=checked, assessment=assessment.model_dump(mode="json"),
                    confidence=score.to_dict(), confidence_score=score.to_dict()["confidence_score"],
                    verification_status=score.verification_status, lifecycle_status="REVIEW_REQUIRED",
                    blockers=list(score.blockers))
                from scholarship_intelligence.reuse import fingerprint
                row["extraction_cache_fingerprint"] = fingerprint(seed)
                if details.get("programme_identity_review_reasons"):
                    row.update(stage="identity_review_required", reason=" ".join(details["programme_identity_review_reasons"]))
                    continue
                existing = [identity for identity, hashes in stored if matches(row, hashes, identity)]
                # A shared page can retain historical entries with another name
                # (for example, an old award parsed from a scholarship page).
                named = [identity for identity in existing if programme_key(stored_names[identity]) == programme_key(candidate.scholarship_name.value)]
                if named:
                    existing = named
                if existing:
                    if any(stored_types.get(identity) != row["offering_type"] for identity in existing):
                        row.update(stage="identity_review_required", scholarship_ids=existing,
                                   reason="The extraction changed the programme classification. Retain the existing record and review its identity before accepting changes.")
                        continue
                    if len(existing) == 1:
                        packet = stored_details[existing[0]].get("python_source_assessment")
                        if packet:
                            baseline_path = _inside(root, packet["candidate_path"], "data/runtime/extractions")
                            if sha256(baseline_path.read_bytes()).hexdigest() != packet["candidate_sha256"]:
                                raise ValueError("Previous source candidate changed; comparison is not trusted.")
                            baseline_candidate, _, _, _, _ = _saved_draft(baseline_path.parent, project_root=root)
                            from scholarship_intelligence.comparison import compare_source_values
                            row["observed_changes"] = compare_source_values(baseline_candidate, candidate)
                            old_bundle = load_bundle(root / "data/runtime/bundles" / (baseline_candidate.bundle_id + ".json"), project_root=root)
                            source_changed = {ref.source_url: ref.content_hash for ref in old_bundle.documents} != row["source_hashes"]
                            row["change_basis"] = "source_and_extraction_revision" if source_changed and packet.get("rule_version") != RULE_VERSION else "source_revision" if source_changed else "extraction_revision"
                    row.update(stage="update_review_required", scholarship_ids=existing,
                               reason="Saved-source extraction uses changed content or updated rules; compare the draft with the existing offering before accepting changes. No duplicate record was inserted.")
                    continue
                if candidate.scholarship_name.value is None or candidate.provider.value is None:
                    row["reason"] = "Name or provider could not be established; retain the draft for review."
                    continue
                assessment_path = directory / "assessment-input.json"
                content = json.dumps(assessment.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n"
                if assessment_path.exists() and assessment_path.read_text(encoding="utf-8") != content:
                    raise ValueError("The saved automatic assessment changed; inspect before importing.")
                if not assessment_path.exists():
                    assessment_path.write_text(content, encoding="utf-8")
                existing_reviews = sorted(directory.glob("reviewed-*/review.json"))
                if len(existing_reviews) > 1:
                    raise ValueError("Multiple review packages require explicit selection.")
                if existing_reviews:
                    review_directory = existing_reviews[0].parent
                else:
                    review_directory = root / create_review_package(directory, pilot, assessment_path, project_root=root)["review_directory"]
                saved = persist_review_import(prepare_review_import(review_directory, pilot, project_root=root), database_path=database)
                row.update(stage="stored", scholarship_id=saved["scholarship_id"], inserted=saved["inserted"],
                           review_directory=review_directory.relative_to(root).as_posix(),
                           lifecycle_status=inspect_scholarship(saved["scholarship_id"], database_path=database)["lifecycle_status"])
                stored.append((saved["scholarship_id"], row["source_hashes"]))
                stored_names[saved["scholarship_id"]] = candidate.scholarship_name.value
                stored_types[saved["scholarship_id"]] = row["offering_type"]
                stored_details[saved["scholarship_id"]] = {}
            except (ValueError, LookupError, RuntimeError, OSError, sqlite3.Error) as error:
                row.update(stage="failed", error_type=type(error).__name__, error=str(error))
            finally:
                save()
        final = "COMPLETED" if (report["offerings"] and not report["coverage"]["budget_skipped_count"]
                                and not report["coverage"]["failed_pages"]
                                and report["selected_offerings"] == report["discovered_offerings"]
                                and all(row["stage"] in {"unchanged", "stored"} for row in report["offerings"])) else "PARTIAL"
        save(final)
        if not dry_run and report["selected_offerings"] == report["discovered_offerings"] and report["offerings"] and any(row.get("candidate_path") and row.get("offering_type") == "scholarship" and not row.get("reused_validated") for row in report["offerings"]):
            try:
                from scholarship_intelligence.progress import report_progress
                report_progress(root, seed_id, "Verifying and scoring changed records")
                scored = score_seed_scholarships(seed_id, apply=True, database_path=database, project_root=root, _skip_reused=True)
                report["evidence_scoring"] = {key: value for key, value in scored.items() if key != "scholarships"}
                scores = {item.get("offering_key", item["source_url"]): item for item in scored["scholarships"]}
                for row in report["offerings"]:
                    if row.get("offering_key", row["source_url"]) in scores:
                        row["evidence_confidence_score"] = scores[row.get("offering_key", row["source_url"])]["confidence_score"]
                        row["verification_status"] = scores[row.get("offering_key", row["source_url"])]["verification_status"]
                        row["lifecycle_status"] = scores[row.get("offering_key", row["source_url"])]["lifecycle_status"]
                        row["source_freshness"] = scores[row.get("offering_key", row["source_url"])]["source_freshness"]
                save(final)
            except (ValueError, LookupError, OSError, sqlite3.Error) as error:
                report["evidence_scoring"] = {"stage": "review_required", "reason": str(error)}
                save(final)
        if not dry_run and seed_id == "nsp":
            listing_events = [event for event in events if event.get("event") == "fetched" and event.get("http_status") == 200
                              and event.get("snapshot", {}).get("source_url") == NSP_AVAILABILITY_URL]
            if listing_events:
                try:
                    saved = listing_events[-1]["snapshot"]
                    source_path = (root / saved["raw_file_path"]).resolve()
                    if not source_path.is_relative_to(root / "data/runtime/snapshots/raw") or sha256(source_path.read_bytes()).hexdigest() != saved["content_hash"]:
                        raise ValueError("Saved availability crawl bytes changed.")
                    report["availability_refresh"] = refresh_availability(apply=True, database_path=database, project_root=root,
                        saved_content=source_path.read_bytes(), retrieved_at=saved["retrieved_at"])
                except (ValueError, OSError, sqlite3.Error) as error:
                    report["availability_refresh"] = {"stage": "review_required", "reason": str(error)}
                save(final)
        return report
    except BaseException as error:
        report["interrupted_or_failed"] = type(error).__name__
        save("PARTIAL")
        raise
    finally:
        if locked:
            lock.unlink(missing_ok=True)




def check_seed_scholarships(seed_id, *, workflow_report=None, database_path=DATABASE_PATH, project_root=PROJECT_ROOT, _skip_reused=False):
    """Check every scholarship's saved values and evidence, without writes or limits."""
    from collections import Counter
    from contextlib import closing
    from urllib.parse import urlsplit
    from scholarship_intelligence.models import classify_offering, Claim
    from scholarship_intelligence.python_extraction import RULE_VERSION
    from scholarship_intelligence.semantic_assessment import SemanticAssessment, validate_assessment
    from scholarship_intelligence import dashboard

    root = Path(project_root).resolve()
    seed = next((item for item in _decode((root / "config/seeds.json").read_bytes())["seeds"] if item["seed_id"] == seed_id), None)
    if seed is None:
        raise ValueError("Unknown seed ID.")
    with closing(sqlite3.connect(Path(database_path).resolve().as_uri() + "?mode=ro", uri=True)) as connection:
        reports = [_decode(row[0]) for row in connection.execute("SELECT summary_json FROM crawl_runs ORDER BY run_id DESC")]
    reports = [report for report in reports if report.get("operation") == "seed_workflow" and report.get("seed_id") == seed_id]
    if workflow_report is not None:
        selected = _decode(_inside(root, workflow_report, "data/runtime").read_bytes())
    else:
        selected = next((report for report in reports if report.get("rule_version") in {RULE_VERSION, "python-sections-v6", "python-sections-v7", "python-sections-v8", "python-sections-v9", "python-sections-v10", "python-sections-v11", "python-sections-v12"}
                         and report.get("offerings") and any(row.get("candidate_path") for row in report["offerings"])), None)
    if not isinstance(selected, dict) or selected.get("operation") != "seed_workflow" or selected.get("seed_id") != seed_id:
        raise ValueError("No complete extraction report for this seed. Run workflow --resume --reextract --limit 1000 first.")
    if selected.get("rule_version") not in {RULE_VERSION, "python-sections-v6", "python-sections-v7", "python-sections-v8", "python-sections-v9", "python-sections-v10", "python-sections-v11", "python-sections-v12"} or selected.get("selected_offerings") != selected.get("discovered_offerings"):
        raise ValueError("All fetched offerings must have current-rule drafts before checking the entire seed.")
    if reports:
        latest_hashes = {row.get("offering_key", row["source_url"]): row.get("source_hashes") for row in reports[0].get("offerings", [])}
        selected_hashes = {row.get("offering_key", row["source_url"]): row.get("source_hashes") for row in selected["offerings"]}
        if latest_hashes != selected_hashes:
            raise ValueError("The extraction report differs from the latest observed seed sources; re-extract before checking.")

    stored_by_url = {}
    stored_records = {}
    for record in dashboard.filter_records(dashboard.load_records(database_path, project_root=root, include_source_assessments=False)):
        claim = record["details"].get("accepted_claims", {}).get("official_source", {})
        url = claim.get("value")
        if isinstance(url, str) and urlsplit(url).hostname in seed["allowed_hosts"]:
            stored_by_url.setdefault(url, []).append(record["scholarship_id"])
            stored_records[record["scholarship_id"]] = record
    rows = [row for row in selected["offerings"] if row.get("offering_type") == "scholarship" or (not row.get("source_scope") and row["source_url"] in stored_by_url)]
    if len({row.get("offering_key", row["source_url"]) for row in rows}) != len(rows):
        raise ValueError("Duplicate source identities in seed extraction report.")
    results, totals = [], Counter()
    field_counts = {}
    matched_ids = {identity for row in rows for identity in ([row["scholarship_id"]] if row.get("scholarship_id") else row.get("scholarship_ids", []))}
    missing_stored = sorted(url for url, ids in stored_by_url.items() if url not in {row["source_url"] for row in rows} and not set(ids).intersection(matched_ids))
    for row in rows:
        if _skip_reused and row.get("reused_validated"):
            totals["reused_validated"] += 1
            continue
        ids = ([row["scholarship_id"]] if row.get("scholarship_id") else row.get("scholarship_ids"))
        if ids is None:
            ids = stored_by_url.get(row["source_url"], [])
        item = {"source_url": row["source_url"], "offering_key": row.get("offering_key", row["source_url"]), "source_scope": row.get("source_scope"), "stored_scholarship_ids": ids,
                "candidate_path": row.get("candidate_path"), "evidence_checks_passed": False}
        results.append(item)
        try:
            path = _inside(root, row["candidate_path"], "data/runtime/extractions")
            content = path.read_bytes()
            if sha256(content).hexdigest() != row["candidate_sha256"]:
                raise ValueError("Candidate bytes differ from the extraction report.")
            candidate, evidence, _, origin_path, origin_bytes = _saved_draft(path.parent, project_root=root)
            origin = _decode(origin_bytes)
            if origin.get("author") != selected["rule_version"] or candidate.bundle_id != row.get("bundle_id"):
                raise ValueError("Candidate provenance differs from current-rule extraction identity.")
            if str(candidate.official_source.value) != row["source_url"]:
                raise ValueError("Candidate official source differs from offering identity.")
            from scholarship_intelligence.source_profiles import programme_key
            for identity in ids:
                stored_record = stored_records.get(identity)
                if stored_record is None or (row.get("source_scope") and programme_key(stored_record["name"]) != programme_key(candidate.scholarship_name.value)):
                    raise ValueError("Stored programme identity does not match this source scope.")
            classification = classify_offering(candidate.scholarship_name.value, candidate.model_dump(mode="json"))
            if classification["offering_type"] != "scholarship":
                raise ValueError("Stored and current programme types disagree; resolve offering identity first.")
            if not evidence.evidence_checks_passed:
                raise ValueError("Saved candidate failed source/evidence validation: " + "; ".join(issue.code for issue in evidence.issues))
            assessment = validate_assessment(candidate, SemanticAssessment.model_validate(row["assessment"]))
            bundle = load_bundle(root / "data/runtime/bundles" / f"{candidate.bundle_id}.json", project_root=root)
            sources = {ref.snapshot_id: ref for ref in bundle.documents}
            parents = {ref.snapshot_id: {block["block_id"]: block for block in _decode((root / ref.prepared_file_path).read_bytes())["blocks"]} for ref in bundle.documents}
            quoted = {entry.evidence_id: entry for entry in candidate.evidence}
            payload = candidate.model_dump(mode="json")
            fields = {}
            for name in type(candidate).model_fields:
                claim = getattr(candidate, name)
                if not isinstance(claim, Claim):
                    continue
                value = payload[name]
                status = "present" if claim.value is not None else "not_applicable" if claim.missing_reason == "not_applicable" else "unknown"
                field_counts.setdefault(name, Counter())[status] += 1
                citations = []
                for evidence_id in claim.evidence_ids:
                    quote = quoted[evidence_id]
                    ref = sources[quote.snapshot_id]
                    citations.append({**quote.model_dump(mode="json"), "source_url": ref.source_url,
                        "raw_file_path": ref.raw_file_path, "prepared_file_path": ref.prepared_file_path,
                        "source_context": [{key: parents[quote.snapshot_id][block_id].get(key)
                            for key in ("block_id", "kind", "text", "heading_path", "source_xpath", "page_number")}
                            for block_id in quote.location.block_ids]})
                fields[name] = {**value, "value_status": status, "source_evidence": citations}
            item.update(scholarship_name=candidate.scholarship_name.value,
                bundle_id=candidate.bundle_id, snapshot_ids=candidate.snapshot_ids,
                candidate_sha256=sha256(content).hexdigest(), candidate_fingerprint=candidate_fingerprint(candidate),
                provenance_path=origin_path.relative_to(root).as_posix(), provenance_sha256=sha256(origin_bytes).hexdigest(),
                offering_classification=classification, fields=fields, evidence_report=evidence.to_dict(),
                assessment=assessment.model_dump(mode="json"), blockers=list(assessment.blockers),
                evidence_checks_passed=True, verification_status="REVIEW_REQUIRED", factual_verification="pending")
            totals["passed"] += 1
            totals["evidence_entries_checked"] += evidence.evidence_checked
            totals["exact_quote_matches"] += evidence.exact_quote_matches
            totals["documents_checked"] += evidence.documents_checked
        except (ValueError, KeyError, TypeError, LookupError, OSError) as error:
            item.update(error_type=type(error).__name__, error=str(error))
            totals["failed"] += 1
    coverage_complete = not selected.get("coverage", {}).get("budget_skipped_count", 0) and not selected.get("coverage", {}).get("failed_pages", 0)
    complete = bool(results) and not totals["failed"] and not missing_stored and coverage_complete
    return {"operation": "seed_evidence_check", "audit_version": "seed-evidence-v1", "seed_id": seed_id,
        "checked_at": datetime.now(timezone.utc).isoformat(), "rule_version": RULE_VERSION,
        "pilot_file": selected["pilot_file"], "workflow_run_id": selected.get("run_id"), "workflow_fingerprint": sha256(json.dumps(selected, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest(),
        "scholarships_checked": len(results), "counts": dict(totals),
        "field_coverage": {name: dict(counts) for name, counts in field_counts.items()},
        "unmatched_stored_scholarships": missing_stored, "observed_crawl_coverage_complete": coverage_complete,
        "all_evidence_checks_passed": complete,
        "stage": "values_and_evidence_checked" if complete else "evidence_review_required",
        "factual_verification": "pending", "confidence_score": None,
        "coverage": selected.get("coverage", {}), "scholarships": results,
        "live_crawl_performed": False, "api_calls_made": 0, "database_rows_changed": 0}



def score_seed_scholarships(seed_id, *, apply=False, database_path=DATABASE_PATH, project_root=PROJECT_ROOT, _skip_reused=False):
    """Recheck all sources, then atomically attach bound dashboard assessments.

    Accepted facts, review identities and evidence history stay intact. Updated
    extraction values are presented as source drafts until semantic review passes.
    """
    from contextlib import closing
    from collections import Counter
    from scholarship_intelligence.confidence import score_source_evidence, review_python_source
    from scholarship_intelligence.semantic_assessment import SemanticAssessment
    from scholarship_intelligence.scholarship_storage import reviewed_lifecycle
    root = Path(project_root).resolve()
    database = Path(database_path).resolve()
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as connection:
        baseline = dict(connection.execute("SELECT scholarship_id, verification_details_json FROM scholarships"))
    audit = check_seed_scholarships(seed_id, database_path=database, project_root=root, _skip_reused=_skip_reused)
    if not any(item.get("evidence_checks_passed") for item in audit["scholarships"]):
        raise ValueError("Every scored record must pass its source/evidence check before scoring or saving.")
    seed = next(item for item in _decode((root / "config/seeds.json").read_bytes())["seeds"] if item["seed_id"] == seed_id)
    pilot_path = _inside(root, audit["pilot_file"], "data/runtime")
    pilot_bytes = pilot_path.read_bytes()
    events = [_decode(line) for line in pilot_bytes.decode("utf-8").splitlines() if line.strip()]
    updates, results = {}, []
    for item in audit["scholarships"]:
        ids = item["stored_scholarship_ids"]
        if len(ids) != 1 or not item.get("evidence_checks_passed"):
            continue
        candidate = ScholarshipExtraction.model_validate_json((root / item["candidate_path"]).read_bytes())
        assessment = SemanticAssessment.model_validate(item["assessment"])
        retrieval_times = {}
        for ref in load_bundle(root / "data/runtime/bundles" / (candidate.bundle_id + ".json"), project_root=root).documents:
            matching = [event["snapshot"]["retrieved_at"] for event in events if event.get("event") == "fetched"
                        and event.get("http_status") == 200 and event.get("seed_id") == seed_id
                        and event["snapshot"]["source_url"] == ref.source_url and event["snapshot"]["content_hash"] == ref.content_hash]
            if matching:
                retrieval_times[ref.snapshot_id] = max(matching, key=datetime.fromisoformat)
        source_review = review_python_source(candidate, root / "data/runtime/bundles" / (candidate.bundle_id + ".json"),
                                            {**seed, "source_scope": item.get("source_scope")}, retrieval_times, project_root=root)
        score = score_source_evidence(candidate, assessment, evidence_checks_passed=True, source_review=source_review)
        claims = {name: {key: value for key, value in field.items() if key not in {"value_status", "source_evidence"}}
                  for name, field in item["fields"].items()}
        status = source_review["lifecycle_status"]
        packet = {"seed_id": seed_id, "source_scope": item.get("source_scope"), "rule_version": audit["rule_version"],
                  "candidate_path": item["candidate_path"], "candidate_sha256": item["candidate_sha256"],
                  "provenance_path": item["provenance_path"], "provenance_sha256": item["provenance_sha256"],
                  "bundle_id": item["bundle_id"], "assessment": item["assessment"],
                  "pilot_file": audit["pilot_file"], "pilot_sha256": sha256(pilot_bytes).hexdigest(),
                  "source_review": source_review,
                  "score": score, "lifecycle_status": status}
        from scholarship_intelligence.reuse import stamp
        stamp(packet, seed, root)
        details = _decode(baseline[ids[0]])
        previous_packet = details.get("python_source_assessment")
        def stable_packet(value):
            if value is None:
                return None
            stable = json.loads(json.dumps(value))
            stable.pop("reuse_receipt", None)
            stable.get("source_review", {}).pop("maximum_source_age_days", None)
            stable.get("source_review", {}).pop("assessed_at", None)
            stable.get("score", {}).pop("source_age_days", None)
            return stable
        if stable_packet(previous_packet) != stable_packet(packet):
            details["python_source_assessment"] = packet
            updates[ids[0]] = json.dumps(details, ensure_ascii=False, allow_nan=False)
        results.append({"scholarship_id": ids[0], "scholarship_name": item["scholarship_name"],
                        "source_url": item["source_url"], "offering_key": item.get("offering_key",item["source_url"]), **score, "lifecycle_status": status,
                        "opening_date": claims["opening_date"]["value"],
                        "closing_date": claims["closing_date"]["value"], "cycle": claims["cycle"]["value"]})
    if apply:
        with connect_database(database) as connection:
            with transaction(connection):
                for identity, value in updates.items():
                    changed = connection.execute("UPDATE scholarships SET verification_details_json=? WHERE scholarship_id=? AND verification_details_json=?",
                                                 (value, identity, baseline[identity])).rowcount
                    if changed != 1:
                        raise ValueError("Stored review changed during scoring; transaction rolled back.")
    if not results:
        raise ValueError("No uniquely stored, evidence-valid scholarship is available for scoring.")
    values = [item["confidence_score"] for item in results]
    return {"operation": "seed_confidence", "seed_id": seed_id, "dry_run": not apply,
            "policy_version": score["policy_version"], "scholarships_scored": len(results),
            "unscored_records": len(audit["scholarships"]) - len(results), "evidence_failures": audit["counts"].get("failed", 0),
            "application_status_counts": dict(Counter(item["lifecycle_status"] for item in results)),
            "record_verification_counts": dict(Counter(item["verification_status"] for item in results)),
            "source_authenticity_counts": dict(Counter(item["source_authenticity_status"] for item in results)),
            "source_freshness_counts": dict(Counter(item["source_freshness"] for item in results)),
            "scores_at_or_above_95": sum(value >= 95 for value in values),
            "verified": sum(item["verification_status"] == "VERIFIED" for item in results),
            "score_minimum": min(values), "score_maximum": max(values),
            "score_average": round(sum(values) / len(values), 2), "scholarships": results,
            "database_rows_changed": len(updates) if apply else 0, "api_calls_made": 0,
            "exact_quote_matches": audit["counts"]["exact_quote_matches"],
            "stage": "dashboard_assessments_saved" if apply else "confidence_preview"}
