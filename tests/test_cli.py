"""Fresh-checkout entry points; network calls are mocked in unit tests."""
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from scholarship_intelligence import cli


class CommandTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory(dir=Path(__file__).parent, prefix="cli-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.addCleanup(patch.stopall)
        patch.object(cli, "PROJECT_ROOT", self.root).start()
        patch.object(cli, "DATABASE_PATH", self.root / "data/runtime/scholarships.sqlite3").start()

    def test_init_creates_database_without_credentials(self):
        with redirect_stdout(StringIO()):
            self.assertEqual(cli.main(["init-db"]), 0)
        self.assertTrue(cli.DATABASE_PATH.is_file())

    def test_start_runs_whole_pipeline_then_dashboard(self):
        result = {"operation": "live_source_workflows", "sources": [], "stage": "dashboard_updated"}
        with patch.object(cli.pipeline, "run_live_workflows", return_value=result) as run:
            with patch.object(cli.subprocess, "call", return_value=0) as serve, redirect_stdout(StringIO()):
                self.assertEqual(cli.main(["start", "--crawl"]), 0)
        run.assert_called_once_with(max_pages=1000)
        self.assertIn("streamlit", serve.call_args.args[0])
        self.assertTrue((self.root / "data/runtime/live-source-workflows.json").is_file())

    def test_open_existing_data_does_not_trigger_cli_crawl(self):
        with patch.object(cli.pipeline, "run_live_workflows") as run:
            with patch.object(cli.subprocess, "call", return_value=0):
                self.assertEqual(cli.main(["start", "--no-crawl"]), 0)
        run.assert_not_called()

    def test_source_failure_has_nonzero_exit_and_retained_report(self):
        result = {"operation": "live_source_workflows", "sources": [{"seed_id": "ffe", "stage": "failed"}]}
        with patch.object(cli.pipeline, "run_live_workflows", return_value=result), redirect_stdout(StringIO()):
            self.assertEqual(cli.main(["workflow-all"]), 1)
        self.assertTrue((self.root / "data/runtime/live-source-workflows.json").is_file())

    def test_manual_and_provider_commands_are_not_exposed(self):
        for name in ("manual-draft", "jobs", "demonstrate-changes", "run-one"):
            with self.subTest(command=name), redirect_stderr(StringIO()), self.assertRaises(SystemExit):
                cli.main([name])
