"""Network/preview/cache regression tests; no external websites are used."""
from contextlib import closing, redirect_stdout
from datetime import datetime, timezone
from hashlib import sha256
from io import StringIO
import json
import gzip
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import zipfile

from scholarship_intelligence.http_cache import ResponseCache
from scholarship_intelligence import bootstrap, pipeline, python_extraction, reuse
from scholarship_intelligence.storage import initialize_database
import test_python_extraction as fixtures


class HTTPTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory(dir=Path(__file__).parent, prefix='cache-test-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.cache = ResponseCache(self.root)
        self.url = 'https://example.test/list'
        self.session = Mock()
        self.session.headers = {}

    def response(self, status=200, body=b'official source', **headers):
        return SimpleNamespace(status_code=status, content=body, url=self.url,
                               headers={'Content-Type': 'text/html', **headers})

    def fetch(self, method='GET', form=None):
        return self.cache.fetch(self.session, method, self.url, form, self.url, 'test')

    def test_304_reuses_verified_bytes_and_preserves_download_date(self):
        self.session.request.return_value = self.response(ETag='"v1"')
        _, first, _, _ = self.fetch()
        self.session.request.return_value = self.response(304, b'')
        response, second, status, state = self.fetch()
        self.assertEqual(response.content, b'official source')
        self.assertEqual((status, state), (304, 'not_modified'))
        self.assertEqual(first.content_hash, second.content_hash)
        self.assertEqual(self.session.request.call_args.kwargs['headers']['If-None-Match'], '"v1"')
        self.assertEqual(self.cache.entries[self.url]['body_downloaded_at'], first.retrieved_at)

    def test_last_modified_validator_is_sent(self):
        self.session.request.return_value = self.response(**{'Last-Modified': 'Wed, 14 Oct 2026 10:00:00 GMT'})
        self.fetch()
        self.fetch()
        self.assertIn('If-Modified-Since', self.session.request.call_args.kwargs['headers'])

    def test_200_same_hash_reuses_extraction_identity(self):
        self.session.request.return_value = self.response()
        self.fetch()
        self.assertEqual(self.fetch()[3], 'unchanged_hash')

    def test_changed_body_gets_a_new_hash(self):
        self.session.request.return_value = self.response()
        first = self.fetch()[1]
        self.session.request.return_value = self.response(body=b'new deadline')
        _, later, _, state = self.fetch()
        self.assertNotEqual(first.content_hash, later.content_hash)
        self.assertEqual(state, 'changed_or_new')

    def test_corrupt_body_never_supports_a_304(self):
        self.session.request.return_value = self.response(ETag='v1')
        first = self.fetch()[1]
        (self.root / first.raw_file_path).write_bytes(b'corrupt')
        self.session.request.side_effect = [self.response(304, b''), self.response(body=b'new valid source')]
        self.assertEqual(self.fetch()[0].content, b'new valid source')
        self.assertNotIn('headers', self.session.request.call_args.kwargs)

    def test_post_requests_never_use_get_validators(self):
        self.session.request.return_value = self.response(ETag='v1')
        self.fetch()
        self.fetch('POST', {'department': '1'})
        self.assertNotIn('headers', self.session.request.call_args.kwargs)

    def test_failed_check_does_not_replace_saved_body(self):
        self.session.request.return_value = self.response()
        first = self.fetch()[1]
        self.session.request.return_value = self.response(404, b'not found')
        self.fetch()
        entry = self.cache.entries[self.url]
        self.assertEqual(entry['snapshot']['content_hash'], first.content_hash)
        self.assertEqual(entry['status'], 'failed')

    def test_known_get_pages_remain_in_next_frontier(self):
        self.session.request.return_value = self.response()
        self.fetch()
        self.assertEqual(ResponseCache(self.root).known('test'), [self.url])

    def test_refreshed_listing_discovers_new_page_while_old_page_is_304(self):
        base = 'https://alumni.iitd.ac.in'
        listing = base + '/scholarships-awards'
        seed = {'seed_id': 'iit_delhi', 'start_url': listing, 'allowed_hosts': ['alumni.iitd.ac.in'],
                'download_delay': 0, 'max_depth': 3, 'provider_name': 'IIT Delhi'}
        (self.root/'config').mkdir()
        (self.root/'config/seeds.json').write_text(json.dumps({'seeds':[seed]}))
        phase = [0]
        def request(method, url, **kwargs):
            if url == listing:
                links = '<a href="/scholarships-awards/930">Scholarship</a>'
                if phase[0]:
                    links += '<a href="/scholarships-awards/931">New scholarship</a>'
                return SimpleNamespace(status_code=200, content=links.encode(), url=url, headers={'ETag': 'listing'})
            status = 304 if phase[0] and url.endswith('/930') else 200
            return SimpleNamespace(status_code=status, content=b'<h1>Scholarship</h1>', url=url, headers={'ETag':'detail'})
        self.session.request.side_effect = request
        self.session.get.return_value = SimpleNamespace(status_code=200, text='User-agent: *\nAllow: /')
        with patch('requests.Session', return_value=self.session):
            pipeline.crawl_sources(seed_id='iit_delhi', project_root=self.root)
            phase[0] = 1
            result = pipeline.crawl_sources(seed_id='iit_delhi', project_root=self.root)
        events = [json.loads(line) for line in (self.root/result['pilot_file']).read_text(encoding='utf-8').splitlines()]
        self.assertTrue(any(e.get('network_status') == 304 for e in events))
        self.assertTrue(any(e.get('snapshot',{}).get('source_url','').endswith('/931') for e in events))


class ReuseTests(unittest.TestCase):
    setUp = fixtures.PythonWorkflowTests.setUp
    write_pilot = fixtures.PythonWorkflowTests.write_pilot
    run_batch = fixtures.PythonWorkflowTests.run_batch

    def packet(self):
        with closing(sqlite3.connect(self.database)) as db:
            return json.loads(db.execute('SELECT verification_details_json FROM scholarships LIMIT 1').fetchone()[0])['python_source_assessment']

    def test_unchanged_full_run_avoids_extraction_and_context_review(self):
        self.run_batch(pilot_file=self.pilot)
        with patch.object(python_extraction, 'extract_bundle', side_effect=AssertionError('Repeated extraction')):
            with patch('scholarship_intelligence.confidence.review_python_source', side_effect=AssertionError('Repeated context review')):
                result = self.run_batch(pilot_file=self.pilot)
        self.assertEqual(result['counts'], {'unchanged': 2})
        self.assertTrue(all(row['reused_validated'] for row in result['offerings']))

    def test_seed_configuration_change_invalidates_cache(self):
        self.run_batch(pilot_file=self.pilot)
        with self.assertRaises(ValueError):
            reuse.checked(self.packet(), {**self.seed, 'provider_name': 'Different provider'}, self.root)

    def test_changed_candidate_or_receipt_cannot_reuse(self):
        self.run_batch(pilot_file=self.pilot)
        packet = self.packet()
        (self.root / packet['candidate_path']).write_bytes(b'{}')
        with self.assertRaises(ValueError):
            reuse.checked(packet, self.seed, self.root)

    def test_review_tampering_invalidates_receipt(self):
        self.run_batch(pilot_file=self.pilot)
        packet = self.packet()
        packet['source_review']['official_ownership_passed'] = False
        with self.assertRaises(ValueError):
            reuse.checked(packet, self.seed, self.root)

    def test_time_changes_recalculate_expiry_without_extracting(self):
        self.run_batch(pilot_file=self.pilot)
        packet = self.packet()
        from scholarship_intelligence.confidence import redate_source_review
        candidate, _ = reuse.checked(packet, self.seed, self.root)
        later = redate_source_review(candidate, packet['source_review'], now=datetime(2027, 1, 1, tzinfo=timezone.utc))
        self.assertEqual(later['source_freshness'], 'STALE')

    def test_failed_source_is_flagged_and_never_assumed_expired(self):
        from scholarship_intelligence.dashboard import load_records
        self.run_batch(pilot_file=self.pilot)
        (self.root/'data/runtime/http-cache.json').write_text(json.dumps({self.snapshots[0].source_url: {
            'url': self.snapshots[0].source_url, 'status': 'failed', 'error': 'Timeout'}}))
        record = load_records(self.database, project_root=self.root)[0]
        self.assertEqual(record['lifecycle_status'], 'NO_LONGER_VERIFIABLE')
        self.assertIsNone(record['confidence_score'])

    def test_existing_user_records_are_never_overwritten_by_preview(self):
        self.run_batch(pilot_file=self.pilot)
        before = self.database.read_bytes()
        (self.root/'data/samples').mkdir()
        (self.root/'data/samples/saved-results.manifest.json').write_text('{}')
        self.assertFalse(bootstrap.ensure_saved_results(project_root=self.root, database_path=self.database))
        self.assertEqual(self.database.read_bytes(), before)

    def test_code_fingerprint_change_blocks_validated_reuse(self):
        self.run_batch(pilot_file=self.pilot)
        packet = self.packet()
        with patch.object(reuse, 'fingerprint', return_value='new-policy'):
            with self.assertRaises(ValueError):
                reuse.checked(packet, self.seed, self.root)

    def saved_view(self):
        from scholarship_intelligence.dashboard import load_records
        from scholarship_intelligence import preview
        self.run_batch(pilot_file=self.pilot)
        records = load_records(self.database, project_root=self.root)
        folder = self.root/'data/samples'
        folder.mkdir(exist_ok=True)
        content = gzip.compress(json.dumps({'records':records,
            'processing_fingerprints':{self.seed['seed_id']:reuse.fingerprint(self.seed)}}).encode())
        (folder/'saved-view.json.gz').write_bytes(content)
        (folder/'saved-results.manifest.json').write_text(json.dumps({'preview_sha256':sha256(content).hexdigest(),
            'files':{'data/runtime/scholarships.sqlite3':sha256(self.database.read_bytes()).hexdigest()}}))
        self.addCleanup(preview._VIEWS.pop, self.root, None)
        return records

    def test_saved_view_keeps_original_verification_and_retrieval_dates(self):
        from scholarship_intelligence import preview
        old = self.saved_view()
        with patch('requests.Session', side_effect=AssertionError('Preview must not access network')):
            shown = preview.records(self.root)
        self.assertEqual(shown[0]['last_verified_at'], old[0]['last_verified_at'])
        self.assertEqual(shown[0]['source_review']['retrieval_times'], old[0]['source_review']['retrieval_times'])

    def test_saved_view_is_invalidated_after_database_changes(self):
        from scholarship_intelligence import preview
        self.saved_view()
        (self.root/'data/runtime/saved-results-origin.json').write_text('{}')
        self.assertTrue(preview.available(self.root))
        with closing(sqlite3.connect(self.database)) as db:
            db.execute("UPDATE programmes SET name=name || ' revised'")
            db.commit()
        self.assertFalse(preview.available(self.root))

    def test_preview_policy_change_cannot_keep_verified_scores(self):
        from scholarship_intelligence import preview
        self.saved_view()
        with patch.object(reuse, 'fingerprint', return_value='changed-policy'):
            records = preview.records(self.root)
        self.assertTrue(all(r['confidence_score'] is None and r['verification_status']=='REVIEW_REQUIRED' for r in records))

    def test_shared_document_tampering_is_detected_within_one_read(self):
        from scholarship_intelligence.bundles import document_checks
        self.run_batch(pilot_file=self.pilot)
        packet = self.packet()
        with document_checks():
            reuse.checked(packet, self.seed, self.root)
            (self.root/self.snapshots[0].raw_file_path).write_bytes(b'changed source bytes')
            with self.assertRaises(ValueError):
                reuse.checked(packet, self.seed, self.root)


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory(dir=Path(__file__).parent, prefix='preview-test-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / 'data/samples').mkdir(parents=True)
        db_path = initialize_database(self.root / 'source.sqlite3')
        self.body = db_path.read_bytes()

    def bundle(self, name='data/runtime/scholarships.sqlite3'):
        path = self.root / 'data/samples/saved-results.zip'
        with zipfile.ZipFile(path, 'w') as output:
            output.writestr(name, self.body)
        manifest = {'archive_sha256': sha256(path.read_bytes()).hexdigest(),
                    'source_checked_at': '2026-01-01T00:00:00+00:00',
                    'files': {name: sha256(self.body).hexdigest()}}
        (path.parent / 'saved-results.manifest.json').write_text(json.dumps(manifest), encoding='utf-8')

    def install(self):
        return bootstrap.ensure_saved_results(project_root=self.root, database_path=self.root/'data/runtime/scholarships.sqlite3')

    def test_preview_preserves_actual_source_date(self):
        self.bundle()
        self.assertTrue(self.install())
        receipt = json.loads((self.root/'data/runtime/saved-results-origin.json').read_bytes())
        self.assertEqual(receipt['source_checked_at'], '2026-01-01T00:00:00+00:00')

    def test_archive_tampering_is_rejected(self):
        self.bundle()
        path = self.root / 'data/samples/saved-results.zip'
        path.write_bytes(path.read_bytes()+b'changed')
        with self.assertRaises(ValueError):
            self.install()

    def test_path_traversal_is_rejected(self):
        self.bundle('data/runtime/../../outside.txt')
        with self.assertRaises(ValueError):
            self.install()
        self.assertFalse((self.root/'outside.txt').exists())

    def test_active_crawl_prevents_preview_install(self):
        self.bundle()
        runtime=self.root/'data/runtime'
        runtime.mkdir()
        (runtime/'crawl.lock').write_text('{}')
        self.assertFalse(self.install())

    def test_split_archive_is_loaded_automatically(self):
        self.bundle()
        folder = self.root/'data/samples'
        archive = folder/'saved-results.zip'
        raw = archive.read_bytes()
        manifest = json.loads((folder/'saved-results.manifest.json').read_bytes())
        manifest['archive_parts'] = []
        for index, part in enumerate((raw[:100], raw[100:])):
            name = f'saved-results.zip.part{index:02d}'
            (folder/name).write_bytes(part)
            manifest['archive_parts'].append({'name':name, 'sha256':sha256(part).hexdigest()})
        (folder/'saved-results.manifest.json').write_text(json.dumps(manifest))
        archive.unlink()
        self.assertTrue(self.install())
