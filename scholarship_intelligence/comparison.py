"""Read-only comparison of a validated review package with a stored offering.

This prepares a proposal; it does not implement or authorize database updates.
An explicit target ID is required. Names alone never identify an offering.
"""

import argparse
from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path
import sqlite3

from scholarship_intelligence.confidence import score_candidate
from scholarship_intelligence.pipeline import prepare_review_import
from scholarship_intelligence.settings import DATABASE_PATH, PROJECT_ROOT


def _fingerprint(value):
    return sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()


def compare_source_values(before, after, *, official_notice_pair=None):
    """Observed source facts, distinct from accepted database updates."""
    from scholarship_intelligence.source_profiles import programme_key
    from pydantic import HttpUrl
    source_pair = (str(before.official_source.value), str(after.official_source.value))
    expected_pair = tuple(str(HttpUrl(url)) for url in official_notice_pair) if official_notice_pair is not None else None
    replay_pair = (official_notice_pair is not None and expected_pair == source_pair
                   and len(set(source_pair)) == 2 and before.cycle.value is not None
                   and before.cycle.value == after.cycle.value
                   and before.closing_date.value is not None and after.closing_date.value is not None
                   and before.closing_date.value.stage == after.closing_date.value.stage == "student_application"
                   and before.closing_date.value.route == after.closing_date.value.route)
    if official_notice_pair is not None and not replay_pair:
        raise ValueError("Official replay requires the explicit ordered notice pair, same cycle and student route.")
    if (programme_key(before.scholarship_name.value) != programme_key(after.scholarship_name.value)
            or before.provider.value != after.provider.value
            or (source_pair[0] != source_pair[1] and not replay_pair)):
        raise ValueError("Source comparison requires the same programme, provider and source URL.")
    def canonical(value):
        if isinstance(value, str):
            return " ".join(value.split())
        if isinstance(value, list):
            return [canonical(item) for item in value]
        if isinstance(value, dict):
            return {key: canonical(item) for key, item in value.items()}
        return value
    old, new = before.model_dump(mode="json"), after.model_dump(mode="json")
    changes = []
    for field in old:
        if not isinstance(old[field], dict) or "value" not in old[field]:
            continue
        if replay_pair and field == "official_source":
            continue  # Different notice URLs are evidence versions, not a scholarship fact change.
        a, b = old[field]["value"], new[field]["value"]
        if a is not None and b is not None and canonical(a) != canonical(b):
            changes.append({"field": field, "old_value": a, "new_value": b,
                            "old_evidence_ids": old[field]["evidence_ids"], "new_evidence_ids": new[field]["evidence_ids"]})
    return changes


def compare_review(review_directory, pilot_file, *, scholarship_id,
                   assessment_file=None, confidence_file=None,
                   database_path=DATABASE_PATH, project_root=PROJECT_ROOT):
    if type(scholarship_id) is not int or scholarship_id < 1:
        raise ValueError("An explicit positive scholarship ID is required.")
    prepared = prepare_review_import(
        review_directory, pilot_file, assessment_file=assessment_file,
        confidence_file=confidence_file, project_root=project_root,
    )
    candidate = prepared.candidate
    score = score_candidate(candidate, prepared.assessment)
    database = Path(database_path).resolve()
    connection = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("BEGIN")  # One consistent read snapshot for the baseline.
        row = connection.execute(
            "SELECT s.*, p.name, p.provider_name FROM scholarships s JOIN programmes p "
            "ON p.programme_id=s.programme_id WHERE s.scholarship_id=?", (scholarship_id,),
        ).fetchone()
        if row is None:
            raise LookupError("Target scholarship does not exist.")
        source_rows = [dict(item) for item in connection.execute(
            "SELECT src.*, ss.role FROM sources src JOIN scholarship_sources ss "
            "ON ss.source_id=src.source_id WHERE ss.scholarship_id=? ORDER BY src.source_id, ss.role",
            (scholarship_id,),
        ).fetchall()]
        evidence_rows = [dict(item) for item in connection.execute(
            "SELECT * FROM evidence WHERE scholarship_id=? ORDER BY evidence_id", (scholarship_id,),
        ).fetchall()]
        snapshot_rows = [dict(item) for item in connection.execute(
            "SELECT DISTINCT snap.* FROM source_snapshots snap JOIN evidence ev "
            "ON ev.snapshot_id=snap.snapshot_id WHERE ev.scholarship_id=? ORDER BY snap.snapshot_id",
            (scholarship_id,),
        ).fetchall()]
        change_rows = [dict(item) for item in connection.execute(
            "SELECT * FROM changes WHERE scholarship_id=? ORDER BY change_id", (scholarship_id,),
        ).fetchall()]
    finally:
        connection.close()
    details = json.loads(row["verification_details_json"])
    old_claims = details.get("accepted_claims")
    if not isinstance(old_claims, dict):
        raise ValueError("Target does not contain supported claim comparison metadata.")
    # Stored review artifacts remain immutable and checked before comparison.
    root = Path(project_root).resolve()
    artifact_root = (root / "data/runtime/extractions").resolve()
    artifact_sets = [details.get("artifacts", {})]
    artifact_sets.extend(item["previous_review"].get("artifacts", {})
                         for item in details.get("update_history", []))
    for artifact in [item for group in artifact_sets for item in group.values()]:
        path = (root / artifact["path"]).resolve()
        if not path.is_relative_to(artifact_root) or sha256(path.read_bytes()).hexdigest() != artifact["sha256"]:
            raise ValueError("A stored review artifact changed after import.")

    identity = details.get("storage_identity", {})
    source = str(candidate.official_source.value) if candidate.official_source.value else None
    old_source = old_claims.get("official_source", {}).get("value")
    same_bundle = identity.get("bundle_id") == candidate.bundle_id
    same_cycle = candidate.cycle.value is not None and candidate.cycle.value == old_claims.get("cycle", {}).get("value")
    identity_reasons = []
    if source is None or source != old_source:
        identity_reasons.append("The reviewed official source differs or is unresolved.")
    if candidate.provider.value != row["provider_name"] or candidate.scholarship_name.value != row["name"]:
        identity_reasons.append("Name/provider changes require an explicit programme identity review.")
    if not same_bundle and not same_cycle:
        identity_reasons.append("A new bundle needs a supported matching cycle; an unknown or different cycle cannot be merged automatically.")
    stored_cycle = old_claims.get("cycle", {}).get("value")
    if stored_cycle is not None and candidate.cycle.value is not None and stored_cycle != candidate.cycle.value:
        identity_reasons.append("The known offering cycle differs; establish a separate offering or explicitly review the cycle correction.")
    if not same_bundle and snapshot_rows:
        latest_saved = max(datetime.fromisoformat(item["retrieved_at"]) for item in snapshot_rows)
        latest_incoming = max(prepared.retrieval_times.values())
        if latest_incoming < latest_saved:
            identity_reasons.append("Incoming source retrieval predates the stored offering; a historical bundle cannot automatically replace newer facts.")

    proposed_claims = candidate.model_dump(mode="json")
    withheld = set(prepared.assessment.withheld_fields)
    fields, changed, blocked = [], [], []
    for field, old in old_claims.items():
        new = proposed_claims.get(field)
        if not isinstance(new, dict) or "value" not in new:
            raise ValueError("Candidate is missing a stored claim field.")
        previous, proposed = old["value"], new["value"]
        if field in withheld:
            disposition = "withheld_keep_existing"
            if previous is not None:
                blocked.append(field)
        elif proposed is None and previous is not None:
            disposition = "unknown_keep_existing"
            blocked.append(field)
        elif previous == proposed:
            disposition = "unchanged"
        else:
            disposition = "proposed_change"
            changed.append(field)
        fields.append({"field": field, "disposition": disposition,
                       "stored_value": previous, "proposed_value": proposed,
                       "evidence_ids": new["evidence_ids"],
                       "missing_reason": new["missing_reason"]})

    expected_identity = {
        "bundle_id": candidate.bundle_id,
        "candidate_fingerprint": score.candidate_fingerprint,
        "assessment_fingerprint": score.assessment_fingerprint,
        "policy_fingerprint": score.policy_fingerprint,
        "generation_config_hash": prepared.generation_config_hash,
    }
    if prepared.extraction_method == "python_rules":
        expected_identity["generation_config_hash"] = None
        origin_key = "python_provenance"
        expected_identity[origin_key + "_hash"] = prepared.generation_config_hash
    exact_packet = identity == expected_identity
    if identity_reasons:
        action = "identity_review_required"
    elif exact_packet:
        action = "unchanged"
    elif blocked or (changed and not score.critical_checks_passed):
        action = "review_required"
    elif changed:
        action = "proposed_update"
    else:
        action = "no_accepted_fact_change"
    return {
        "comparison_version": "review-comparison-v2", "scholarship_id": scholarship_id,
        "bundle_id": candidate.bundle_id,
        "baseline_fingerprint": _fingerprint({"scholarship": dict(row), "sources": source_rows,
                                               "evidence": evidence_rows, "snapshots": snapshot_rows,
                                               "changes": change_rows}),
        "review_context_fingerprint": _fingerprint({
            "generation_config_hash": prepared.generation_config_hash,
            "retrieval_times": {key: value.isoformat() for key, value in prepared.retrieval_times.items()},
            "source_types": prepared.source_types,
            "artifacts": {name: {"path": supplied, "sha256": sha256((root / supplied).read_bytes()).hexdigest()}
                          for name, supplied in prepared.artifact_paths.items()},
        }),
        "candidate_fingerprint": score.candidate_fingerprint,
        "assessment_fingerprint": score.assessment_fingerprint,
        "policy_fingerprint": score.policy_fingerprint,
        "action": action, "changed_fields": changed, "blocked_fields": blocked,
        "identity_review_reasons": identity_reasons, "fields": fields,
        "verification_status": score.verification_status,
        "confidence_score": score.to_dict()["confidence_score"],
        "blockers": list(score.blockers), "withheld_fields": sorted(withheld),
        "api_calls_made": 0, "database_rows_changed": 0,
        "comparison_is_read_only": True, "update_writer_available": True,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--review-dir", required=True)
    parser.add_argument("--pilot", required=True)
    parser.add_argument("--scholarship-id", type=int, required=True)
    parser.add_argument("--assessment")
    parser.add_argument("--confidence")
    parser.add_argument("--database", type=Path, default=DATABASE_PATH)
    args = parser.parse_args(argv)
    try:
        result = compare_review(args.review_dir, args.pilot, scholarship_id=args.scholarship_id,
                                assessment_file=args.assessment, confidence_file=args.confidence,
                                database_path=args.database)
        # Concise display; the library result retains the full field proposal.
        print(json.dumps({key: value for key, value in result.items() if key != "fields"}, indent=2))
        return 0
    except (ValueError, LookupError, OSError, sqlite3.Error) as error:
        print(json.dumps({"error_type": type(error).__name__, "reason": str(error)}, indent=2))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
