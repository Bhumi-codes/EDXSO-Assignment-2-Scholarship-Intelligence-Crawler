"""Python-only extraction integration tests; network calls are forbidden."""
from contextlib import closing
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import json
import socket
import sqlite3
import unittest
from unittest.mock import patch

from scholarship_intelligence import pipeline, dashboard, python_extraction
from scholarship_intelligence.snapshots import save_raw_snapshot
from scholarship_intelligence.storage import initialize_database


class PythonWorkflowTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory(prefix="python-workflow-", dir=Path(__file__).resolve().parent)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / "config").mkdir()
        self.seed = {"seed_id": "iit_delhi", "start_url": "https://alumni.iitd.ac.in/scholarships-awards",
                     "allowed_hosts": ["alumni.iitd.ac.in"], "provider_name": "IIT Delhi", "extraction_profile": "iit_alumni_v1"}
        (self.root / "config/seeds.json").write_text(json.dumps({"seeds": [self.seed]}), encoding="utf-8")
        self.when = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.snapshots = []
        for number in (930, 931):
            snapshot = save_raw_snapshot(f"https://alumni.iitd.ac.in/scholarships-awards/{number}",
                f"<html><body><h1>TEST Scholarship {number}</h1><p>Eligibility Criteria</p><p>Only first-year students are eligible.</p><p>Selection Criteria</p><p>Lowest income takes preference.</p><p>Scholarship Amount and number</p><p>Rs. 25,000/-</p><h2>Address</h2><p>Donor value Rs. 999,999 is footer content.</p><p>IIT Delhi</p></body></html>".encode(),
                self.when, "text/html", project_root=self.root)
            self.snapshots.append(snapshot)
        self.pilot = self.root / "data/runtime/pilot.jsonl"
        self.write_pilot()
        self.database = initialize_database(self.root / "data/runtime/scholarships.sqlite3")
        self.enterContext(patch.object(socket, "create_connection", side_effect=AssertionError("No network is allowed during saved-source extraction.")))

    def write_pilot(self):
        self.pilot.write_text("\n".join(json.dumps({"event": "fetched", "seed_id": "iit_delhi", "source_category": "university",
            "http_status": 200, "page_role": "candidate_detail", "snapshot": asdict(snapshot)}) for snapshot in self.snapshots) + "\n", encoding="utf-8")

    def run_batch(self, **kwargs):
        return pipeline.run_seed_workflow("iit_delhi", project_root=self.root, database_path=self.database, **kwargs)

    def test_seed_check_includes_all_values_and_evidence_without_writes(self):
        from hashlib import sha256
        self.run_batch(pilot_file=self.pilot)
        before = sha256(self.database.read_bytes()).hexdigest()
        result = pipeline.check_seed_scholarships("iit_delhi", database_path=self.database, project_root=self.root)
        self.assertEqual(result["scholarships_checked"], 2)
        self.assertTrue(result["all_evidence_checks_passed"])
        self.assertEqual(result["counts"]["exact_quote_matches"], result["counts"]["evidence_entries_checked"])
        self.assertEqual(result["field_coverage"]["eligibility"], {"present": 2})
        claim = result["scholarships"][0]["fields"]["eligibility"]
        self.assertIn("first-year", claim["value"]["summary"])
        self.assertEqual(claim["source_evidence"][0]["quote"], "Only first-year students are eligible.")
        self.assertTrue(claim["source_evidence"][0]["source_context"])
        self.assertEqual(result["database_rows_changed"], 0)
        self.assertFalse(result["live_crawl_performed"])
        self.assertIsNone(result["confidence_score"])
        self.assertEqual(before, sha256(self.database.read_bytes()).hexdigest())

    def test_dashboard_source_checks_do_not_block_background_database_commits(self):
        self.run_batch(pilot_file=self.pilot)
        def inspect_source(record, **kwargs):
            with closing(sqlite3.connect(self.database, timeout=.1)) as connection:
                connection.execute("UPDATE scholarships SET confidence_score=confidence_score WHERE scholarship_id=?", (record['scholarship_id'],))
                connection.commit()
        with patch.object(dashboard, 'apply_source_assessment', side_effect=inspect_source):
            self.assertEqual(len(dashboard.load_records(self.database, project_root=self.root)), 2)

    def test_new_classification_requires_identity_review_without_duplicate_record(self):
        old = self.snapshots[0]
        content = (self.root / old.raw_file_path).read_bytes().replace(b'TEST Scholarship 930', b'TEST Memorial Financial Aid')
        self.snapshots[0] = save_raw_snapshot(old.source_url, content, self.when, 'text/html', project_root=self.root)
        self.write_pilot()
        first = self.run_batch(pilot_file=self.pilot, limit=1)
        self.assertEqual(first['offerings'][0]['offering_type'], 'unknown')
        self.assertTrue(first['offerings'][0]['inserted'])
        revised = content.replace(b'Rs. 25,000/-', b'The scholarship provides Rs. 25,000/- per year.')
        self.snapshots[0] = save_raw_snapshot(old.source_url, revised, datetime(2026, 1, 2, tzinfo=timezone.utc), 'text/html', project_root=self.root)
        self.write_pilot()
        result = self.run_batch(pilot_file=self.pilot, limit=1, reextract=True)
        row = result['offerings'][0]
        self.assertEqual(row['offering_type'], 'scholarship')
        self.assertEqual(row['stage'], 'identity_review_required')
        self.assertIn('classification', row['reason'])
        self.assertEqual(row['scholarship_ids'], [first['offerings'][0]['scholarship_id']])
        with closing(sqlite3.connect(self.database)) as connection:
            self.assertEqual(connection.execute('SELECT count(*) FROM scholarships').fetchone()[0], 1)

    def test_parental_income_threshold_is_not_a_scholarship_benefit(self):
        self.seed['extraction_profile'] = 'official_sections_v1'
        (self.root / 'config/seeds.json').write_text(json.dumps({'seeds': [self.seed]}))
        old = self.snapshots[0]
        from scholarship_intelligence.models import ScholarshipExtraction
        from scholarship_intelligence.confidence import review_python_source
        for requirement in ['For this scholarship, family income must not exceed INR 250000 per annum.', 'Undergraduate applicants must pay tuition fees of INR 25000 per year.']:
            with self.subTest(requirement=requirement):
                html = ('<html><body><h1>TEST Scholarship 930</h1><p>IIT Delhi</p><h2>Eligibility</h2><p>'+requirement+'</p></body></html>').encode()
                self.snapshots[0] = save_raw_snapshot(old.source_url, html, self.when, 'text/html', project_root=self.root)
                self.write_pilot()
                row = self.run_batch(pilot_file=self.pilot, limit=1, reextract=True)['offerings'][0]
                candidate = dashboard.load_workflow_candidate(row, self.root)
                self.assertIsNone(candidate['benefits']['value'])
                self.assertEqual(candidate['eligibility']['value']['summary'], requirement)
                parsed = ScholarshipExtraction.model_validate(candidate)
                review = review_python_source(parsed, self.root/'data/runtime/bundles'/(row['bundle_id']+'.json'), self.seed, {identity:self.when.isoformat() for identity in parsed.snapshot_ids}, project_root=self.root)
                self.assertNotIn(review['fields']['benefits']['status'], {'supported', 'not_applicable'})

    def test_seed_check_excludes_awards_and_retains_corrupted_source_failure(self):
        old = self.snapshots[1]
        content = (self.root / old.raw_file_path).read_bytes().replace(b"TEST Scholarship 931", b"TEST Cash Award")
        self.snapshots[1] = save_raw_snapshot(old.source_url, content, self.when, "text/html", project_root=self.root)
        self.write_pilot()
        self.run_batch(pilot_file=self.pilot)
        result = pipeline.check_seed_scholarships("iit_delhi", database_path=self.database, project_root=self.root)
        self.assertEqual(result["scholarships_checked"], 1)
        (self.root / self.snapshots[0].raw_file_path).write_bytes(b"TEST corrupted source")
        result = pipeline.check_seed_scholarships("iit_delhi", database_path=self.database, project_root=self.root)
        self.assertFalse(result["all_evidence_checks_passed"])
        self.assertEqual(result["counts"]["failed"], 1)
        self.assertTrue(result["scholarships"][0]["error"])

    def test_seed_check_does_not_call_partial_crawl_complete(self):
        self.budget_events()
        self.run_batch(pilot_file=self.pilot)
        result = pipeline.check_seed_scholarships("iit_delhi", database_path=self.database, project_root=self.root)
        self.assertEqual(result["counts"]["passed"], 2)
        self.assertFalse(result["observed_crawl_coverage_complete"])
        self.assertFalse(result["all_evidence_checks_passed"])

    def test_seed_check_rejects_stale_sources_and_incomplete_scope(self):
        original_report = self.run_batch(pilot_file=self.pilot)
        old_report = self.root / "data/runtime/old-workflow.json"
        old_report.write_text(json.dumps(original_report))
        old = self.snapshots[0]
        self.snapshots[0] = save_raw_snapshot(old.source_url,
            (self.root / old.raw_file_path).read_bytes().replace(b"25,000", b"30,000"),
            datetime(2026, 1, 2, tzinfo=timezone.utc), "text/html", project_root=self.root)
        self.write_pilot()
        self.run_batch(resume=True)
        current = pipeline.check_seed_scholarships("iit_delhi", database_path=self.database, project_root=self.root)
        self.assertFalse(current["all_evidence_checks_passed"])
        self.assertGreater(current["counts"]["failed"], 0)
        with self.assertRaisesRegex(ValueError, "latest observed"):
            pipeline.check_seed_scholarships("iit_delhi", workflow_report=old_report, database_path=self.database, project_root=self.root)

    def test_inline_section_labels_preserve_requirements_and_no_application_guidance(self):
        old = self.snapshots[0]
        html = b"""<html><body><h1>TEST Scholarship 930</h1>
        <p>Eligibility Criterion<br>First-year students with family income less than Rs. 9 lakh.</p>
        <p>Selection Criterion<br>Highest CGPA among eligible applicants.</p>
        <p>Continuation Criterion<br>One year only; students can reapply.</p>
        <p>Amount and number<br>Rs. 1 Lakh</p>
        <p>How to Apply<br>No Application Needed</p>
        <table><tr><th>Student Name</th><th>Academic Year</th></tr><tr><td>TEST Recipient</td><td>2023-24</td></tr></table>
        <h2>Address</h2><p>IIT Delhi</p></body></html>"""
        self.snapshots[0] = save_raw_snapshot(old.source_url, html, self.when, "text/html", project_root=self.root)
        self.write_pilot()
        result = self.run_batch(pilot_file=self.pilot, limit=1)
        row = result["offerings"][0]
        candidate = dashboard.load_workflow_candidate(row, self.root)
        self.assertIn("less than Rs. 9 lakh", candidate["eligibility"]["value"]["summary"])
        self.assertEqual(candidate["benefits"]["value"][0]["description"], "Rs. 1 Lakh")
        self.assertEqual(candidate["renewal_requirements"]["value"]["summary"], "One year only; students can reapply.")
        self.assertEqual(candidate["application_url"]["missing_reason"], "not_applicable")
        self.assertEqual(candidate["status_statements"]["value"][0]["statement"], "No Application Needed")
        self.assertIsNone(candidate["cycle"]["value"])
        self.assertIsNone(candidate["closing_date"]["value"])
        self.assertFalse(row["evidence"]["issues"])
        checks = {item["check_id"]: item for item in row["assessment"]["checks"]}
        self.assertEqual(checks["application_route_support"]["status"], "not_applicable")
        self.assertEqual(checks["availability_support"]["status"], "pending")
        self.assertEqual(row["verification_status"], "REVIEW_REQUIRED")

    def test_no_application_guidance_conflicting_with_apply_link_requires_review(self):
        old = self.snapshots[0]
        html = b"""<html><body><h1>TEST Scholarship</h1>
        <p>How to Apply<br>No Application Needed</p>
        <p><a href="https://alumni.iitd.ac.in/application-form">Apply online</a></p>
        <h2>Address</h2><p>IIT Delhi</p></body></html>"""
        self.snapshots[0] = save_raw_snapshot(old.source_url, html, self.when, "text/html", project_root=self.root)
        self.write_pilot()
        row = self.run_batch(pilot_file=self.pilot, limit=1)["offerings"][0]
        candidate = dashboard.load_workflow_candidate(row, self.root)
        self.assertEqual(candidate["application_url"]["missing_reason"], "ambiguous_or_conflicting")
        self.assertIsNone(candidate["application_url"]["value"])
        checks = {item["check_id"]: item for item in row["assessment"]["checks"]}
        self.assertEqual(checks["conflict_resolution"]["status"], "fail")
        self.assertEqual(checks["application_route_support"]["status"], "pending")
        self.assertFalse(row["evidence"]["issues"])

    def test_colon_inline_labels_and_cash_prize_titles_are_recognized(self):
        old = self.snapshots[0]
        html = b"""<html><body><h1>TEST Cash Prize</h1>
        <p>Eligibility Criterion: Final-year students.</p>
        <p>Selection Criterion: Best project work.</p>
        <p>Amount and number: Rs. 25,000/-</p><h2>Address</h2><p>IIT Delhi</p></body></html>"""
        self.snapshots[0] = save_raw_snapshot(old.source_url, html, self.when, "text/html", project_root=self.root)
        self.write_pilot()
        result = self.run_batch(pilot_file=self.pilot, limit=1)
        row = result["offerings"][0]
        self.assertEqual(row["name"], "TEST Cash Prize")
        self.assertEqual(row["offering_type"], "award")
        candidate = dashboard.load_workflow_candidate(row, self.root)
        self.assertEqual(candidate["benefits"]["value"][0]["amount"], "25000")
        self.assertEqual(candidate["eligibility"]["value"]["summary"], "Final-year students.")
        self.assertFalse(row["evidence"]["issues"])
        self.assertFalse(dashboard.filter_records(dashboard.load_records(self.database)))

    def test_neutral_title_uses_evidenced_scholarship_code_for_classification(self):
        old = self.snapshots[0]
        html = b"""<html><body><h1>TEST Memorial Financial Aid</h1>
        <p>Scholarship Code: TEST123</p><p>Eligibility Criteria<br>First-year students.</p>
        <p>Scholarship Amount and number<br>Rs. 100,000 pa</p>
        <p>How to Apply<br>No Application Needed</p><h2>Address</h2><p>IIT Delhi</p></body></html>"""
        self.snapshots[0] = save_raw_snapshot(old.source_url, html, self.when, "text/html", project_root=self.root)
        self.write_pilot()
        result = self.run_batch(pilot_file=self.pilot, limit=1)
        row = result["offerings"][0]
        self.assertEqual(row["name"], "TEST Memorial Financial Aid")
        self.assertEqual(row["offering_type"], "scholarship")
        candidate = dashboard.load_workflow_candidate(row, self.root)
        self.assertEqual(len(candidate["scholarship_name"]["evidence_ids"]), 2)
        self.assertFalse(row["evidence"]["issues"])
        records = dashboard.filter_records(dashboard.load_records(self.database))
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["offering_classification"]["basis"], "source_programme_designation")

    def test_section_tables_and_application_labels_do_not_enter_benefit_text(self):
        blocks = [{"block_id": "b1", "kind": "paragraph", "text": "Scholarship Amount and number"},
                  {"block_id": "b2", "kind": "paragraph", "text": "Rs. 25,000/-"},
                  {"block_id": "b3", "kind": "table", "text": "Student Name\n2024-25"},
                  {"block_id": "b4", "kind": "paragraph", "text": "TEST Recipient"},
                  {"block_id": "b5", "kind": "paragraph", "text": "How to Apply\nNo Application Needed"}]
        sections = python_extraction._section_blocks(python_extraction._source_views(blocks))
        self.assertEqual([block["text"] for block in sections["benefits"]], ["Rs. 25,000/-"])
        self.assertEqual([block["text"] for block in sections["application_instructions"]], ["No Application Needed"])

    def test_offering_types_exclude_awards_even_when_they_fund_students(self):
        from scholarship_intelligence.models import classify_offering
        claims = {"benefits": {"value": [{"description": "This scholarship covers tuition for students."}]}}
        for title in ("Best Thesis Award", "Merit Prize", "Gold Medal", "Student Cash Awards"):
            with self.subTest(title=title):
                self.assertEqual(classify_offering(title, claims)["offering_type"], "award")
                self.assertFalse(classify_offering(title, claims)["main_dashboard_eligible"])
        self.assertEqual(classify_offering("Merit Scholarship", claims)["offering_type"], "scholarship")
        for title in ("Research Fellowship", "Fellowship/Scholarship", "Scholarship and Award", "Education Support"):
            self.assertFalse(classify_offering(title)["main_dashboard_eligible"])
        self.assertTrue(classify_offering("Education Support", claims)["main_dashboard_eligible"])
        exclusions = {"eligibility": {"value": {"summary": "Must not receive another scholarship."}}}
        self.assertEqual(classify_offering("Education Support", exclusions)["offering_type"], "unknown")

    def test_dashboard_filters_awards_from_records_and_historical_workflow_rows(self):
        original = self.snapshots[1]
        content = (self.root / original.raw_file_path).read_bytes().replace(b"TEST Scholarship 931", b"TEST Student Cash Award")
        self.snapshots[1] = save_raw_snapshot(original.source_url, content, self.when, "text/html", project_root=self.root)
        self.write_pilot()
        first = self.run_batch(pilot_file=self.pilot)
        records = dashboard.load_records(self.database)
        self.assertEqual(len(records), 2)
        shown = dashboard.filter_records(records)
        self.assertEqual([row["name"] for row in shown], ["TEST Scholarship 930"])
        self.assertEqual(dashboard.summarize(shown)["stored"], 1)
        rows = dashboard.scholarship_workflow_rows(first, records)
        self.assertEqual([row["name"] for row in rows], ["TEST Scholarship 930"])
        repeated = self.run_batch(resume=True)
        self.assertEqual(repeated["counts"], {"unchanged": 2})
        rows = dashboard.scholarship_workflow_rows(repeated, records)
        self.assertEqual([row["name"] for row in rows], ["TEST Scholarship 930"])
        # The full archive remains readable; filtering never deletes evidence.
        self.assertEqual(len(dashboard.filter_records(records, scholarships_only=False)), 2)
        self.assertEqual(first["offerings"][1]["offering_type"], "award")

    def test_workflow_report_can_refresh_but_other_artifacts_are_protected(self):
        from scholarship_intelligence import cli
        output = self.root / "data/runtime/latest.json"
        first = {"operation": "seed_workflow", "seed_id": "iit_delhi", "run_id": 1}
        second = {**first, "run_id": 2}
        with patch.object(cli, "PROJECT_ROOT", self.root), patch("builtins.print"):
            cli._emit(first, output)
            cli._emit(second, output)
            self.assertEqual(json.loads(output.read_text())["run_id"], 2)
            with self.assertRaisesRegex(ValueError, "not a workflow report"):
                cli._emit({**first, "seed_id": "ffe"}, output)
            self.assertEqual(json.loads(output.read_text())["run_id"], 2)
            artifact = self.root / "data/runtime/candidate.json"
            artifact.write_text('{"bundle_id": "TEST"}', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "not a workflow report"):
                cli._emit(second, artifact)
            self.assertEqual(artifact.read_text(), '{"bundle_id": "TEST"}')
            with self.assertRaises(FileExistsError):
                cli._emit({"operation": "inspect"}, output)
        self.assertFalse(list(output.parent.glob("*.tmp")))

    def test_failed_report_replacement_preserves_previous_report(self):
        from scholarship_intelligence import cli
        output = self.root / "data/runtime/latest.json"
        first = {"operation": "seed_workflow", "seed_id": "iit_delhi", "run_id": 1}
        with patch.object(cli, "PROJECT_ROOT", self.root), patch("builtins.print"):
            cli._emit(first, output)
            with patch.object(cli.os, "replace", side_effect=OSError("TEST interrupted write")):
                with self.assertRaises(OSError):
                    cli._emit({**first, "run_id": 2}, output)
        self.assertEqual(json.loads(output.read_text()), first)
        self.assertFalse(list(output.parent.glob("*.tmp")))

    def budget_events(self):
        events = [json.loads(line) for line in self.pilot.read_text().splitlines()]
        for row in events:
            row["navigation_depth"] = 1
        for number in (932, 933):
            events.append({"event": "discovery", "seed_id": "iit_delhi",
                           "parent_url": self.seed["start_url"], "url": f"https://alumni.iitd.ac.in/scholarships-awards/{number}",
                           "link_text": "Read More", "decision": "page_limit", "navigation_depth": 1})
        self.pilot.write_text("\n".join(json.dumps(row) for row in events) + "\n", encoding="utf-8")
        return events

    def test_coverage_distinguishes_fetched_subset_and_remaining_budget(self):
        events = self.budget_events()
        report = pipeline.crawl_coverage(events, "iit_delhi")
        self.assertEqual(report["successful_detail_pages"], 2)
        self.assertEqual(report["discovered_detail_urls"], 4)
        self.assertEqual(report["budget_skipped_count"], 2)
        self.assertEqual(report["status"], "budget_limited")
        report = self.run_batch(pilot_file=self.pilot, dry_run=True)
        self.assertEqual(report["coverage"]["budget_skipped_count"], 2)

    def test_spider_continues_skipped_urls_without_fetching_old_pages(self):
        from scrapy import Request
        from scholarship_intelligence import spider
        self.budget_events()
        with patch.object(spider, "SEEDS_PATH", self.root / "config/seeds.json"):
            worker = spider.ScholarshipSpider(seed_id="iit_delhi", max_pages=1, continue_from=self.pilot)
        # start() yields requests without awaiting I/O. Drive those yields
        # directly: the Windows sandbox cannot create asyncio's self-pipe.
        rows = []
        generator = worker.start()
        while True:
            try:
                generator.__anext__().send(None)
            except StopIteration as yielded:
                rows.append(yielded.value)
            except StopAsyncIteration:
                break
        requests = [row for row in rows if isinstance(row, Request)]
        self.assertEqual(len(requests), 1)
        self.assertTrue(requests[0].url.endswith("/932"))
        self.assertEqual(requests[0].meta["navigation_depth"], 1)
        remaining = [row for row in rows if isinstance(row, dict) and row["decision"] == "page_limit"]
        self.assertEqual(len(remaining), 1)
        self.assertEqual(worker.request_for(self.seed, self.snapshots[0].source_url, None, 1)[1], "previously_fetched")
        self.assertEqual(worker.request_for(self.seed, "https://example.com/notice", None, 1)[1], "external_host")

    def test_continuation_merges_reports_and_passes_budget_to_worker(self):
        from types import SimpleNamespace
        self.budget_events()
        new = save_raw_snapshot("https://alumni.iitd.ac.in/scholarships-awards/932", b"<h1>TEST Scholarship 932</h1>",
                                self.when, "text/html", project_root=self.root)
        def request(method, url, **kwargs):
            self.assertEqual(method, "GET")
            self.assertTrue(url.endswith("/932"))
            return SimpleNamespace(status_code=200, url=url, content=b"<h1>TEST Scholarship 932</h1>", headers={"Content-Type": "text/html"})
        robots = SimpleNamespace(status_code=200, text="User-agent: *\nDisallow:")
        with patch("requests.Session.get", return_value=robots), patch("requests.Session.request", side_effect=request):
            result = pipeline.crawl_sources(seed_id="iit_delhi", max_pages=1, continue_from=self.pilot, project_root=self.root)
        self.assertEqual(result["coverage"]["successful_detail_pages"], 3)
        self.assertEqual(result["coverage"]["budget_skipped_count"], 1)
        self.assertNotEqual(result["pilot_file"], self.pilot.relative_to(self.root).as_posix())
        self.assertEqual(len(pipeline._seed_offerings(self.seed, [json.loads(line) for line in
            (self.root / result["pilot_file"]).read_text().splitlines()])), 3)

    def test_continuation_with_no_backlog_makes_no_network_request(self):
        with patch("requests.Session.request", side_effect=AssertionError("No crawler needed")):
            result = pipeline.crawl_sources(seed_id="iit_delhi", continue_from=self.pilot, project_root=self.root)
        self.assertFalse(result["live_crawl_performed"])
        self.assertEqual(result["coverage"]["budget_skipped_count"], 0)
        self.assertFalse((self.root / "data/runtime/crawl.lock").exists())

    def test_failed_request_does_not_reenter_budget_frontier(self):
        events = self.budget_events()
        failed_url = "https://alumni.iitd.ac.in/scholarships-awards/932"
        events.append({"event": "request_failed", "seed_id": "iit_delhi", "url": failed_url, "error_type": "TimeoutError"})
        self.pilot.write_text("\n".join(json.dumps(row) for row in events) + "\n", encoding="utf-8")
        coverage = pipeline.crawl_coverage(events, "iit_delhi")
        self.assertEqual(coverage["failed_pages"], 1)
        self.assertEqual(coverage["budget_skipped_count"], 1)
        from scholarship_intelligence import spider
        with patch.object(spider, "SEEDS_PATH", self.root / "config/seeds.json"):
            worker = spider.ScholarshipSpider(seed_id="iit_delhi", max_pages=5, continue_from=self.pilot)
        self.assertEqual([row[0] for row in worker.frontier], ["https://alumni.iitd.ac.in/scholarships-awards/933"])

    def test_continuation_cannot_hide_a_live_crawl_in_preview(self):
        with self.assertRaisesRegex(ValueError, "cannot use --dry-run"):
            self.run_batch(resume=True, continue_crawl=True, dry_run=True)
        with self.assertRaisesRegex(ValueError, "page budget"):
            self.run_batch(pilot_file=self.pilot, max_pages=0)

    def test_assessment_gives_specific_gaps_and_supported_route(self):
        old = self.snapshots[0]
        content = (self.root / old.raw_file_path).read_bytes().replace(
            b"</body>", b'<p><a href="https://alumni.iitd.ac.in/application-form">Apply online</a></p></body>')
        self.snapshots[0] = save_raw_snapshot(old.source_url, content,
            self.when, "text/html", project_root=self.root)
        self.write_pilot()
        result = self.run_batch(pilot_file=self.pilot, limit=1)
        checks = {check["check_id"]: check for check in result["offerings"][0]["assessment"]["checks"]}
        self.assertEqual(checks["application_route_support"]["status"], "pass")
        self.assertTrue(checks["application_route_support"]["evidence_ids"])
        self.assertIn("opening_date", checks["completeness"]["reason"])
        self.assertIn("literal prose", checks["numbers_dates_units"]["reason"])
        self.assertEqual(checks["availability_support"]["status"], "pending")
        self.assertIsNone(result["offerings"][0]["confidence_score"])

    def test_exclusive_notice_grouped_shared_notice_flagged_and_dates_extracted(self):
        events = [json.loads(line) for line in self.pilot.read_text().splitlines()]
        primary = self.snapshots[0].source_url
        notice = save_raw_snapshot("https://alumni.iitd.ac.in/notice/930",
            b"<html><body><p>Applications open on 1 November 2026 and close on 30 November 2026.</p></body></html>",
            self.when, "text/html", project_root=self.root)
        events.extend([
            {"event": "discovery", "seed_id": "iit_delhi", "parent_url": primary,
             "url": notice.source_url, "scheduled_url": notice.source_url, "link_text": "Application notice"},
            {"event": "fetched", "seed_id": "iit_delhi", "source_category": "university",
             "http_status": 200, "page_role": "announcement", "snapshot": asdict(notice)},
        ])
        grouped = pipeline._seed_offerings(self.seed, events)
        self.assertEqual(grouped[0]["source_urls"], [primary, notice.source_url])
        self.pilot.write_text("\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8")
        result = self.run_batch(pilot_file=self.pilot, limit=1)
        candidate = dashboard.load_workflow_candidate(result["offerings"][0], self.root)
        self.assertEqual(candidate["closing_date"]["value"]["calendar_date"], "2026-11-30")
        self.assertFalse(result["offerings"][0]["evidence"]["issues"])
        events.append({"event": "discovery", "seed_id": "iit_delhi", "parent_url": self.snapshots[1].source_url,
                       "url": notice.source_url, "scheduled_url": None, "link_text": "Application notice"})
        grouped = pipeline._seed_offerings(self.seed, events)
        self.assertNotIn(notice.source_url, grouped[0]["source_urls"])
        self.assertEqual(grouped[0]["supporting_source_review"][0]["reason"], "shared_link_requires_offering_association")

    def test_unfetched_notice_is_visible_without_inventing_a_date(self):
        events = [json.loads(line) for line in self.pilot.read_text().splitlines()]
        events.append({"event": "discovery", "seed_id": "iit_delhi", "parent_url": self.snapshots[0].source_url,
                       "url": "https://alumni.iitd.ac.in/notice/new", "scheduled_url": None,
                       "link_text": "Deadline extension", "decision": "page_budget_reached"})
        grouped = pipeline._seed_offerings(self.seed, events)
        self.assertEqual(len(grouped[0]["source_urls"]), 1)
        self.assertEqual(grouped[0]["supporting_source_review"][0]["crawl_decision"], "page_budget_reached")

    def test_batch_stores_evidence_without_llm_and_repeat_is_idempotent(self):
        first = self.run_batch(pilot_file=self.pilot)
        self.assertEqual(first["counts"], {"stored": 2}, first)
        self.assertEqual(first["api_calls_made"], 0)
        self.assertEqual(first["scholarship_rows_changed"], 2)
        for row in first["offerings"]:
            info = pipeline.inspect_scholarship(row["scholarship_id"], database_path=self.database)
            self.assertEqual(info["extraction_provenance"]["method"], "python_rules")
            self.assertEqual(info["extraction_provenance"]["author_kind"], "software")
            self.assertEqual(info["verification_status"], "REVIEW_REQUIRED")
            self.assertIsNone(info["confidence_score"])
            self.assertFalse(info["accepted_eligibility_present"])
            candidate = dashboard.load_workflow_candidate(row, self.root)
            self.assertEqual(candidate["benefits"]["value"][0]["amount"], "25000")
            self.assertNotIn("999,999", candidate["benefits"]["value"][0]["description"])
        repeated = self.run_batch(resume=True)
        self.assertEqual(repeated["counts"], {"unchanged": 2}, repeated)
        self.assertEqual(repeated["scholarship_rows_changed"], 0)
        self.assertEqual(dashboard.load_workflows(self.database)[0]["run_id"], repeated["run_id"])

    def test_preview_is_read_only(self):
        result = self.run_batch(pilot_file=self.pilot, dry_run=True)
        self.assertEqual(result["counts"], {"ready": 2})
        self.assertFalse((self.root / "data/runtime/extractions").exists())
        with closing(sqlite3.connect(self.database)) as c:
            self.assertEqual(c.execute("SELECT count(*) FROM crawl_runs").fetchone()[0], 0)

    def test_limit_and_resume_process_remaining_offering(self):
        first = self.run_batch(pilot_file=self.pilot, limit=1)
        second = self.run_batch(resume=True)
        self.assertEqual(first["scholarship_rows_changed"], 1)
        self.assertEqual(second["counts"], {"unchanged": 1, "stored": 1})

    def test_changed_source_needs_update_review_without_duplicate_record(self):
        self.run_batch(pilot_file=self.pilot)
        old = self.snapshots[0]
        self.snapshots[0] = save_raw_snapshot(old.source_url,
            (self.root / old.raw_file_path).read_bytes().replace(b"25,000", b"30,000"),
            datetime(2026, 1, 2, tzinfo=timezone.utc), "text/html", project_root=self.root)
        self.write_pilot()
        result = self.run_batch(resume=True)
        self.assertEqual(result["counts"], {"update_review_required": 1, "unchanged": 1})
        self.assertEqual(result["scholarship_rows_changed"], 0)
        with closing(sqlite3.connect(self.database)) as c:
            self.assertEqual(c.execute("SELECT count(*) FROM scholarships").fetchone()[0], 2)

    def test_ambiguous_amount_is_preserved_without_inventing_fixed_value(self):
        self.assertEqual(python_extraction._benefit("Rs. 1 Lakh")["amount"], "100000")
        annual = python_extraction._benefit("Rs. 100,000 pa")
        self.assertEqual(annual["amount"], "100000")
        self.assertEqual(annual["period"], "annual")
        value = python_extraction._benefit("INR 5-8 lakhs, depending on need")
        self.assertIsNone(value["amount"])
        self.assertEqual(value["description"], "INR 5-8 lakhs, depending on need")
        self.assertIsNone(python_extraction._date("01/02/2026"))
        self.assertIsNone(python_extraction._date("2026-02-30"))

    def test_tampered_python_provenance_and_candidate_are_rejected(self):
        result = self.run_batch(pilot_file=self.pilot, limit=1)
        row = result["offerings"][0]
        directory = self.root / row["draft_directory"]
        path = directory / "python-provenance.json"
        origin = json.loads(path.read_bytes())
        origin["author_kind"] = "human"
        path.write_text(json.dumps(origin), encoding="utf-8")
        with self.assertRaises(ValueError):
            pipeline.check_saved_draft(directory, project_root=self.root)

    def test_numbered_labels_and_source_typo_preserve_section_boundaries(self):
        blocks = [{"block_id": "b" + str(i), "kind": "paragraph", "text": text} for i, text in enumerate([
            "2. Eligibiltiy criterion :", "Only first-year students.", "3. Selection criterion :",
            "Lowest income.", "4. Award Amount and number :", "Rs. 25,000/-", "Address", "Footer amount Rs. 999,999"])]
        sections = python_extraction._section_blocks(blocks)
        self.assertEqual([b["text"] for b in sections["benefits"]], ["Rs. 25,000/-"])
        self.assertEqual([b["text"] for b in sections["eligibility"]], ["Only first-year students."])

    def test_pdf_views_keep_physical_page_and_exact_substrings(self):
        source = {"block_id": "p000001", "kind": "pdf_page", "page_number": 1,
                  "text": "  TEST SCHOLARSHIP\nEligibility Criteria\nOnly first-year students.\nAward Amount and number\nRs. 25,000/-\nPage 1 of 1"}
        views = python_extraction._source_views([source])
        self.assertTrue(all(v["block_id"] == source["block_id"] and v["page_number"] == 1 for v in views))
        self.assertTrue(all(v["text"] in source["text"] for v in views))
        self.assertEqual(views[0]["kind"], "heading")
        self.assertEqual(python_extraction._section_blocks(views)["benefits"][0]["text"], "Rs. 25,000/-")

    def test_confidence_formula_runs_without_model_or_credentials(self):
        from scholarship_intelligence.confidence import score_candidate
        from scholarship_intelligence.semantic_assessment import CheckResult, SemanticAssessment, CHECK_DEFINITIONS, candidate_fingerprint
        from scholarship_intelligence.models import ScholarshipExtraction
        result = self.run_batch(pilot_file=self.pilot, limit=1)
        candidate = ScholarshipExtraction.model_validate(dashboard.load_workflow_candidate(result["offerings"][0], self.root))
        assessment = SemanticAssessment(bundle_id=candidate.bundle_id, snapshot_ids=tuple(candidate.snapshot_ids),
            candidate_fingerprint=candidate_fingerprint(candidate), assessed_at=self.when, evaluator="TEST assessed failures",
            checks=tuple(CheckResult(check_id=key, status="pass" if key in {"source_integrity", "evidence_integrity"} else "fail", reason="Synthetic assessed check.") for key in CHECK_DEFINITIONS))
        score = score_candidate(candidate, assessment)
        self.assertEqual(float(score.confidence_score), 25.0)
        self.assertEqual(score.verification_status, "REVIEW_REQUIRED")

    def test_dates_in_prose_and_numeric_formats_have_explicit_application_context(self):
        from types import SimpleNamespace
        document = {"blocks": [{"block_id": "b1", "kind": "paragraph", "text": "Applications open on 1st August, 2026 and applications close on October 31, 2026."}]}
        dates, review = python_extraction._application_dates({"url": document}, {"url": SimpleNamespace(snapshot_id="test")})
        self.assertEqual(dates["opening_date"][0][0]["calendar_date"], "2026-08-01")
        self.assertEqual(dates["closing_date"][0][0]["calendar_date"], "2026-10-31")
        self.assertFalse(review)
        self.assertEqual(python_extraction._date("31/10/2026")["calendar_date"], "2026-10-31")
        self.assertIsNone(python_extraction._date("05/06/2026"))
        self.assertEqual(python_extraction._date("05/06/2026", date_order="DMY")["calendar_date"], "2026-06-05")

    def test_labelled_dates_table_rows_and_non_student_deadlines_are_separate(self):
        from types import SimpleNamespace
        document = {"blocks": [
            {"block_id": "b1", "kind": "heading", "text": "Opening date"},
            {"block_id": "b2", "kind": "paragraph", "text": "15 August 2026"},
            {"block_id": "b3", "kind": "table", "text": "Application deadline\n31 October 2026\nInstitution verification deadline\n15 November 2026", "table_rows": [
                [{"text": "Application deadline"}, {"text": "31 October 2026"}],
                [{"text": "Institution verification deadline"}, {"text": "15 November 2026"}]]},
            {"block_id": "b4", "kind": "paragraph", "text": "Renewal applications close on 30 November 2026."},
            {"block_id": "b5", "kind": "paragraph", "text": "Final PhD viva is held from 1 January to 31 December of the preceding year."}]}
        dates, review = python_extraction._application_dates({"url": document}, {"url": SimpleNamespace(snapshot_id="test")})
        self.assertEqual(dates["opening_date"][0][0]["calendar_date"], "2026-08-15")
        self.assertEqual([d[0]["calendar_date"] for d in dates["closing_date"]], ["2026-10-31"])
        self.assertTrue(any(item["stage"] == "renewal_application" for item in review))

    def test_extracted_application_dates_are_evidence_validated_and_stored(self):
        original = self.snapshots[0]
        content = (self.root / original.raw_file_path).read_bytes().replace(b"</body>",
            b"<h2>Applications</h2><p>Applications open on 15 August 2026 and applications close on 31 October 2026.</p></body>")
        self.snapshots[0] = save_raw_snapshot(original.source_url, content, self.when, "text/html", project_root=self.root)
        self.write_pilot()
        result = self.run_batch(pilot_file=self.pilot, limit=1)
        row = result["offerings"][0]
        self.assertEqual(row["stage"], "stored", row)
        candidate = dashboard.load_workflow_candidate(row, self.root)
        self.assertEqual(candidate["opening_date"]["value"]["calendar_date"], "2026-08-15")
        self.assertEqual(candidate["closing_date"]["value"]["calendar_date"], "2026-10-31")
        self.assertTrue(row["evidence"]["evidence_checks_passed"])
        with closing(sqlite3.connect(self.database)) as c:
            opening, closing_date, lifecycle = c.execute("SELECT opening_date, closing_date, lifecycle_status FROM scholarships WHERE scholarship_id=?", (row["scholarship_id"],)).fetchone()
        self.assertEqual((opening, closing_date), ("2026-08-15", "2026-10-31"))
        self.assertEqual(lifecycle, "REVIEW_REQUIRED")

    def test_conflicting_dates_are_not_arbitrarily_selected(self):
        original = self.snapshots[0]
        content = (self.root / original.raw_file_path).read_bytes().replace(b"</body>",
            b"<p>Applications close on 31 October 2026.</p><p>Applications close on 15 November 2026.</p></body>")
        self.snapshots[0] = save_raw_snapshot(original.source_url, content, self.when, "text/html", project_root=self.root)
        self.write_pilot()
        result = self.run_batch(pilot_file=self.pilot, limit=1)
        candidate = dashboard.load_workflow_candidate(result["offerings"][0], self.root)
        self.assertIsNone(candidate["closing_date"]["value"])
        self.assertEqual(candidate["closing_date"]["missing_reason"], "ambiguous_or_conflicting")

    def test_explicit_application_window_assigns_start_and_end(self):
        from types import SimpleNamespace
        document = {"blocks": [{"block_id": "b1", "kind": "paragraph", "text": "Application window: from 15 August 2026 to 31 October 2026."}]}
        dates, review = python_extraction._application_dates({"url": document}, {"url": SimpleNamespace(snapshot_id="test")})
        self.assertEqual(dates["opening_date"][0][0]["calendar_date"], "2026-08-15")
        self.assertEqual(dates["closing_date"][0][0]["calendar_date"], "2026-10-31")
        self.assertFalse(review)

    def test_cutoff_time_is_not_attributed_to_the_opening_date(self):
        from types import SimpleNamespace
        document = {"blocks": [{"block_id": "b1", "kind": "paragraph", "text": "Applications open on 15 August 2026 and applications close on 31 October 2026 at 23:59 IST."}]}
        dates, review = python_extraction._application_dates({"url": document}, {"url": SimpleNamespace(snapshot_id="test")})
        self.assertIsNone(dates["opening_date"][0][0]["time_qualifier"])
        self.assertIn("23:59 IST", dates["closing_date"][0][0]["time_qualifier"])
        self.assertFalse(review)

    def test_reextract_preserves_existing_records_for_update_review(self):
        self.run_batch(pilot_file=self.pilot)
        result = self.run_batch(resume=True, reextract=True)
        self.assertEqual(result["counts"], {"update_review_required": 2}, result)
        self.assertEqual(result["scholarship_rows_changed"], 0)


    def test_corrupt_one_source_does_not_stop_remaining_offerings(self):
        (self.root / self.snapshots[0].raw_file_path).write_bytes(b"damaged")
        result = self.run_batch(pilot_file=self.pilot)
        self.assertEqual(result["counts"], {"failed": 1, "stored": 1}, result)
        self.assertEqual(result["api_calls_made"], 0)

    def test_python_packet_compare_preserves_provenance_identity(self):
        from scholarship_intelligence.comparison import compare_review
        result = self.run_batch(pilot_file=self.pilot, limit=1)
        row = result["offerings"][0]
        compared = compare_review(self.root / row["review_directory"], self.pilot,
            scholarship_id=row["scholarship_id"], database_path=self.database, project_root=self.root)
        self.assertEqual(compared["action"], "unchanged")


    def test_existing_cohort_run_does_not_import_new_discoveries(self):
        first = self.run_batch(pilot_file=self.pilot, limit=1)
        result = self.run_batch(pilot_file=self.pilot, existing_only=True, reextract=True, limit=1000)
        self.assertEqual(result["crawl_discovered_offerings"],2)
        self.assertEqual(result["excluded_new_offerings"],1)
        self.assertEqual(result["selected_offerings"],1)
        self.assertEqual(result["offerings"][0].get("scholarship_ids",[result["offerings"][0].get("scholarship_id")]),[first["offerings"][0]["scholarship_id"]])
        with closing(sqlite3.connect(self.database)) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM scholarships").fetchone()[0],1)


if __name__ == "__main__":
    unittest.main()


class SourceScoreTests(unittest.TestCase):
    setUp = PythonWorkflowTests.setUp
    write_pilot = PythonWorkflowTests.write_pilot
    def test_complete_supported_window_can_verify_without_rerunning_extractor(self):
        from datetime import timedelta
        from scholarship_intelligence.confidence import review_python_source, score_source_evidence
        self.snapshots[0] = save_raw_snapshot("https://alumni.iitd.ac.in/scholarships-awards/930",
            b"<html><body><h1>TEST Scholarship 930</h1><p>IIT Delhi</p><p>Eligibility Criteria</p><p>First-year students.</p><p>Scholarship Amount and number</p><p>Rs. 25,000/-</p><h2>How to apply</h2><a href='/apply'>Application form</a><p>Applications open on 1 January 2026 and closes on 1 February 2026.</p></body></html>",
            self.when, "text/html", project_root=self.root)
        self.write_pilot()
        candidate, assessment, bundle, times = self._source_review_fixture()
        with patch("scholarship_intelligence.python_extraction.extract_bundle", side_effect=AssertionError("Verifier must be independent")):
            review = review_python_source(candidate, bundle, self.seed, times, project_root=self.root, now=self.when + timedelta(days=14))
        score = score_source_evidence(candidate, assessment, evidence_checks_passed=True, source_review=review)
        self.assertEqual(score["confidence_score"], 100, score["blockers"])
        self.assertEqual(score["verification_status"], "VERIFIED")
        self.assertEqual(review["lifecycle_status"], "ACTIVE")

    def test_nsp_card_matching_uses_student_deadline_and_guideline_identity(self):
        content = b"<html><div><h6>Other Scheme</h6><span>Student Application Open till : 30-12-2026</span></div><div><h6>National Scholarship For Post Graduate Studies (Merit Based Scheme)</h6><span>Scheme Open from : 16-09-2026</span><span>Student Application Open till : 31-10-2026</span><span>Institute Verification Open till : 15-11-2026</span><a href='/public/schemeGuidelines/ugc.pdf'>Specifications</a></div></html>"
        record = {"name": "National Scholarship for Postgraduate Studies", "provider_name": "University Grants Commission", "scholarship_id": 3,
                  "sources": [{"url": "https://scholarships.gov.in/public/schemeGuidelines/ugc.pdf"}]}
        context = pipeline.nsp_application_context(record, content, self.when.isoformat())
        self.assertEqual(context["closing_date"], "2026-10-31")
        self.assertEqual(context["opening_date"], "2026-09-16")
        self.assertEqual(len(context["evidence"]), 2)
        with self.assertRaises(ValueError):
            pipeline.nsp_application_context({**record, "sources": [{"url": "https://scholarships.gov.in/public/other.pdf"}]}, content, self.when.isoformat())
        with self.assertRaises(ValueError):
            pipeline.nsp_application_context(record, content.replace(b"Student Application Open till", b"Institute Verification Open till"), self.when.isoformat())
        with self.assertRaises(ValueError):
            pipeline.nsp_application_context({**record, "cycle": "2024-25"}, content, self.when.isoformat())

    def test_availability_persistence_is_bound_idempotent_and_detects_tampering(self):
        pipeline.run_seed_workflow("iit_delhi", pilot_file=self.pilot, limit=1, database_path=self.database, project_root=self.root)
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute("UPDATE programmes SET name='National Scholarship for Postgraduate Studies', provider_name='University Grants Commission'")
            connection.execute("UPDATE sources SET url='https://scholarships.gov.in/public/schemeGuidelines/ugc.pdf'")
        content = b"<html><div><h6>National Scholarship For Post Graduate Studies (Merit Based Scheme)</h6><span>Scheme Open from : 16-09-2026</span><span>Student Application Open till : 31-10-2026</span><span>Institute Verification Open till : 15-11-2026</span><a href='/public/schemeGuidelines/ugc.pdf'>Specifications</a></div></html>"
        result = pipeline.refresh_availability(apply=True, database_path=self.database, project_root=self.root, saved_content=content, retrieved_at=self.when.isoformat())
        self.assertEqual(result["database_rows_changed"], 1)
        again = pipeline.refresh_availability(apply=True, database_path=self.database, project_root=self.root, saved_content=content, retrieved_at=self.when.isoformat())
        self.assertEqual(again["database_rows_changed"], 0)
        row = dashboard.load_records(self.database, project_root=self.root)[0]
        self.assertEqual(row["closing_date"], "2026-10-31")
        self.assertFalse(row["availability_verified"])  # old source retrieval
        self.assertEqual(row["verification_status"], "REVIEW_REQUIRED")
        packet = row["details"]["application_context"]
        receipt = self.root / packet["receipt_path"]
        receipt.write_bytes(receipt.read_bytes() + b" ")
        row = dashboard.load_records(self.database, project_root=self.root)[0]
        self.assertFalse(row["availability_verified"])
        self.assertIn("availability_error", row)

    def test_partially_quoted_eligibility_section_requires_review(self):
        from datetime import timedelta
        from scholarship_intelligence.confidence import review_python_source
        from scholarship_intelligence.models import ScholarshipExtraction
        candidate, _, bundle, times = self._source_review_fixture()
        payload = candidate.model_dump(mode="json")
        evidence = next(entry for entry in payload["evidence"] if entry["evidence_id"] in payload["eligibility"]["evidence_ids"])
        evidence["quote"] = "first-year students"
        payload["eligibility"]["value"]["summary"] = "first-year students"
        payload["eligibility"]["value"]["conditions"]["statement"] = "first-year students"
        review = review_python_source(ScholarshipExtraction.model_validate(payload), bundle, self.seed, times, project_root=self.root, now=self.when + timedelta(days=1))
        self.assertEqual(review["fields"]["eligibility"]["status"], "unresolved")
        self.assertIn("completeness", review["fields"]["eligibility"]["reason"])

    def test_source_score_saves_all_and_repeat_is_idempotent(self):
        pipeline.run_seed_workflow("iit_delhi", pilot_file=self.pilot, limit=1000,
                                   database_path=self.database, project_root=self.root)
        first = pipeline.score_seed_scholarships("iit_delhi", apply=True,
                    database_path=self.database, project_root=self.root)
        self.assertEqual(first["scholarships_scored"], 2)
        self.assertEqual(first["application_status_counts"], {"REVIEW_REQUIRED": 2})
        self.assertGreater(first["score_minimum"], 0)
        self.assertEqual(first["verified"], 0)
        for item in first["scholarships"]:
            expected = round(sum(metric["weight"] * metric["score"] for metric in item["metrics"]) / 100, 2)
            self.assertEqual(item["confidence_score"], expected)
            self.assertEqual(item["verification_confidence_score"], item["confidence_score"])
        inspected = pipeline.inspect_scholarship(first["scholarships"][0]["scholarship_id"], database_path=self.database, project_root=self.root)
        self.assertEqual(inspected["evidence_confidence_score"], first["scholarships"][0]["confidence_score"])
        again = pipeline.score_seed_scholarships("iit_delhi", apply=True,
                    database_path=self.database, project_root=self.root)
        self.assertEqual(again["database_rows_changed"], 0)
        rows = dashboard.load_records(self.database, project_root=self.root)
        self.assertTrue(all(row.get("source_score") for row in rows))
        self.assertTrue(all(row["confidence_score"] is not None for row in rows))
        self.assertTrue(all(row["opening_date"] is None for row in rows))
        packet = rows[0]["details"]["python_source_assessment"]
        path = self.root / packet["candidate_path"]
        path.write_bytes(path.read_bytes() + b" ")
        rows = dashboard.load_records(self.database, project_root=self.root)
        self.assertIsNone(rows[0]["confidence_score"])
        self.assertIn("source_assessment_error", rows[0])
        result = pipeline.score_seed_scholarships("iit_delhi", apply=True,
                    database_path=self.database, project_root=self.root)
        self.assertEqual(result["scholarships_scored"], 1)
        self.assertEqual(result["unscored_records"], 1)
        self.assertEqual(result["evidence_failures"], 1)
        invalid = dashboard.load_records(self.database, project_root=self.root)[0]
        self.assertIsNone(invalid["confidence_score"])

    def test_lifecycle_proof_and_qualified_expiry(self):
        from datetime import date
        from scholarship_intelligence.scholarship_storage import reviewed_lifecycle
        fields = {"opening_date": {"value": {"calendar_date": "2026-01-01", "stage": "student_application"}},
                  "closing_date": {"value": {"calendar_date": "2026-02-01", "stage": "student_application"}}}
        self.assertEqual(reviewed_lifecycle(fields, "VERIFIED", today=date(2026, 1, 15)), "ACTIVE")
        self.assertEqual(reviewed_lifecycle(fields, "VERIFIED", today=date(2026, 2, 2)), "EXPIRED")
        self.assertEqual(reviewed_lifecycle(fields, "REVIEW_REQUIRED", today=date(2026, 1, 15)), "REVIEW_REQUIRED")
        fields["closing_date"]["value"]["route"] = "Department nominations"
        self.assertEqual(reviewed_lifecycle(fields, "VERIFIED", today=date(2026, 2, 2)), "REVIEW_REQUIRED")


    def test_score_write_rolls_back_on_concurrent_review_change(self):
        pipeline.run_seed_workflow("iit_delhi", pilot_file=self.pilot, limit=1000,
                                   database_path=self.database, project_root=self.root)
        with closing(sqlite3.connect(self.database)) as connection, connection:
            for identity, content in connection.execute("SELECT scholarship_id, verification_details_json FROM scholarships").fetchall():
                details = json.loads(content)
                details.pop("python_source_assessment", None)
                connection.execute("UPDATE scholarships SET verification_details_json=? WHERE scholarship_id=?", (json.dumps(details), identity))
        original = pipeline.check_seed_scholarships
        def changed_review(*args, **kwargs):
            audit = original(*args, **kwargs)
            with closing(sqlite3.connect(self.database)) as connection, connection:
                identity, content = connection.execute("SELECT scholarship_id, verification_details_json FROM scholarships ORDER BY scholarship_id DESC LIMIT 1").fetchone()
                details = json.loads(content)
                details["concurrent_change"] = True
                connection.execute("UPDATE scholarships SET verification_details_json=? WHERE scholarship_id=?", (json.dumps(details), identity))
            return audit
        with patch.object(pipeline, "check_seed_scholarships", side_effect=changed_review):
            with self.assertRaisesRegex(ValueError, "transaction rolled back"):
                pipeline.score_seed_scholarships("iit_delhi", apply=True, database_path=self.database, project_root=self.root)
        with closing(sqlite3.connect(self.database)) as connection, connection:
            details = [json.loads(row[0]) for row in connection.execute("SELECT verification_details_json FROM scholarships")]
        self.assertTrue(all("python_source_assessment" not in item for item in details))


    def _source_review_fixture(self):
        from scholarship_intelligence.models import ScholarshipExtraction
        report = pipeline.run_seed_workflow("iit_delhi", pilot_file=self.pilot, limit=1,
                                           database_path=self.database, project_root=self.root)
        row = report["offerings"][0]
        candidate = ScholarshipExtraction.model_validate_json((self.root / row["candidate_path"]).read_bytes())
        from scholarship_intelligence.semantic_assessment import SemanticAssessment
        assessment = SemanticAssessment.model_validate(row["assessment"])
        bundle = self.root / "data/runtime/bundles" / (candidate.bundle_id + ".json")
        return candidate, assessment, bundle, {identity: self.when.isoformat() for identity in candidate.snapshot_ids}

    def test_optional_absence_is_scoped_but_undated_context_prevents_high_confidence(self):
        from datetime import timedelta
        from scholarship_intelligence.confidence import review_python_source, score_source_evidence
        candidate, assessment, bundle, times = self._source_review_fixture()
        review = review_python_source(candidate, bundle, self.seed, times, project_root=self.root, now=self.when + timedelta(days=1))
        score = score_source_evidence(candidate, assessment, evidence_checks_passed=True, source_review=review)
        self.assertEqual(score["confidence_score"], 80)
        self.assertEqual(score["verification_status"], "REVIEW_REQUIRED")
        self.assertTrue(any(item.startswith("offering_context:") for item in score["blockers"]))
        self.assertEqual(score["field_assessments"]["documents"]["status"], "not_specified")
        self.assertEqual(review["lifecycle_status"], "REVIEW_REQUIRED")
        self.assertFalse(review["eligibility_decisions_supported"])
        stale = review_python_source(candidate, bundle, self.seed, times, project_root=self.root, now=self.when + timedelta(days=31))
        stale_score = score_source_evidence(candidate, assessment, evidence_checks_passed=True, source_review=stale)
        self.assertEqual(stale_score["confidence_score"], 80)
        self.assertEqual(stale["source_freshness"], "STALE")
        self.assertEqual(stale_score["verification_status"], "REVIEW_REQUIRED")

    def test_exact_quote_in_wrong_field_context_cannot_verify(self):
        from datetime import timedelta
        from scholarship_intelligence.confidence import review_python_source, score_source_evidence
        from scholarship_intelligence.evidence_validation import validate_evidence
        from scholarship_intelligence.models import ScholarshipExtraction
        from scholarship_intelligence.semantic_assessment import candidate_fingerprint
        candidate, assessment, bundle, times = self._source_review_fixture()
        payload = candidate.model_dump(mode="json")
        selection = next(item for item in payload["evidence"] if item["evidence_id"] in payload["selection_process"]["evidence_ids"])
        eligibility = next(item for item in payload["evidence"] if item["evidence_id"] in payload["eligibility"]["evidence_ids"])
        eligibility.update(quote=selection["quote"], location=selection["location"])
        payload["eligibility"]["value"]["summary"] = selection["quote"]
        payload["eligibility"]["value"]["conditions"]["statement"] = selection["quote"]
        changed = ScholarshipExtraction.model_validate(payload)
        self.assertTrue(validate_evidence(changed, bundle, project_root=self.root).evidence_checks_passed)
        bound = assessment.model_copy(update={"candidate_fingerprint": candidate_fingerprint(changed)})
        review = review_python_source(changed, bundle, self.seed, times, project_root=self.root, now=self.when + timedelta(days=1))
        score = score_source_evidence(changed, bound, evidence_checks_passed=True, source_review=review)
        self.assertEqual(review["fields"]["eligibility"]["status"], "unsupported")
        self.assertLess(score["confidence_score"], 95)
        self.assertEqual(score["verification_status"], "REVIEW_REQUIRED")

    def test_page_url_is_not_an_application_link(self):
        from scholarship_intelligence.models import ScholarshipExtraction
        from scholarship_intelligence.evidence_validation import validate_evidence
        candidate, _, bundle, _ = self._source_review_fixture()
        payload = candidate.model_dump(mode="json")
        evidence = next(item for item in payload["evidence"] if item["evidence_id"] in payload["official_source"]["evidence_ids"])
        payload["evidence"].append({**evidence, "evidence_id": "fake_apply", "field_paths": ["application_url"]})
        payload["application_url"].update(value=payload["official_source"]["value"], missing_reason=None, explanation=None, evidence_ids=["fake_apply"])
        changed = ScholarshipExtraction.model_validate(payload)
        checked = validate_evidence(changed, bundle, project_root=self.root)
        self.assertIn("unsupported_url", [item.code for item in checked.issues])


    def test_document_requirement_in_eligibility_is_extracted_and_dates_are_independent(self):
        from datetime import timedelta
        from scholarship_intelligence.confidence import review_python_source, score_source_evidence
        self.snapshots[0] = save_raw_snapshot("https://alumni.iitd.ac.in/scholarships-awards/930",
            b"<html><body><h1>TEST Scholarship 930</h1><p>IIT Delhi</p><p>Eligibility Criteria</p><p>First-year students.</p><p>Income certificate must be attached.</p><p>Applications open on 1 January 2026 and closes on 1 February 2026.</p></body></html>",
            self.when, "text/html", project_root=self.root)
        self.write_pilot()
        candidate, assessment, bundle, times = self._source_review_fixture()
        review = review_python_source(candidate, bundle, self.seed, times, project_root=self.root, now=self.when + timedelta(days=14))
        score = score_source_evidence(candidate, assessment, evidence_checks_passed=True, source_review=review)
        self.assertEqual(review["fields"]["documents"]["status"], "supported")
        self.assertEqual(score["verification_status"], "REVIEW_REQUIRED")
        self.assertLess(score["confidence_score"], 95)
        self.assertEqual(review["lifecycle_status"], "ACTIVE")
        later = review_python_source(candidate, bundle, self.seed, times, project_root=self.root, now=self.when + timedelta(days=32))
        self.assertEqual(later["lifecycle_status"], "EXPIRED")

    def test_nulling_a_present_source_field_cannot_earn_high_confidence(self):
        from datetime import timedelta
        from scholarship_intelligence.confidence import review_python_source, score_source_evidence
        from scholarship_intelligence.models import ScholarshipExtraction
        from scholarship_intelligence.semantic_assessment import candidate_fingerprint
        candidate, assessment, bundle, times = self._source_review_fixture()
        payload = candidate.model_dump(mode="json")
        payload["benefits"].update(value=None, missing_reason="not_specified_in_inspected_material", explanation="Absent", evidence_ids=[])
        changed = ScholarshipExtraction.model_validate(payload)
        bound = assessment.model_copy(update={"candidate_fingerprint": candidate_fingerprint(changed)})
        review = review_python_source(changed, bundle, self.seed, times, project_root=self.root, now=self.when + timedelta(days=1))
        score = score_source_evidence(changed, bound, evidence_checks_passed=True, source_review=review)
        self.assertEqual(review["fields"]["benefits"]["status"], "unresolved")
        self.assertLess(score["confidence_score"], 95)


    def test_duplicate_citation_is_not_missing_source_content(self):
        from datetime import timedelta
        from scholarship_intelligence.confidence import review_python_source
        from scholarship_intelligence.models import ScholarshipExtraction
        candidate, _, bundle, times = self._source_review_fixture()
        payload = candidate.model_dump(mode="json")
        original = next(entry for entry in payload["evidence"] if entry["evidence_id"] in payload["eligibility"]["evidence_ids"])
        duplicate = {**original, "evidence_id": original["evidence_id"] + "_duplicate"}
        payload["evidence"].append(duplicate)
        payload["eligibility"]["evidence_ids"].append(duplicate["evidence_id"])
        summary = payload["eligibility"]["value"]["summary"]
        payload["eligibility"]["value"]["summary"] = summary + "\n" + summary
        payload["eligibility"]["value"]["conditions"]["statement"] = summary + "\n" + summary
        review = review_python_source(ScholarshipExtraction.model_validate(payload), bundle, self.seed, times, project_root=self.root, now=self.when + timedelta(days=1))
        self.assertEqual(review["fields"]["eligibility"]["status"], "supported")

    def test_document_upload_threshold_is_not_a_benefit(self):
        from scholarship_intelligence.source_profiles import inline_role, source_sections
        text="Documents are required to be uploaded only for the scholarship amount more than Rs. 50000 per annum."
        self.assertEqual(inline_role(text), "documents")
        blocks=[{"kind":"heading","text":"Benefits","heading_path":["Benefits"]},{"kind":"paragraph","text":text,"heading_path":["Benefits"]}]
        sections=source_sections(blocks)
        self.assertNotIn("benefits",sections)
        self.assertEqual(sections["documents"][0]["text"],text)

    def test_non_monetary_payment_period_is_retained(self):
        from scholarship_intelligence.python_extraction import _benefit
        self.assertEqual(_benefit("Tuition reimbursement for the duration of the course")["period"],"whole_course")
        self.assertEqual(_benefit("One-time book assistance")["period"],"one_time")
        self.assertIsNone(_benefit("Tuition reimbursement for the duration of the course")["amount"])


class MultiSourceTests(unittest.TestCase):
    write_pilot = PythonWorkflowTests.write_pilot
    setUp = PythonWorkflowTests.setUp
    tearDown = PythonWorkflowTests.tearDown

    def test_card_workflow_isolates_programmes_and_does_not_invent_opening(self):
        self.when = datetime.now(timezone.utc)
        seed = {"seed_id": "hdfc_ecss", "start_url": "https://www.parivartanecss.com/",
                "provider_name": "HDFC Bank", "source_category": "corporate_csr", "allowed_hosts": ["www.parivartanecss.com"],
                "application_hosts": ["forms.buddy4study.com"], "workflow": {"cards": True}, "extraction_profile": "hdfc_cards_v1"}
        (self.root / "config/seeds.json").write_text(json.dumps({"seeds": [seed]}))
        cards = []
        for number, amount, end in [(1, 15000, "31st October 2026"), (2, 30000, "30th November 2026")]:
            cards.append(f"<div class='accordion-item'><h2><button>ECSS Programme {number} 2026-27</button> <a href='https://forms.buddy4study.com/application/{number}'>Apply Now</a></h2><h3>Eligibility</h3><p>Students in course {number}.</p><h3>Benefits:</h3><p>INR {amount}</p><h3>Deadline</h3><p>{end}</p></div>")
        raw = save_raw_snapshot(seed["start_url"], ("<html><body><p>HDFC Bank scholarship initiative</p>"+"".join(cards)+"</body></html>").encode(), self.when, "text/html", project_root=self.root)
        self.pilot.write_text(json.dumps({"event":"fetched","seed_id":"hdfc_ecss","source_category":"corporate_csr","http_status":200,"snapshot":asdict(raw)})+"\n")
        report = pipeline.run_seed_workflow("hdfc_ecss", pilot_file=self.pilot, limit=1000, database_path=self.database, project_root=self.root)
        self.assertEqual(report["counts"], {"stored": 2}, report)
        self.assertEqual(report["evidence_scoring"]["verified"], 2, report)
        for number, row in enumerate(report["offerings"], 1):
            candidate = json.loads((self.root / row["candidate_path"]).read_text())
            self.assertEqual(candidate["benefits"]["value"][0]["amount"], str(15000*number))
            self.assertIsNone(candidate["opening_date"]["value"])
            self.assertTrue(candidate["application_url"]["value"].endswith(str(number)))
        repeated = pipeline.run_seed_workflow("hdfc_ecss", pilot_file=self.pilot, limit=1000, reextract=True, database_path=self.database, project_root=self.root)
        self.assertEqual(repeated["scholarship_rows_changed"], 0)

    def test_bad_support_pdf_retains_primary_html_and_records_drawback(self):
        pdf = save_raw_snapshot("https://alumni.iitd.ac.in/bad.pdf", b"%PDF-broken", self.when, "application/pdf", project_root=self.root)
        with self.pilot.open("a") as output:
            output.write(json.dumps({"event":"fetched","seed_id":"iit_delhi","http_status":200,"snapshot":asdict(pdf)})+"\n")
        result = pipeline.prepare_crawl_bundle(self.pilot, [self.snapshots[0].source_url, pdf.source_url], "supported-html", allow_support_failures=True, project_root=self.root)
        self.assertEqual(result["documents"], 1)
        self.assertEqual(result["support_failures"][0]["reason"], "preparation_failed")
        with self.assertRaises(ValueError):
            pipeline.prepare_crawl_bundle(self.pilot, [self.snapshots[0].source_url, pdf.source_url], "strict", project_root=self.root)

    def test_student_open_till_is_a_closing_event_not_an_opening_event(self):
        from scholarship_intelligence.python_extraction import _application_dates
        from types import SimpleNamespace
        block = {"block_id":"b1","kind":"text","text":"Scheme Open from : 01-06-2026\nStudent Application Open till : 31-10-2026\nInstitute Verification Open till : 15-11-2026"}
        dates, issues = _application_dates({"https://example.org": {"blocks":[block]}}, {"https://example.org":SimpleNamespace(snapshot_id="s1")},date_order="DMY")
        self.assertEqual([item[0]["calendar_date"] for item in dates["opening_date"]], ["2026-06-01"])
        self.assertEqual([item[0]["calendar_date"] for item in dates["closing_date"]], ["2026-10-31"])

    def test_pdf_wrapped_payment_keeps_duration_separate_from_amount_operator(self):
        from scholarship_intelligence.python_extraction import _source_views, _benefit
        block = {"block_id":"p1","kind":"pdf_page","page_number":1,"text":"4.0 AMOUNT OF SCHOLARSHIP:\n1) Rs. 50,000/- per  annum for maximum 4 years\n   through the course of study.\n5.0 MODE OF PAYMENT:\nPaid through DBT."}
        views = _source_views([block])
        item = _benefit(views[1]["text"])
        self.assertEqual(item["amount_kind"], "fixed")
        self.assertEqual(item["amount"], "50000")
        self.assertEqual(item["period"], "annual")
        self.assertIn("through the course", item["description"])
        self.assertIn(item["description"], block["text"])

    def test_cached_preparation_rechecks_saved_bytes(self):
        from scholarship_intelligence.preparation import prepare_snapshot
        result = prepare_snapshot(self.snapshots[0],200,project_root=self.root)
        path = self.root / result.prepared_file_path
        original = path.read_bytes()
        path.write_bytes(original.replace(b"TEST",b"FAKE",1))
        with self.assertRaises(ValueError):
            prepare_snapshot(self.snapshots[0],200,project_root=self.root)

    def test_official_page_h1_wins_over_an_additional_awards_subsection(self):
        from scholarship_intelligence.python_extraction import extract_bundle
        seed = {"seed_id":"chevening_india","extraction_profile":"official_sections_v1","provider_name":"Chevening","allowed_hosts":["www.chevening.org"]}
        raw=save_raw_snapshot("https://www.chevening.org/scholarship/india/", b"<html><body><main><h1>Chevening in India</h1><p>Chevening scholarship programme.</p><h2>Additional awards</h2><p>Other opportunities.</p></main></body></html>", self.when,"text/html",project_root=self.root)
        self.pilot.write_text(json.dumps({"event":"fetched","seed_id":"chevening_india","source_category":"international","http_status":200,"snapshot":asdict(raw)})+"\n")
        result=pipeline.prepare_crawl_bundle(self.pilot,[raw.source_url],"Chevening India",project_root=self.root)
        candidate,details=extract_bundle(self.root/result["bundle_file_path"],seed,raw.source_url,project_root=self.root)
        self.assertEqual(candidate.scholarship_name.value,"Chevening in India")
        self.assertEqual(details["offering_classification"]["offering_type"],"scholarship")
