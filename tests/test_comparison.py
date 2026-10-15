"""Offline comparison regression checks. Synthetic fixtures remain temporary."""

from datetime import timedelta
from hashlib import sha256
import json
import shutil
import sqlite3
import unittest

import test_pipeline as fixtures
from scholarship_intelligence.comparison import compare_review
from scholarship_intelligence.confidence import score_candidate
from scholarship_intelligence.pipeline import persist_review_import
from scholarship_intelligence.semantic_assessment import SemanticAssessment, candidate_fingerprint
from scholarship_intelligence.models import ScholarshipExtraction


class ComparisonTests(unittest.TestCase):
    setUp = fixtures.PipelineTests.setUp
    save = fixtures.PipelineTests.save
    prepare = fixtures.PipelineTests.prepare

    def store(self):
        return persist_review_import(self.prepare(), database_path=self.database)["scholarship_id"]

    def compare(self, scholarship_id):
        return compare_review(self.directory, self.pilot, scholarship_id=scholarship_id,
                              database_path=self.database, project_root=self.root)

    def reassess(self, withheld=None, candidate=None):
        # Preserve files referenced by the old stored record.
        previous = self.directory
        self.directory = previous.with_name(previous.name + "-new")
        shutil.copytree(previous, self.directory)
        if candidate is not None:
            self.candidate = candidate
            self.save("candidate.json", candidate.model_dump(mode="json"))
        path = self.directory / "semantic-assessment-test.json"
        data = json.loads(path.read_bytes())
        data["candidate_sha256"] = sha256((self.directory / "candidate.json").read_bytes()).hexdigest()
        data["assessment"]["candidate_fingerprint"] = candidate_fingerprint(self.candidate)
        data["assessment"]["assessed_at"] = (self.when + timedelta(days=1)).isoformat()
        if withheld is not None:
            data["assessment"]["withheld_fields"] = withheld
        self.save(path.name, data)
        assessment = SemanticAssessment.model_validate(data["assessment"])
        self.save("confidence-test.json", {
            **score_candidate(self.candidate, assessment).to_dict(),
            "candidate_sha256": sha256((self.directory / "candidate.json").read_bytes()).hexdigest(),
            "assessment_sha256": sha256(path.read_bytes()).hexdigest(),
        })

    def test_exact_repeat_is_unchanged_without_writes(self):
        target = self.store()
        before = self.database.read_bytes()
        report = self.compare(target)
        self.assertEqual(report["action"], "unchanged")
        self.assertEqual(report["changed_fields"], [])
        self.assertEqual(report["blocked_fields"], [])
        self.assertEqual(report["database_rows_changed"], 0)
        self.assertEqual(self.database.read_bytes(), before)

    def test_new_assessment_is_not_a_source_fact_change(self):
        target = self.store()
        self.reassess()
        report = self.compare(target)
        self.assertEqual(report["action"], "no_accepted_fact_change")
        self.assertEqual(report["verification_status"], "REVIEW_REQUIRED")
        self.assertIsNone(report["confidence_score"])

    def test_withheld_known_field_is_kept_and_blocks_update(self):
        target = self.store()
        self.reassess(["eligibility", "provider"])
        report = self.compare(target)
        self.assertEqual(report["action"], "review_required")
        self.assertIn("provider", report["blocked_fields"])
        field = next(item for item in report["fields"] if item["field"] == "provider")
        self.assertEqual(field["disposition"], "withheld_keep_existing")
        self.assertEqual(field["stored_value"], "TEST Provider")

    def test_changed_stored_artifact_is_rejected(self):
        target = self.store()
        self.origin.write_bytes(self.origin.read_bytes() + b" ")
        with self.assertRaises(ValueError):
            self.compare(target)

    def test_unreviewed_draft_cannot_enter_comparison(self):
        target = self.store()
        (self.directory / "semantic-assessment-test.json").unlink()
        with self.assertRaises(ValueError):
            self.compare(target)

    def test_target_id_is_required_and_not_inferred_from_name(self):
        self.store()
        with self.assertRaises(LookupError):
            self.compare(999)
        with self.assertRaises(ValueError):
            self.compare(True)

    def test_missing_database_is_never_created(self):
        missing = self.root / "absent.sqlite3"
        with self.assertRaises(sqlite3.Error):
            compare_review(self.directory, self.pilot, scholarship_id=1,
                           database_path=missing, project_root=self.root)
        self.assertFalse(missing.exists())

    def test_changed_claim_with_pending_checks_requires_review(self):
        target = self.store()
        payload = self.candidate.model_dump(mode="json")
        payload["documents"] = {"value": [{"name": "TEST Provider", "condition": None}],
            "missing_reason": None, "explanation": None, "evidence_ids": ["test-provider"],
            "inspected_snapshot_ids": payload["snapshot_ids"]}
        for item in payload["evidence"]:
            if item["evidence_id"] == "test-provider":
                item["field_paths"].append("documents")
        self.reassess(candidate=ScholarshipExtraction.model_validate(payload))
        before = self.database.read_bytes()
        report = self.compare(target)
        self.assertEqual(report["action"], "review_required")
        self.assertEqual(report["changed_fields"], ["documents"])
        self.assertEqual(self.database.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
