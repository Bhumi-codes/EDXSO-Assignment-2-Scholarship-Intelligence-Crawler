import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from scholarship_intelligence import workflow_service as service
from scholarship_intelligence.settings import PROJECT_ROOT


class DashboardWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent, prefix="workflow-controls-")
        self.root = Path(self.temp.name)
        (self.root / 'config').mkdir()
        (self.root / 'config/seeds.json').write_text(json.dumps({'seeds': [{'seed_id': 'sample'}]}))

    def tearDown(self):
        self.temp.cleanup()

    def test_daily_schedule_uses_ist_and_catches_up_once(self):
        now = datetime(2026, 10, 14, 4, tzinfo=timezone.utc)
        self.assertEqual(service.next_daily(now), datetime(2026, 10, 15, 3, 30, tzinfo=timezone.utc))
        state = service.status(self.root)
        state['next_daily_at'] = (now-timedelta(days=1)).isoformat()
        service._write(self.root / 'data/runtime/crawl-control.json', state)
        with patch.object(service, 'launch') as launch:
            self.assertTrue(service.tick(project_root=self.root, now=now))
            launch.assert_called_once_with(trigger='daily', project_root=self.root)
        service.set_daily(False, self.root)
        with patch.object(service, 'launch') as launch:
            self.assertFalse(service.tick(project_root=self.root, now=now))
            launch.assert_not_called()

    def test_manual_job_is_async_and_blocks_overlapping_jobs(self):
        gate = threading.Event()
        def workflow(*args, **kwargs):
            gate.wait(3)
            return {'sources': [], 'api_calls_made': 0}
        from scholarship_intelligence.storage import initialize_database
        initialize_database(self.root / 'data/runtime/scholarships.sqlite3')
        with patch('scholarship_intelligence.pipeline.run_live_workflows', side_effect=workflow):
            try:
                job = service.launch(['sample'], project_root=self.root)
                self.assertEqual(job['status'], 'running')
                with self.assertRaises(RuntimeError):
                    service.launch(['sample'], project_root=self.root)
            finally:
                gate.set()
            for _ in range(100):
                if service.status(self.root)['job']['status'] != 'running':
                    break
                time.sleep(.02)
            self.assertEqual(service.status(self.root)['job']['status'], 'completed')
            self.assertFalse((self.root / 'data/runtime/dashboard-crawl.lock').exists())

    def test_dead_process_lock_is_recovered_but_live_lock_is_preserved(self):
        path = self.root / 'data/runtime/workflow.lock'
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({'pid': 100}))
        with patch.object(service, '_alive', return_value=True):
            service._recover(self.root)
            self.assertTrue(path.exists())
        with patch.object(service, '_alive', return_value=False):
            service._recover(self.root)
            self.assertFalse(path.exists())

    def test_dashboard_buttons_dispatch_shared_workflow_and_isolated_demo(self):
        from scholarship_intelligence.dashboard import _crawl_controls
        class Surface:
            def __init__(self, clicked):
                self.sidebar = self
                self.session_state = {}
                self.clicked = clicked
            def checkbox(self, label, **kwargs):
                return kwargs['value']
            def selectbox(self, label, options, **kwargs):
                return options[0]
            def button(self, label, **kwargs):
                return label == self.clicked and not kwargs.get('disabled')
            def fragment(self, **kwargs):
                return lambda function: function
            def expander(self, *args, **kwargs):
                return self
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass
            def __getattr__(self, name):
                return lambda *args, **kwargs: None
        state = {'daily_enabled': True, 'job': None}
        for clicked, arguments in [('Crawl now', {}), ('Run change/expiry demonstration', {'action': 'demonstration'})]:
            with patch.object(service, 'start'), patch.object(service, 'status', return_value=state), patch.object(service, 'launch', return_value={'id':'test', 'action':'crawl', 'status':'running'}) as launch:
                _crawl_controls(Surface(clicked))
                if clicked == 'Crawl now':
                    launch.assert_called_once_with(None)
                else:
                    launch.assert_not_called()  # The demonstration panel was removed from the dashboard.





if __name__ == '__main__':
    unittest.main()
