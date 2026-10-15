"""Offline integration tests. TEST fixtures never enter the project database."""

from copy import deepcopy
from contextlib import closing
from dataclasses import asdict, replace
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from scholarship_intelligence.bundles import create_bundle
from scholarship_intelligence.confidence import score_candidate
from scholarship_intelligence.models import ScholarshipExtraction
from scholarship_intelligence.pipeline import (
    inspect_scholarship, persist_review_import, prepare_review_import,
)
from scholarship_intelligence.preparation import prepare_html_snapshot
from scholarship_intelligence.semantic_assessment import (
    CHECK_DEFINITIONS, CheckResult, SemanticAssessment, candidate_fingerprint,
)
from scholarship_intelligence.snapshots import save_raw_snapshot
from scholarship_intelligence.storage import initialize_database


class PipelineTests(unittest.TestCase):






    def setUp(self):
        self.temporary = TemporaryDirectory(prefix="pipeline-test-", dir=Path(__file__).resolve().parent)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.when = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.snapshot = save_raw_snapshot(
            "https://example.test/scholarship/",
            b"<html><body><h1>TEST Scholarship</h1><p>TEST Provider</p>"
            b"<p>TEST eligibility requires first-year study.</p>"
            b"<a href='/apply'>Apply Now</a></body></html>",
            self.when, "text/html; charset=UTF-8", project_root=self.root,
        )
        prepared = prepare_html_snapshot(self.snapshot, 200, project_root=self.root)
        bundle = create_bundle([prepared.prepared_file_path], "TEST fixture only", project_root=self.root)
        self.directory = self.root / "data/runtime/extractions/test-run/reviewed"
        self.directory.mkdir(parents=True)
        self.pilot = self.root / "data/runtime/test-pilot.jsonl"
        self.pilot.write_text(json.dumps({
            "event": "fetched", "http_status": 200, "source_category": "unknown",
            "snapshot": asdict(self.snapshot),
        }) + "\n", encoding="utf-8")

        payload = {"schema_version": "1.0", "bundle_id": bundle.bundle_id,
                   "snapshot_ids": [prepared.snapshot_id], "evidence": []}
        for field in ScholarshipExtraction.model_fields:
            if field not in payload:
                payload[field] = {
                    "value": None, "missing_reason": "not_specified_in_inspected_material",
                    "explanation": "No value in this synthetic fixture.",
                    "evidence_ids": [], "inspected_snapshot_ids": [prepared.snapshot_id],
                }

        def claim(field, value, evidence_id, quote, block):
            payload[field] = {
                "value": value, "missing_reason": None, "explanation": None,
                "evidence_ids": [evidence_id], "inspected_snapshot_ids": [prepared.snapshot_id],
            }
            existing = next((item for item in payload["evidence"] if item["evidence_id"] == evidence_id), None)
            if existing:
                existing["field_paths"].append(field)
            else:
                payload["evidence"].append({
                    "evidence_id": evidence_id, "snapshot_id": prepared.snapshot_id, "quote": quote,
                    "location": {"block_ids": [block.block_id], "page_number": None, "section": None},
                    "field_paths": [field],
                })

        name = next(block for block in prepared.blocks if block.text == "TEST Scholarship")
        provider = next(block for block in prepared.blocks if block.text == "TEST Provider")
        eligibility = next(block for block in prepared.blocks if block.text == "TEST eligibility requires first-year study.")
        application = next(block for block in prepared.blocks if any(link.text == "Apply Now" for link in block.links))
        claim("scholarship_name", name.text, "test-name", name.text, name)
        claim("official_source", prepared.source_url, "test-name", name.text, name)
        claim("provider", provider.text, "test-provider", provider.text, provider)
        claim("application_url", "https://example.test/apply", "test-application", "Apply Now", application)
        claim("eligibility", {
            "summary": eligibility.text,
            "conditions": {"kind": "condition", "category": "course_level", "statement": eligibility.text, "numeric": None, "qualifiers": []},
            "exceptions": [],
        }, "test-eligibility", eligibility.text, eligibility)
        self.candidate = ScholarshipExtraction.model_validate(payload)
        self.save("candidate.json", self.candidate.model_dump(mode="json"))
        checks = []
        for check_id in CHECK_DEFINITIONS:
            status = "pass" if check_id in {
                "source_integrity", "evidence_integrity", "application_route_support",
            } else "pending"
            checks.append(CheckResult(check_id=check_id, status=status, reason="Synthetic fixture outcome, not real verification."))
        assessment = SemanticAssessment(
            bundle_id=bundle.bundle_id, snapshot_ids=(prepared.snapshot_id,),
            candidate_fingerprint=candidate_fingerprint(self.candidate),
            assessed_at=self.when, evaluator="TEST fixture only", checks=tuple(checks),
            withheld_fields=("eligibility",),
        )
        candidate_hash = sha256((self.directory / "candidate.json").read_bytes()).hexdigest()
        self.save("semantic-assessment-test.json", {
            "candidate_sha256": candidate_hash, "assessment": assessment.model_dump(mode="json"),
        })
        self.save("confidence-test.json", {
            **score_candidate(self.candidate, assessment).to_dict(),
            "candidate_sha256": candidate_hash,
            "assessment_sha256": sha256((self.directory / "semantic-assessment-test.json").read_bytes()).hexdigest(),
        })
        self.origin = self.directory.parent / "python-provenance.json"
        original = self.directory.parent / "candidate.json"
        original.write_bytes((self.directory / "candidate.json").read_bytes())
        self.origin.write_text(json.dumps({
            "version": "python-extraction-v1", "method": "python_rules",
            "author": "TEST rules", "author_kind": "software", "created_at": self.when.isoformat(),
            "bundle_id": self.candidate.bundle_id, "snapshot_ids": self.candidate.snapshot_ids,
            "candidate_sha256": sha256(original.read_bytes()).hexdigest(),
            "candidate_fingerprint": candidate_fingerprint(self.candidate), "api_calls_made": 0,
        }), encoding="utf-8")
        self.save("review.json", {
            "extraction_method": "python_rules", "candidate_correction_selected": True,
            "python_provenance_path": str(self.origin),
            "python_provenance_sha256": sha256(self.origin.read_bytes()).hexdigest(),
        })
        self.database = initialize_database(self.root / "test.sqlite3")

    def save(self, name, value):
        (self.directory / name).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")

    def prepare(self, **kwargs):
        return prepare_review_import(self.directory, self.pilot, project_root=self.root, **kwargs)

    def test_valid_preparation_makes_no_database_writes(self):
        prepared = self.prepare()
        self.assertEqual(prepared.summary()["verification_status"], "REVIEW_REQUIRED")
        self.assertIsNone(prepared.summary()["confidence_score"])
        self.assertEqual(prepared.retrieval_times[self.candidate.snapshot_ids[0]], self.when)
        with closing(sqlite3.connect(self.database)) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM scholarships").fetchone()[0], 0)

    def test_import_inspection_and_repeat_are_consistent(self):
        prepared = self.prepare()
        first = persist_review_import(prepared, database_path=self.database)
        second = persist_review_import(prepared, database_path=self.database)
        self.assertTrue(first["inserted"])
        self.assertFalse(second["inserted"])
        self.assertEqual(first["scholarship_id"], second["scholarship_id"])
        record = inspect_scholarship(first["scholarship_id"], database_path=self.database)
        self.assertEqual(record["verification_status"], "REVIEW_REQUIRED")
        self.assertIsNone(record["confidence_score"])
        self.assertFalse(record["accepted_eligibility_present"])
        self.assertTrue(record["draft_eligibility_present"])
        self.assertEqual(record["field_evidence_rows"], 5)
        self.assertEqual(record["distinct_evidence_entries"], 4)
        with closing(sqlite3.connect(self.database)) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM scholarships").fetchone()[0], 1)

    def test_changed_candidate_bytes_reject_old_assessment(self):
        path = self.directory / "candidate.json"
        path.write_bytes(path.read_bytes() + b" ")
        with self.assertRaises(ValueError): self.prepare()

    def test_fake_confidence_is_not_trusted(self):
        data = json.loads((self.directory / "confidence-test.json").read_bytes())
        data["confidence_score"] = 100
        data["verification_status"] = "VERIFIED"
        self.save("confidence-test.json", data)
        with self.assertRaises(ValueError): self.prepare()



    def test_missing_successful_crawl_event_is_rejected(self):
        self.pilot.write_text("", encoding="utf-8")
        with self.assertRaises(ValueError): self.prepare()

    def test_ambiguous_report_selection_requires_explicit_file(self):
        (self.directory / "confidence-copy.json").write_bytes((self.directory / "confidence-test.json").read_bytes())
        with self.assertRaises(ValueError): self.prepare()
        self.prepare(confidence_file=self.directory / "confidence-test.json")

    def test_paths_outside_runtime_storage_are_rejected(self):
        with self.assertRaises(ValueError):
            prepare_review_import(self.root, self.pilot, project_root=self.root)

    def test_failed_repeat_rolls_back_run_and_keeps_record(self):
        prepared = self.prepare()
        result = persist_review_import(prepared, database_path=self.database)
        changed = replace(prepared, source_types={url: "university" for url in prepared.source_types})
        with self.assertRaises(ValueError): persist_review_import(changed, database_path=self.database)
        with closing(sqlite3.connect(self.database)) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM crawl_runs").fetchone()[0], 1)
        self.assertEqual(inspect_scholarship(result["scholarship_id"], database_path=self.database)["verification_status"], "REVIEW_REQUIRED")

    def test_inspect_does_not_create_missing_database(self):
        missing = self.root / "missing.sqlite3"
        with self.assertRaises(sqlite3.Error): inspect_scholarship(1, database_path=missing)
        self.assertFalse(missing.exists())


class PDFTests(unittest.TestCase):
    """PDF fixtures remain in the existing isolated TEST pipeline database."""

    save = PipelineTests.save

    def setUp(self):
        from scholarship_intelligence import preparation
        PipelineTests.setUp(self)
        self.pdf_snapshot = save_raw_snapshot(
            "https://example.test/TEST-guidelines.pdf", self.pdf_bytes(),
            self.when, "application/pdf", project_root=self.root,
        )
        self.pdf = preparation.prepare_snapshot(self.pdf_snapshot, 200, project_root=self.root)
        self.bundle = create_bundle([self.pdf.prepared_file_path], "TEST PDF fixture", project_root=self.root)
        data = self.candidate.model_dump(mode="json")
        data["bundle_id"] = self.bundle.bundle_id
        data["snapshot_ids"] = [self.pdf.snapshot_id]
        data["official_source"]["value"] = self.pdf_snapshot.source_url
        for value in data.values():
            if isinstance(value, dict) and "inspected_snapshot_ids" in value:
                value["inspected_snapshot_ids"] = [self.pdf.snapshot_id]
        for entry in data["evidence"]:
            number = 2 if entry["quote"].startswith("TEST eligibility") else 1
            entry["snapshot_id"] = self.pdf.snapshot_id
            entry["location"] = {"block_ids": [f"p{number:06d}"], "page_number": number, "section": None}
        self.pdf_candidate = ScholarshipExtraction.model_validate(data)

    @staticmethod
    def pdf_bytes(*, drawing_only=False, add_blank=False):
        from io import BytesIO
        from pypdf import PdfWriter
        from pypdf.annotations import Link
        from pypdf.generic import DictionaryObject, NameObject, DecodedStreamObject
        writer = PdfWriter()
        for number in (1, 2):
            page = writer.add_blank_page(width=612, height=792)
            font = DictionaryObject({NameObject("/Type"): NameObject("/Font"),
                                     NameObject("/Subtype"): NameObject("/Type1"),
                                     NameObject("/BaseFont"): NameObject("/Helvetica")})
            page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})})
            stream = DecodedStreamObject()
            body = (b"BT /F1 12 Tf 40 740 Td (TEST Scholarship) Tj 0 -30 Td (TEST Provider) Tj 0 -30 Td (Apply Now) Tj ET"
                    if number == 1 else b"BT /F1 12 Tf 40 740 Td (TEST eligibility requires first-year study.) Tj ET")
            stream.set_data(b"40 40 100 100 re f" if number == 2 and drawing_only else body)
            page[NameObject("/Contents")] = writer._add_object(stream)
        writer.add_annotation(0, Link(rect=(40, 660, 140, 700), url="https://example.test/apply"))
        if add_blank:
            writer.add_blank_page(width=612, height=792)
        content = BytesIO()
        writer.write(content)
        return content.getvalue()

    def report(self, data=None):
        from scholarship_intelligence.evidence_validation import validate_evidence
        candidate = self.pdf_candidate if data is None else ScholarshipExtraction.model_validate(data)
        return validate_evidence(candidate, self.root / self.bundle.bundle_file_path, project_root=self.root)

    def test_pdf_retains_pages_links_and_reuses_identical_preparation(self):
        from scholarship_intelligence.preparation import prepare_snapshot
        self.assertEqual([block.page_number for block in self.pdf.blocks], [1, 2])
        self.assertIn("TEST Provider", self.pdf.blocks[0].text)
        self.assertEqual(self.pdf.blocks[0].links[0].url, "https://example.test/apply")
        self.assertEqual(self.pdf.blocks[0].links[0].text, "")
        self.assertEqual(self.pdf, prepare_snapshot(self.pdf_snapshot, 200, project_root=self.root))

    def test_pdf_evidence_accepts_exact_quotes_and_correct_physical_pages(self):
        report = self.report()
        self.assertTrue(report.evidence_checks_passed)
        self.assertEqual(report.exact_quote_matches, len(self.pdf_candidate.evidence))
        self.assertEqual(report.validator_version, "evidence-validator-v4")
        self.assertFalse(report.to_dict()["verified"])

    def test_pdf_missing_wrong_and_cross_page_locations_are_rejected(self):
        for page in (None, 2):
            data = self.pdf_candidate.model_dump(mode="json")
            data["evidence"][0]["location"]["page_number"] = page
            self.assertIn("pdf_page_number", [issue.code for issue in self.report(data).issues])
        data = self.pdf_candidate.model_dump(mode="json")
        data["evidence"][0]["location"]["block_ids"].append("p000002")
        self.assertFalse(self.report(data).evidence_checks_passed)

    def test_pdf_corrupted_raw_content_is_rejected(self):
        from scholarship_intelligence.preparation import prepare_snapshot
        (self.root / self.pdf_snapshot.raw_file_path).write_bytes(b"damaged")
        with self.assertRaises(ValueError):
            prepare_snapshot(self.pdf_snapshot, 200, project_root=self.root)

    def test_drawing_only_page_blocks_partial_document_preparation(self):
        from scholarship_intelligence.preparation import prepare_snapshot
        raw = save_raw_snapshot("https://example.test/TEST-drawing.pdf", self.pdf_bytes(drawing_only=True),
                                self.when, "application/pdf", project_root=self.root)
        with self.assertRaisesRegex(ValueError, "no extractable text"):
            prepare_snapshot(raw, 200, project_root=self.root)
        identity = sha256((raw.source_url + "\0" + raw.content_hash).encode()).hexdigest()
        self.assertFalse((self.root / "data/runtime/snapshots/prepared" / identity).exists())

    def test_genuinely_empty_page_keeps_its_physical_location(self):
        from scholarship_intelligence.preparation import prepare_snapshot
        raw = save_raw_snapshot("https://example.test/TEST-blank-page.pdf", self.pdf_bytes(add_blank=True),
                                self.when, "application/pdf", project_root=self.root)
        prepared = prepare_snapshot(raw, 200, project_root=self.root)
        self.assertEqual(prepared.blank_pages, (3,))
        self.assertEqual(prepared.blocks[2].page_number, 3)
        self.assertEqual(prepared.blocks[2].text, "")


    def test_pdf_invalid_page_map_is_rejected_before_evidence_checks(self):
        path = self.root / self.pdf.prepared_file_path
        data = json.loads(path.read_text(encoding="utf-8"))
        data["blocks"][1]["page_number"] = 1
        path.write_text(json.dumps(data), encoding="utf-8")
        altered = create_bundle([self.pdf.prepared_file_path], "TEST invalid page map", project_root=self.root)
        candidate = self.pdf_candidate.model_copy(update={"bundle_id": altered.bundle_id})
        from scholarship_intelligence.evidence_validation import validate_evidence
        with self.assertRaisesRegex(ValueError, "physical page locations"):
            validate_evidence(candidate, self.root / altered.bundle_file_path, project_root=self.root)

    def test_saved_crawl_pipeline_routes_pdf_without_database_writes(self):
        from scholarship_intelligence.pipeline import prepare_crawl_bundle
        self.pilot.write_text(json.dumps({"event": "fetched", "http_status": 200,
                                        "source_category": "unknown", "snapshot": asdict(self.pdf_snapshot)}) + "\n", encoding="utf-8")
        result = prepare_crawl_bundle(self.pilot, [self.pdf_snapshot.source_url], "TEST PDF fixture", project_root=self.root)
        self.assertEqual(result["bundle_id"], self.bundle.bundle_id)
        self.assertEqual(result["api_calls_made"], 0)
        with closing(sqlite3.connect(self.database)) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM scholarships").fetchone()[0], 0)


class WorkflowTests(unittest.TestCase):
    def test_seed_selection_and_grouping_exclude_shared_support_and_failed_latest(self):
        from unittest.mock import patch
        from scholarship_intelligence import spider, pipeline
        with TemporaryDirectory(dir=Path(__file__).resolve().parent) as temporary:
            path = Path(temporary) / "seeds.json"
            seeds = [{"seed_id": seed, "allowed_hosts": ["example.test"],
                      "start_url": "https://example.test/"} for seed in ("iit_delhi", "other")]
            path.write_text(json.dumps({"seeds": seeds}), encoding="utf-8")
            with patch.object(spider, "SEEDS_PATH", path):
                self.assertEqual(len(spider.ScholarshipSpider().seeds), 2)
                self.assertEqual(spider.ScholarshipSpider(seed_id="iit_delhi").seeds, seeds[:1])
                with self.assertRaises(ValueError):
                    spider.ScholarshipSpider(seed_id="missing")
        urls = ["https://example.test/scholarships-awards/" + str(n) for n in (930, 931)]
        def fetched(url, role="candidate_detail", status=200, day=1):
            return {"seed_id": "iit_delhi", "event": "fetched", "page_role": role,
                    "http_status": status, "snapshot": {"source_url": url,
                    "content_hash": "a" * 64, "retrieved_at": f"2026-01-{day:02d}T00:00:00+00:00"}}
        shared, own = "https://example.test/shared.pdf", "https://example.test/own.pdf"
        events = [fetched(url) for url in urls] + [fetched(shared, "document"), fetched(own, "document")]
        events += [{"event": "discovery", "seed_id": "iit_delhi", "parent_url": parent, "url": target}
                   for parent, target in [(urls[0], shared), (urls[1], shared), (urls[0], own)]]
        groups = pipeline._seed_offerings(seeds[0], events)
        self.assertEqual(groups[0]["source_urls"], [urls[0], own])
        self.assertEqual(groups[1]["source_urls"], [urls[1]])
        events.append(fetched(urls[0], status=404, day=2))
        self.assertEqual([g["source_url"] for g in pipeline._seed_offerings(seeds[0], events)], urls[1:])

    def test_lifecycle_needs_reviewed_student_window(self):
        from datetime import date
        from scholarship_intelligence.scholarship_storage import reviewed_lifecycle
        claims = {"opening_date": {"value": {"calendar_date": "2026-01-01", "stage": "student_application"}},
                  "closing_date": {"value": {"calendar_date": "2026-01-31", "stage": "student_application"}}}
        self.assertEqual(reviewed_lifecycle(claims, "REVIEW_REQUIRED", today=date(2026, 1, 15)), "REVIEW_REQUIRED")
        self.assertEqual(reviewed_lifecycle(claims, "VERIFIED", today=date(2026, 1, 15)), "ACTIVE")
        self.assertEqual(reviewed_lifecycle(claims, "VERIFIED", today=date(2026, 2, 1)), "EXPIRED")
        claims["closing_date"]["value"]["stage"] = "institution_verification"
        self.assertEqual(reviewed_lifecycle(claims, "VERIFIED", today=date(2026, 1, 15)), "REVIEW_REQUIRED")



if __name__ == "__main__":
    unittest.main()
