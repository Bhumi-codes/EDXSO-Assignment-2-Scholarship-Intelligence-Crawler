"""Transactional update tests using TEST records in temporary databases only."""

from datetime import timedelta
from hashlib import sha256
import json
import sqlite3
import unittest
from unittest.mock import patch

import test_pipeline as fixtures
import test_comparison as comparisons
from scholarship_intelligence import updates
from scholarship_intelligence.comparison import compare_review
from scholarship_intelligence.confidence import score_candidate
from scholarship_intelligence.models import ScholarshipExtraction
from scholarship_intelligence.pipeline import persist_review_import
from scholarship_intelligence.semantic_assessment import SemanticAssessment
from scholarship_intelligence.storage import connect_database, transaction


class UpdateTests(unittest.TestCase):
    setUp = fixtures.PipelineTests.setUp
    save = fixtures.PipelineTests.save
    prepare = fixtures.PipelineTests.prepare
    reassess = comparisons.ComparisonTests.reassess

    def store(self):
        self.target = persist_review_import(self.prepare(), database_path=self.database)["scholarship_id"]
        return self.target

    def plan(self):
        return compare_review(self.directory, self.pilot, scholarship_id=self.target,
                              database_path=self.database, project_root=self.root)

    def apply(self, plan):
        return updates.apply_review_update(self.directory, self.pilot, plan,
                                          database_path=self.database, project_root=self.root)

    def changed_package(self, fully_assessed=True):
        payload = self.candidate.model_dump(mode="json")
        payload["documents"] = {"value": [{"name": "TEST Provider", "condition": None}],
            "missing_reason": None, "explanation": None, "evidence_ids": ["test-provider"],
            "inspected_snapshot_ids": payload["snapshot_ids"]}
        for item in payload["evidence"]:
            if item["evidence_id"] == "test-provider": item["field_paths"].append("documents")
        self.reassess(withheld=[] if fully_assessed else ["eligibility"],
                      candidate=ScholarshipExtraction.model_validate(payload))
        if fully_assessed:
            path = self.directory / "semantic-assessment-test.json"
            data = json.loads(path.read_bytes())
            for check in data["assessment"]["checks"]:
                check["status"] = "pass"
                check["reason"] = "TEST synthetic assessment; not real verification evidence."
            self.save(path.name, data)
            assessment = SemanticAssessment.model_validate(data["assessment"])
            self.save("confidence-test.json", {
                **score_candidate(self.candidate, assessment).to_dict(),
                "candidate_sha256": sha256((self.directory / "candidate.json").read_bytes()).hexdigest(),
                "assessment_sha256": sha256(path.read_bytes()).hexdigest(),
            })

    def test_unchanged_packet_writes_no_rows(self):
        self.store()
        plan = self.plan()
        before = self.database.read_bytes()
        result = self.apply(plan)
        self.assertEqual(result["action"], "unchanged")
        self.assertEqual(result["database_rows_changed"], 0)
        self.assertEqual(self.database.read_bytes(), before)

    def test_update_preserves_ids_evidence_and_supported_change_history(self):
        self.store()
        with connect_database(self.database) as connection:
            old_ids = [row[0] for row in connection.execute("SELECT evidence_id FROM evidence")]
        self.changed_package()
        plan = self.plan()
        self.assertEqual(plan["action"], "proposed_update")
        result = self.apply(plan)
        self.assertEqual(result["accepted_fact_changes"], 2)
        with connect_database(self.database) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM programmes").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT count(*) FROM scholarships").fetchone()[0], 1)
            row = connection.execute("SELECT * FROM scholarships").fetchone()
            self.assertEqual(row["scholarship_id"], self.target)
            self.assertEqual(row["programme_id"], 1)
            self.assertIsNotNone(row["last_changed_at"])
            history = list(connection.execute("SELECT * FROM changes ORDER BY change_id"))
            self.assertEqual({item["field_name"] for item in history}, {"eligibility", "documents"})
            for item in history:
                self.assertIn("same source bundle", item["reason"])
                self.assertIsNone(json.loads(item["old_value_json"]))
                self.assertIsNotNone(json.loads(item["new_value_json"]))
                new_ids = json.loads(item["new_evidence_ids_json"])
                self.assertTrue(new_ids)
                for evidence_id in new_ids:
                    evidence = connection.execute("SELECT * FROM evidence WHERE evidence_id=?", (evidence_id,)).fetchone()
                    self.assertEqual(evidence["scholarship_id"], self.target)
                    self.assertEqual(evidence["is_current"], 1)
            for evidence_id in old_ids:
                self.assertEqual(connection.execute("SELECT is_current FROM evidence WHERE evidence_id=?", (evidence_id,)).fetchone()[0], 0)
            details = json.loads(row["verification_details_json"])
            self.assertEqual(len(details["update_history"]), 1)
            self.assertIn("eligibility", details["update_history"][0]["previous_review"]["draft_claims"])
            self.assertFalse(connection.execute("PRAGMA foreign_key_check").fetchall())
        again = self.apply(self.plan())
        self.assertEqual(again["action"], "unchanged")
        with connect_database(self.database) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM changes").fetchone()[0], 2)

    def test_pending_critical_checks_block_update(self):
        self.store()
        self.changed_package(fully_assessed=False)
        plan = self.plan()
        before = self.database.read_bytes()
        with self.assertRaises(ValueError): self.apply(plan)
        self.assertEqual(self.database.read_bytes(), before)

    def test_stale_database_baseline_is_rejected(self):
        self.store()
        self.changed_package()
        plan = self.plan()
        with connect_database(self.database) as connection:
            with transaction(connection):
                connection.execute("UPDATE scholarships SET lifecycle_status='NO_LONGER_VERIFIABLE'")
        before = self.database.read_bytes()
        with self.assertRaises(ValueError): self.apply(plan)
        self.assertEqual(self.database.read_bytes(), before)

    def test_snapshot_metadata_change_invalidates_baseline(self):
        self.store()
        self.changed_package()
        plan = self.plan()
        with connect_database(self.database) as connection:
            with transaction(connection):
                connection.execute("UPDATE source_snapshots SET retrieved_at=?", ((self.when + timedelta(hours=1)).isoformat(),))
        with self.assertRaises(ValueError): self.apply(plan)

    def test_crawl_metadata_change_invalidates_review_context(self):
        self.store()
        self.changed_package()
        plan = self.plan()
        data = json.loads(self.pilot.read_bytes())
        data["snapshot"]["retrieved_at"] = (self.when + timedelta(hours=1)).isoformat()
        self.pilot.write_text(json.dumps(data) + "\n", encoding="utf-8")
        with self.assertRaises(ValueError): self.apply(plan)

    def test_failure_after_evidence_insert_rolls_back_everything(self):
        self.store()
        self.changed_package()
        plan = self.plan()
        original = updates._copy_review
        def fail_after_copy(*args):
            original(*args)
            raise RuntimeError("TEST failure after all update operations")
        before = self.database.read_bytes()
        with patch.object(updates, "_copy_review", side_effect=fail_after_copy):
            with self.assertRaises(RuntimeError): self.apply(plan)
        self.assertEqual(self.database.read_bytes(), before)

    def test_late_history_constraint_failure_rolls_back(self):
        self.store()
        self.changed_package()
        with connect_database(self.database) as connection:
            connection.execute("CREATE TRIGGER test_late_failure BEFORE INSERT ON changes BEGIN SELECT RAISE(ABORT,'TEST late failure'); END")
        plan = self.plan()
        before = self.database.read_bytes()
        with self.assertRaises(sqlite3.IntegrityError): self.apply(plan)
        self.assertEqual(self.database.read_bytes(), before)

    def test_altered_proposal_is_rejected(self):
        self.store()
        self.changed_package()
        plan = self.plan()
        plan["changed_fields"] = []
        with self.assertRaises(ValueError): self.apply(plan)

    def test_unknown_value_cannot_clear_an_accepted_fact(self):
        self.store()
        self.changed_package()
        self.apply(self.plan())
        payload = self.candidate.model_dump(mode="json")
        payload["documents"] = {"value": None, "missing_reason": "not_specified_in_inspected_material",
            "explanation": "TEST unknown value must not erase a fact", "evidence_ids": [],
            "inspected_snapshot_ids": payload["snapshot_ids"]}
        self.reassess(candidate=ScholarshipExtraction.model_validate(payload))
        report = self.plan()
        self.assertIn("documents", report["blocked_fields"])
        before = self.database.read_bytes()
        with self.assertRaises(ValueError): self.apply(report)
        self.assertEqual(self.database.read_bytes(), before)

    def test_source_category_conflict_rolls_back_run(self):
        self.store()
        self.changed_package()
        with connect_database(self.database) as connection:
            with transaction(connection): connection.execute("UPDATE sources SET source_type='university'")
        report = self.plan()
        before = self.database.read_bytes()
        with self.assertRaises(ValueError): self.apply(report)
        self.assertEqual(self.database.read_bytes(), before)

    def test_different_known_cycle_requires_offering_identity_review(self):
        payload = self.candidate.model_dump(mode="json")
        payload["cycle"] = {"value": "TEST Cycle A", "missing_reason": None,
            "explanation": None, "evidence_ids": ["test-provider"],
            "inspected_snapshot_ids": payload["snapshot_ids"]}
        for item in payload["evidence"]:
            if item["evidence_id"] == "test-provider": item["field_paths"].append("cycle")
        self.reassess(candidate=ScholarshipExtraction.model_validate(payload))
        self.store()
        self.changed_package()
        payload = self.candidate.model_dump(mode="json")
        payload["cycle"]["value"] = "TEST Cycle B"
        self.reassess(withheld=[], candidate=ScholarshipExtraction.model_validate(payload))
        report = self.plan()
        self.assertEqual(report["action"], "identity_review_required")
        with self.assertRaises(ValueError): self.apply(report)

    def test_second_revision_links_old_and_new_evidence(self):
        self.store()
        self.changed_package()
        self.apply(self.plan())
        payload = self.candidate.model_dump(mode="json")
        payload["documents"]["value"][0]["condition"] = "TEST synthetic revised condition"
        self.reassess(withheld=[], candidate=ScholarshipExtraction.model_validate(payload))
        result = self.apply(self.plan())
        self.assertEqual(result["accepted_fact_changes"], 1)
        with connect_database(self.database) as connection:
            changes = list(connection.execute("SELECT * FROM changes ORDER BY change_id"))
            self.assertEqual(len(changes), 3)
            last = changes[-1]
            self.assertTrue(json.loads(last["old_evidence_ids_json"]))
            self.assertTrue(json.loads(last["new_evidence_ids_json"]))
            details = json.loads(connection.execute("SELECT verification_details_json FROM scholarships").fetchone()[0])
            self.assertEqual(len(details["update_history"]), 2)


if __name__ == "__main__":
    unittest.main()
