"""Initial offering persistence; reviewed updates need a comparison path."""

from dataclasses import dataclass
from datetime import date, datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import re
import sqlite3
from typing import Mapping

from scholarship_intelligence.bundles import load_bundle
from scholarship_intelligence.confidence import score_candidate
from scholarship_intelligence.evidence_validation import validate_evidence
from scholarship_intelligence.models import ScholarshipExtraction
from scholarship_intelligence.semantic_assessment import SemanticAssessment
from scholarship_intelligence.settings import PROJECT_ROOT


SOURCE_TYPES = {
    "government", "university", "corporate_csr", "international",
    "ngo_trust", "aggregator", "other", "unknown",
}


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def reviewed_lifecycle(accepted_claims, verification_status, *, today=None):
    """Only a reviewed student application window establishes availability."""
    if verification_status != "VERIFIED":
        return "REVIEW_REQUIRED"
    today = today or datetime.now(timezone.utc).date()
    opening = accepted_claims["opening_date"]["value"]
    closing = accepted_claims["closing_date"]["value"]
    if closing is None or closing["stage"] != "student_application":
        return "REVIEW_REQUIRED"
    end = date.fromisoformat(closing["calendar_date"])
    if closing.get("route") or closing.get("time_qualifier"):
        return "REVIEW_REQUIRED"
    if opening is not None and (opening.get("stage") != "student_application" or opening.get("route") or opening.get("time_qualifier") or date.fromisoformat(opening["calendar_date"]) > end):
        return "REVIEW_REQUIRED"
    if end < today:
        return "EXPIRED"
    if opening is None or opening["stage"] != "student_application":
        return "REVIEW_REQUIRED"
    # Do not turn a route-specific or time-qualified deadline into a blanket
    # assertion about all applications.
    if any(value.get("route") or value.get("time_qualifier") for value in (opening, closing)):
        return "REVIEW_REQUIRED"
    start = date.fromisoformat(opening["calendar_date"])
    return "ACTIVE" if start <= today < end else "REVIEW_REQUIRED"


def _time(value):
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Retrieval times must be timezone-aware datetimes.")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


@dataclass(frozen=True)
class StoredOffering:
    programme_id: int
    scholarship_id: int
    inserted: bool
    evidence_rows: int


def save_reviewed_candidate(
    connection: sqlite3.Connection,
    candidate: ScholarshipExtraction,
    assessment: SemanticAssessment,
    bundle_file_path: str | Path,
    *,
    run_id: int,
    generation_config_hash: str,
    retrieval_times: Mapping[str, datetime],
    source_types: Mapping[str, str],
    artifact_paths: Mapping[str, str] | None = None,
    project_root: Path = PROJECT_ROOT,
) -> StoredOffering:
    """Insert one reviewed offering and evidence in the caller's transaction.

    Exact repeat imports reuse the existing record. A different reviewed
    result for an already imported bundle raises rather than silently
    overwriting facts. Programme identity is not inferred from names alone.
    Withheld claims stay in review details and do not enter fact columns.
    """
    if not connection.in_transaction:
        raise RuntimeError("Scholarship writes require an active transaction.")
    if connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
        raise RuntimeError("Foreign-key enforcement is required.")
    run = connection.execute("SELECT status FROM crawl_runs WHERE run_id = ?", (run_id,)).fetchone()
    if run is None or run[0] != "RUNNING":
        raise ValueError("Storage requires an existing RUNNING pipeline run.")
    if not isinstance(generation_config_hash, str) or re.fullmatch(r"[0-9a-f]{64}", generation_config_hash) is None:
        raise ValueError("Original generation configuration hash is required.")
    root = Path(project_root).resolve()
    score = score_candidate(candidate, assessment)
    report = validate_evidence(candidate, bundle_file_path, project_root=root)
    if not report.evidence_checks_passed:
        raise ValueError("Candidate failed evidence validation.")
    bundle = load_bundle(bundle_file_path, project_root=root)
    ids = {ref.snapshot_id for ref in bundle.documents}
    urls = {ref.source_url for ref in bundle.documents}
    if score.verification_status == "VERIFIED" and (
        candidate.official_source.value is None
        or str(candidate.official_source.value) not in urls
    ):
        raise ValueError("Verified storage requires an established official source in this bundle.")
    if set(retrieval_times) != ids or set(source_types) != urls:
        raise ValueError("Retrieval/source metadata must match all bundle documents exactly.")
    times = {key: _time(value) for key, value in retrieval_times.items()}
    if any(kind not in SOURCE_TYPES for kind in source_types.values()):
        raise ValueError("Unknown source category.")
    artifacts = {}
    artifact_root = (root / "data/runtime/extractions").resolve()
    for name, supplied_path in (artifact_paths or {}).items():
        path = (root / supplied_path).resolve()
        if not path.is_relative_to(artifact_root):
            raise ValueError("Review artifact is outside extraction storage.")
        artifacts[name] = {
            "path": path.relative_to(root).as_posix(),
            "sha256": sha256(path.read_bytes()).hexdigest(),
        }

    identity = {
        "bundle_id": bundle.bundle_id,
        "candidate_fingerprint": score.candidate_fingerprint,
        "assessment_fingerprint": score.assessment_fingerprint,
        "policy_fingerprint": score.policy_fingerprint,
        "generation_config_hash": generation_config_hash,
    }
    origin_key = "python_provenance"
    if origin_key not in artifacts:
        raise ValueError("Pinned Python extraction provenance is required.")
    origin = json.loads((root / artifacts[origin_key]["path"]).read_bytes())
    if origin.get("method") != "python_rules" or artifacts[origin_key]["sha256"] != generation_config_hash:
        raise ValueError("Python extraction identity differs from its provenance.")
    provenance = {**origin, "provider": None, "model": None}
    identity["generation_config_hash"] = None
    identity[origin_key + "_hash"] = generation_config_hash
    rows = connection.execute(
        "SELECT scholarship_id, programme_id, verification_details_json FROM scholarships "
        "WHERE json_extract(verification_details_json, '$.storage_identity.bundle_id') = ?",
        (bundle.bundle_id,),
    ).fetchall()
    if len(rows) > 1:
        raise ValueError("Multiple stored offerings claim the same import bundle.")
    if rows:
        details = json.loads(rows[0][2])
        if details.get("storage_identity") != identity:
            raise ValueError("This bundle already has a different reviewed result; use the update/comparison workflow.")
        if details.get("confidence") != score.to_dict() or details.get("critical_checks_passed") != score.critical_checks_passed:
            raise ValueError("Stored review/scoring metadata differs from the pinned assessment.")
        for url, kind in source_types.items():
            source = connection.execute("SELECT source_type FROM sources WHERE url = ?", (url,)).fetchone()
            if source is None or source[0] != kind:
                raise ValueError("Repeat import source classification differs.")
        for artifact in details.get("artifacts", {}).values():
            path = (root / artifact["path"]).resolve()
            if not path.is_relative_to(artifact_root) or sha256(path.read_bytes()).hexdigest() != artifact["sha256"]:
                raise ValueError("A stored review artifact changed after import.")
        count = connection.execute("SELECT count(*) FROM evidence WHERE scholarship_id = ?", (rows[0][0],)).fetchone()[0]
        return StoredOffering(rows[0][1], rows[0][0], False, count)

    payload = candidate.model_dump(mode="json")
    withheld = set(assessment.withheld_fields)
    name = candidate.scholarship_name.value
    provider = candidate.provider.value
    if name is None or provider is None or withheld.intersection({"scholarship_name", "provider"}):
        raise ValueError("An initial programme needs supported name/provider values.")
    accepted_claims = {
        field: json.loads(_json(value)) for field, value in payload.items()
        if isinstance(value, dict) and "value" in value
    }
    drafts = {}
    for field in withheld:
        drafts[field] = accepted_claims[field]
        accepted_claims[field] = {
            **accepted_claims[field], "value": None,
            "missing_reason": "ambiguous_or_conflicting",
            "explanation": "Withheld pending review: " + "; ".join(assessment.blockers),
        }

    def fact(field):
        return accepted_claims[field]["value"]

    def json_fact(field):
        value = fact(field)
        return None if value is None else _json(value)

    def student_date(field):
        value = fact(field)
        return value["calendar_date"] if value is not None and value["stage"] == "student_application" else None

    lifecycle = reviewed_lifecycle(accepted_claims, score.verification_status)
    now = datetime.now(timezone.utc).isoformat(timespec="microseconds")
    details = {
        "storage_version": "initial-offering-storage-v1",
        "storage_identity": identity, "stored_at": now, "run_id": run_id,
        "artifacts": artifacts, "extraction_provenance": provenance,
        "critical_checks_passed": score.critical_checks_passed,
        "blockers": list(score.blockers), "withheld_fields": sorted(withheld),
        "missing_reasons": {
            field: {"reason": claim["missing_reason"], "explanation": claim["explanation"]}
            for field, claim in accepted_claims.items() if claim["value"] is None
        },
        "accepted_claims": accepted_claims, "draft_claims": drafts,
        "assessment": assessment.model_dump(mode="json"),
        "confidence": score.to_dict(), "evidence_report": report.to_dict(),
        "availability": lifecycle,
    }
    programme_id = connection.execute(
        "INSERT INTO programmes(name, provider_name) VALUES (?, ?)", (name, provider)
    ).lastrowid
    scholarship_id = connection.execute(
        """INSERT INTO scholarships (
            programme_id, cycle, benefit_json, eligibility_json,
            opening_date, closing_date, documents_json, selection_process,
            renewal_json, lifecycle_status, verification_status,
            confidence_score, verification_details_json, last_verified_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            programme_id, fact("cycle"), json_fact("benefits"), json_fact("eligibility"),
            student_date("opening_date"), student_date("closing_date"),
            json_fact("documents"), fact("selection_process"), json_fact("renewal_requirements"),
            lifecycle, score.verification_status,
            None if score.confidence_score is None else float(score.confidence_score),
            _json(details), score.last_verified_at,
        ),
    ).lastrowid

    primary_url = str(candidate.official_source.value) if candidate.official_source.value is not None else None
    ownership = next(check for check in assessment.checks if check.check_id == "primary_source_ownership")
    snapshot_ids = {}
    for ref in bundle.documents:
        primary = ref.source_url == primary_url and ownership.status == "pass" and "official_source" not in withheld
        authenticity = {
            "status": "primary_source_established" if primary else "unresolved",
            "basis": [ownership.reason] if primary else [],
            "assessment_evaluator": assessment.evaluator,
            "assessed_at": assessment.assessed_at.isoformat(),
        }
        connection.execute(
            "INSERT INTO sources(url, source_type, authenticity_json) VALUES (?, ?, ?) "
            "ON CONFLICT(url) DO NOTHING",
            (ref.source_url, source_types[ref.source_url], _json(authenticity)),
        )
        source = connection.execute("SELECT * FROM sources WHERE url = ?", (ref.source_url,)).fetchone()
        source_id = source["source_id"]
        if source["source_type"] not in ("unknown", source_types[ref.source_url]):
            raise ValueError("Existing source has a different category.")
        connection.execute("UPDATE sources SET source_type = ? WHERE source_id = ?", (source_types[ref.source_url], source_id))
        if primary:
            connection.execute("UPDATE sources SET authenticity_json = ? WHERE source_id = ?", (_json(authenticity), source_id))
        retrieved = times[ref.snapshot_id]
        for column in ("last_attempt_at", "last_success_at"):
            old = source[column]
            if old is None or datetime.fromisoformat(old) <= datetime.fromisoformat(retrieved):
                connection.execute(f"UPDATE sources SET {column} = ? WHERE source_id = ?", (retrieved, source_id))
        if source["last_attempt_at"] is None or datetime.fromisoformat(source["last_attempt_at"]) <= datetime.fromisoformat(retrieved):
            connection.execute("UPDATE sources SET last_access_result = ? WHERE source_id = ?", ("HTTP 200; saved crawl response imported", source_id))
        connection.execute(
            "INSERT INTO source_snapshots(source_id, retrieved_at, content_hash, raw_file_path, prepared_content_path) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT(source_id, content_hash, prepared_content_path) DO NOTHING",
            (source_id, retrieved, ref.content_hash, ref.raw_file_path, ref.prepared_file_path),
        )
        snapshot = connection.execute(
            "SELECT snapshot_id, raw_file_path FROM source_snapshots WHERE source_id = ? AND content_hash = ? AND prepared_content_path = ?",
            (source_id, ref.content_hash, ref.prepared_file_path),
        ).fetchone()
        if snapshot["raw_file_path"] != ref.raw_file_path:
            raise ValueError("Stored snapshot points to a different raw path.")
        snapshot_ids[ref.snapshot_id] = snapshot["snapshot_id"]
        connection.execute(
            "INSERT INTO scholarship_sources(scholarship_id, source_id, role) VALUES (?, ?, ?)",
            (scholarship_id, source_id, "primary" if primary else "discovery"),
        )

    evidence_rows = 0
    for evidence in candidate.evidence:
        if len(evidence.field_paths) != len(set(evidence.field_paths)):
            raise ValueError("Duplicate evidence field paths.")
        for field_path in evidence.field_paths:
            field = field_path.split(".", 1)[0]
            accepted = field not in withheld and fact(field) is not None
            check_result = {
                "local_evidence_id": evidence.evidence_id,
                "local_snapshot_id": evidence.snapshot_id,
                "exact_quote_checked": True,
                "validator_version": report.validator_version,
                "accepted_field": accepted, "withheld": field in withheld,
                "candidate_fingerprint": score.candidate_fingerprint,
                "assessed_semantics": "pending" if field in withheld else "reviewed",
            }
            connection.execute(
                "INSERT INTO evidence(scholarship_id, field_name, snapshot_id, quote, location_json, check_result_json, is_current) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (scholarship_id, field_path, snapshot_ids[evidence.snapshot_id], evidence.quote,
                 _json(evidence.location.model_dump(mode="json")), _json(check_result), int(accepted)),
            )
            evidence_rows += 1
    return StoredOffering(programme_id, scholarship_id, True, evidence_rows)
