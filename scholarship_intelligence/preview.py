"""Immediate genuine saved results while the evidence archive restores."""
from copy import deepcopy
from datetime import date, datetime, timezone, timedelta
import gzip
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
from contextlib import closing
import threading

from scholarship_intelligence.settings import PROJECT_ROOT

_GUARD = threading.RLock()
_STATE = {}
_VIEWS = {}


def preparing(root=PROJECT_ROOT):
    return _STATE.get(Path(root).resolve(), {}).get('status') == 'preparing'


def error(root=PROJECT_ROOT):
    return _STATE.get(Path(root).resolve(), {}).get('error')


def available(root=PROJECT_ROOT):
    """The bundled view applies only until the actual database changes."""
    root = Path(root).resolve()
    if preparing(root):
        return True
    manifest = root / 'data/samples/saved-results.manifest.json'
    database = root / 'data/runtime/scholarships.sqlite3'
    if not (root / 'data/runtime/saved-results-origin.json').exists() or not manifest.exists() or not database.exists():
        return False
    expected = json.loads(manifest.read_bytes())['files'].get('data/runtime/scholarships.sqlite3')
    return sha256(database.read_bytes()).hexdigest() == expected


def start_restore(root=PROJECT_ROOT):
    root = Path(root).resolve()
    with _GUARD:
        if root in _STATE or not (root / 'data/samples/saved-view.json.gz').exists():
            return
        database = root / 'data/runtime/scholarships.sqlite3'
        if database.exists():
            with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as db:
                if db.execute('SELECT count(*) FROM scholarships').fetchone()[0]:
                    return
        _STATE[root] = {'status': 'preparing'}
        def worker():
            try:
                from scholarship_intelligence.bootstrap import ensure_saved_results
                installed = ensure_saved_results(project_root=root, database_path=database)
                if not installed:
                    raise ValueError('Saved results were not installed; another workflow may be running.')
                _STATE[root] = {'status': 'ready'}
            except Exception as exc:
                _STATE[root] = {'status': 'failed', 'error': 'Saved evidence preparation failed: ' + str(exc)}
        threading.Thread(target=worker, name='saved-evidence-restore', daemon=True).start()


def records(root=PROJECT_ROOT):
    """A checksummed render cache, created from fully validated genuine records."""
    from scholarship_intelligence.models import ScholarshipExtraction
    from scholarship_intelligence.semantic_assessment import SemanticAssessment
    from scholarship_intelligence.confidence import redate_source_review, score_source_evidence
    from scholarship_intelligence.reuse import fingerprint
    root = Path(root).resolve()
    manifest = json.loads((root / 'data/samples/saved-results.manifest.json').read_bytes())
    raw = (root / 'data/samples/saved-view.json.gz').read_bytes()
    if sha256(raw).hexdigest() != manifest['preview_sha256']:
        raise ValueError('Saved dashboard preview failed its integrity check.')
    if root not in _VIEWS:
        _VIEWS[root] = json.loads(gzip.decompress(raw))
    payload = deepcopy(_VIEWS[root])
    seeds = {s['seed_id']: s for s in json.loads((root/'config/seeds.json').read_bytes())['seeds']}
    today = datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=5, minutes=30))).date()
    for record in payload['records']:
        packet = record['details']['python_source_assessment']
        key = packet['seed_id']
        if fingerprint(seeds[key]) != payload['processing_fingerprints'][key]:
            record.update(confidence_score=None, verification_status='REVIEW_REQUIRED', lifecycle_status='REVIEW_REQUIRED')
            continue
        candidate = ScholarshipExtraction.model_validate(record['source_candidate'])
        review = redate_source_review(candidate, record['source_review'])
        score = score_source_evidence(candidate, SemanticAssessment.model_validate(packet['assessment']),
                                     evidence_checks_passed=True, source_review=review)
        record.update(source_review=review, source_score=score, confidence_score=score['confidence_score'],
                      verification_status=score['verification_status'], lifecycle_status=review['lifecycle_status'])
        # Preserve separately evidenced saved availability, but age it honestly.
        if record.get('availability_verified') and record.get('closing_date'):
            closing = date.fromisoformat(record['closing_date'])
            opening = date.fromisoformat(record['opening_date']) if record.get('opening_date') else None
            if closing < today:
                record['lifecycle_status'] = 'EXPIRED'
            elif review['source_freshness'] == 'FRESH' and opening and opening <= today < closing:
                record['lifecycle_status'] = 'ACTIVE'
    return payload['records']
