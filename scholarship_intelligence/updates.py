"""Apply an explicitly selected, reviewed comparison atomically.

Current scope: fully assessed fact changes within an established offering.
Unknown-field removals, identity changes and metadata-only refreshes stay out
of this writer. The CLI defaults to a dry run; --apply is explicit.
"""

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory

from scholarship_intelligence.comparison import compare_review
from scholarship_intelligence.pipeline import prepare_review_import, _decode
from scholarship_intelligence.scholarship_storage import save_reviewed_candidate
from scholarship_intelligence.settings import DATABASE_PATH, PROJECT_ROOT
from scholarship_intelligence.storage import connect_database, initialize_database, transaction


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _match(expected, actual):
    # Accept either the complete library report or the concise CLI report.
    visible = {key: value for key, value in actual.items() if key != "fields"}
    supplied = {key: value for key, value in expected.items() if key != "fields"}
    if supplied != visible or ("fields" in expected and expected["fields"] != actual["fields"]):
        raise ValueError("Comparison changed or was altered; generate and inspect a new comparison.")


def _sources(connection, staged):
    source_map, snapshot_map = {}, {}
    for source in staged.execute("SELECT * FROM sources ORDER BY source_id"):
        current = connection.execute("SELECT * FROM sources WHERE url=?", (source["url"],)).fetchone()
        if current is None:
            columns = [name for name in source.keys() if name != "source_id"]
            source_id = connection.execute(
                "INSERT INTO sources(" + ",".join(columns) + ") VALUES (" + ",".join("?" for _ in columns) + ")",
                tuple(source[name] for name in columns),
            ).lastrowid
        else:
            source_id = current["source_id"]
            if current["source_type"] not in ("unknown", source["source_type"]):
                raise ValueError("Source classification conflicts with the stored source.")
            connection.execute("UPDATE sources SET source_type=? WHERE source_id=?", (source["source_type"], source_id))
            old_assessed = json.loads(current["authenticity_json"]).get("assessed_at")
            new_assessed = json.loads(source["authenticity_json"]).get("assessed_at")
            if new_assessed and (not old_assessed or datetime.fromisoformat(new_assessed) >= datetime.fromisoformat(old_assessed)):
                connection.execute("UPDATE sources SET authenticity_json=? WHERE source_id=?", (source["authenticity_json"], source_id))
            for field in ("last_attempt_at", "last_success_at"):
                if source[field] and (not current[field] or datetime.fromisoformat(source[field]) >= datetime.fromisoformat(current[field])):
                    connection.execute(f"UPDATE sources SET {field}=? WHERE source_id=?", (source[field], source_id))
            if source["last_attempt_at"] and (not current["last_attempt_at"] or datetime.fromisoformat(source["last_attempt_at"]) >= datetime.fromisoformat(current["last_attempt_at"])):
                connection.execute("UPDATE sources SET last_access_result=? WHERE source_id=?", (source["last_access_result"], source_id))
        source_map[source["source_id"]] = source_id
    for snapshot in staged.execute("SELECT * FROM source_snapshots ORDER BY snapshot_id"):
        source_id = source_map[snapshot["source_id"]]
        current = connection.execute(
            "SELECT * FROM source_snapshots WHERE source_id=? AND content_hash=? AND prepared_content_path=?",
            (source_id, snapshot["content_hash"], snapshot["prepared_content_path"]),
        ).fetchone()
        if current is None:
            snapshot_id = connection.execute(
                "INSERT INTO source_snapshots(source_id,retrieved_at,content_hash,raw_file_path,prepared_content_path) VALUES (?,?,?,?,?)",
                (source_id, snapshot["retrieved_at"], snapshot["content_hash"], snapshot["raw_file_path"], snapshot["prepared_content_path"]),
            ).lastrowid
        else:
            if current["raw_file_path"] != snapshot["raw_file_path"]:
                raise ValueError("Stored snapshot has a different raw path.")
            snapshot_id = current["snapshot_id"]
        snapshot_map[snapshot["snapshot_id"]] = snapshot_id
    return source_map, snapshot_map


def _copy_review(connection, staged, target, run_id, report):
    previous = connection.execute("SELECT * FROM scholarships WHERE scholarship_id=?", (target,)).fetchone()
    incoming = staged.execute("SELECT * FROM scholarships").fetchone()
    old_details = json.loads(previous["verification_details_json"])
    details = json.loads(incoming["verification_details_json"])
    # One bundle cannot be taken from a different stored offering.
    conflict = connection.execute(
        "SELECT scholarship_id FROM scholarships WHERE scholarship_id!=? AND "
        "json_extract(verification_details_json,'$.storage_identity.bundle_id')=?",
        (target, report["bundle_id"]),
    ).fetchone()
    if conflict:
        raise ValueError("Another offering already owns the incoming bundle.")
    source_map, snapshot_map = _sources(connection, staged)
    for relation in staged.execute("SELECT * FROM scholarship_sources"):
        connection.execute(
            "INSERT INTO scholarship_sources(scholarship_id,source_id,role) VALUES (?,?,?) ON CONFLICT DO NOTHING",
            (target, source_map[relation["source_id"]], relation["role"]),
        )
    old_evidence = [dict(row) for row in connection.execute("SELECT * FROM evidence WHERE scholarship_id=? AND is_current=1", (target,))]
    connection.execute("UPDATE evidence SET is_current=0 WHERE scholarship_id=?", (target,))
    new_evidence = []
    for evidence in staged.execute("SELECT * FROM evidence ORDER BY evidence_id"):
        evidence_id = connection.execute(
            "INSERT INTO evidence(scholarship_id,field_name,snapshot_id,quote,location_json,check_result_json,is_current) VALUES (?,?,?,?,?,?,?)",
            (target, evidence["field_name"], snapshot_map[evidence["snapshot_id"]], evidence["quote"],
             evidence["location_json"], evidence["check_result_json"], evidence["is_current"]),
        ).lastrowid
        new_evidence.append({**dict(evidence), "evidence_id": evidence_id})
    timestamp = _now()
    for field in report["fields"]:
        if field["disposition"] != "proposed_change":
            continue
        name = field["field"]
        def ids(rows):
            return [item["evidence_id"] for item in rows if item["is_current"] == 1 and item["field_name"].split(".", 1)[0] == name]
        old_ids, new_ids = ids(old_evidence), ids(new_evidence)
        if field["stored_value"] is not None and not old_ids:
            raise ValueError("A previous accepted fact has no current evidence to preserve in history.")
        if not new_ids:
            raise ValueError("A changed fact has no current supporting evidence.")
        connection.execute(
            "INSERT INTO changes(scholarship_id,field_name,old_value_json,new_value_json,detected_at,old_evidence_ids_json,new_evidence_ids_json,reason) VALUES (?,?,?,?,?,?,?,?)",
            (target, name, _json(field["stored_value"]), _json(field["proposed_value"]), timestamp,
             _json(old_ids), _json(new_ids),
             ("Reviewed correction of the same source bundle; no website change inferred."
              if old_details["storage_identity"]["bundle_id"] == report["bundle_id"]
              else "Reviewed source update for the established offering; source evidence and critical assessment rechecked.")),
        )
    history = list(old_details.get("update_history", []))
    history.append({"replaced_at": timestamp, "previous_review": {
        key: value for key, value in old_details.items() if key != "update_history"
    }})
    details.update({"storage_version": "reviewed-update-storage-v1", "run_id": run_id,
                    "stored_at": timestamp, "update_history": history,
                    "comparison_baseline": report["baseline_fingerprint"]})
    columns = [name for name in incoming.keys() if name not in {
        "scholarship_id", "programme_id", "verification_details_json", "last_changed_at"
    }]
    connection.execute(
        "UPDATE scholarships SET " + ",".join(name + "=?" for name in columns) +
        ",verification_details_json=?,last_changed_at=? WHERE scholarship_id=?",
        tuple(incoming[name] for name in columns) + (_json(details), timestamp, target),
    )


def apply_review_update(review_directory, pilot_file, expected_comparison, *,
                        database_path=DATABASE_PATH, project_root=PROJECT_ROOT,
                        assessment_file=None, confidence_file=None):
    """Revalidate and update under one reserved transaction; no API calls."""
    if not isinstance(expected_comparison, dict):
        raise TypeError("An inspected comparison report is required.")
    target = expected_comparison.get("scholarship_id")
    options = dict(scholarship_id=target, database_path=database_path, project_root=project_root,
                   assessment_file=assessment_file, confidence_file=confidence_file)
    report = compare_review(review_directory, pilot_file, **options)
    _match(expected_comparison, report)
    if report["action"] in {"unchanged", "no_accepted_fact_change"}:
        return {"action": report["action"], "scholarship_id": target,
                "accepted_fact_changes": 0, "database_rows_changed": 0, "api_calls_made": 0}
    if report["action"] != "proposed_update":
        raise ValueError("Comparison requires review or identity resolution; updates are blocked.")
    database = Path(database_path).resolve()
    # Stage the incoming record with the already-tested initial writer. Temporary
    # IDs and its programme stay in this scratch database, never in production.
    with TemporaryDirectory(prefix="scholarship-update-", dir=database.parent) as directory:
        scratch = initialize_database(Path(directory) / "stage.sqlite3")
        with connect_database(database) as connection:
            with transaction(connection):
                report = compare_review(review_directory, pilot_file, **options)
                _match(expected_comparison, report)
                prepared = prepare_review_import(review_directory, pilot_file,
                    assessment_file=assessment_file, confidence_file=confidence_file, project_root=project_root)
                with connect_database(scratch) as staged:
                    with transaction(staged):
                        stage_run = staged.execute("INSERT INTO crawl_runs(trigger,started_at) VALUES ('command',?)", (_now(),)).lastrowid
                        save_reviewed_candidate(staged, prepared.candidate, prepared.assessment, prepared.bundle_path,
                            run_id=stage_run, generation_config_hash=prepared.generation_config_hash,
                            retrieval_times=prepared.retrieval_times, source_types=prepared.source_types,
                            artifact_paths=prepared.artifact_paths, project_root=prepared.project_root)
                    # Recheck the exact proposal after staging as well.
                    _match(expected_comparison, compare_review(review_directory, pilot_file, **options))
                    run_id = connection.execute("INSERT INTO crawl_runs(trigger,started_at,summary_json) VALUES ('command',?,?)",
                        (_now(), _json({"operation": "reviewed_update", "api_calls_made": 0}))).lastrowid
                    _copy_review(connection, staged, target, run_id, report)
                if connection.execute("PRAGMA foreign_key_check").fetchall():
                    raise ValueError("Foreign-key validation failed after update.")
                outcome = {"action": "updated", "scholarship_id": target, "run_id": run_id,
                           "accepted_fact_changes": len(report["changed_fields"]),
                           "changed_fields": report["changed_fields"], "api_calls_made": 0}
                connection.execute("UPDATE crawl_runs SET status='COMPLETED',finished_at=?,summary_json=? WHERE run_id=?",
                    (_now(), _json({**outcome, "operation": "reviewed_update", "live_crawl_performed": False}), run_id))
        return outcome


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--review-dir", required=True)
    parser.add_argument("--pilot", required=True)
    parser.add_argument("--comparison-report", type=Path, required=True)
    parser.add_argument("--database", type=Path, default=DATABASE_PATH)
    parser.add_argument("--assessment")
    parser.add_argument("--confidence")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    try:
        expected = _decode(args.comparison_report.read_text(encoding="utf-8-sig"))
        options = dict(database_path=args.database, assessment_file=args.assessment, confidence_file=args.confidence)
        if args.apply:
            result = apply_review_update(args.review_dir, args.pilot, expected, **options)
        else:
            report = compare_review(args.review_dir, args.pilot, scholarship_id=expected.get("scholarship_id"), **options)
            _match(expected, report)
            result = {"action": report["action"], "dry_run": True,
                      "changed_fields": report["changed_fields"], "database_rows_changed": 0, "api_calls_made": 0}
        print(json.dumps(result, indent=2))
        return 0
    except (ValueError, TypeError, LookupError, OSError, sqlite3.Error) as error:
        print(json.dumps({"error_type": type(error).__name__, "reason": str(error)}, indent=2))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
