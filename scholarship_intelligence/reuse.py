"""Reuse independently validated facts only while all their inputs match."""
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path

from scholarship_intelligence.bundles import load_bundle
from scholarship_intelligence.models import ScholarshipExtraction


def fingerprint(seed):
    modules = ('confidence.py', 'python_extraction.py', 'source_profiles.py', 'models.py',
               'preparation.py', 'evidence_validation.py', 'semantic_assessment.py', 'bundles.py')
    content = b''.join((Path(__file__).parent / name).read_bytes() for name in modules)
    return sha256(content + json.dumps(seed, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def packet_digest(packet):
    return sha256(json.dumps({k: v for k, v in packet.items() if k != 'reuse_receipt'},
                             sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()


def stamp(packet, seed, root):
    """Called only after full independent verification or validated reuse."""
    packet['processing_fingerprint'] = fingerprint(seed)
    digest = packet_digest(packet)
    path = Path(root) / 'data/runtime/extractions/validated-reuse' / (digest + '.json')
    path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps({'packet_sha256': digest, 'processing_fingerprint': fingerprint(seed)}, sort_keys=True)
    if path.exists() and path.read_text(encoding='utf-8') != content:
        raise ValueError('Validation reuse receipt changed.')
    if not path.exists():
        path.write_text(content, encoding='utf-8')
    packet['reuse_receipt'] = path.relative_to(root).as_posix()


def checked(packet, seed, root):
    """No field-context rerun; exact bundle bytes and policy still required."""
    root = Path(root).resolve()
    if packet.get('processing_fingerprint') != fingerprint(seed):
        raise ValueError('Extraction, validation policy or source configuration changed.')
    digest = packet_digest(packet)
    path = root / 'data/runtime/extractions/validated-reuse' / (digest + '.json')
    if packet.get('reuse_receipt') != path.relative_to(root).as_posix():
        raise ValueError('Validated reuse receipt does not match this packet.')
    receipt = json.loads(path.read_bytes())
    if receipt != {'packet_sha256': digest, 'processing_fingerprint': fingerprint(seed)}:
        raise ValueError('Validated reuse receipt is invalid.')
    def pinned(name, expected):
        p = (root / name).resolve()
        if not p.is_relative_to(root / 'data/runtime/extractions'):
            raise ValueError('Candidate is outside extraction storage.')
        raw = p.read_bytes()
        if sha256(raw).hexdigest() != expected:
            raise ValueError('Pinned extraction artifact changed.')
        return raw
    candidate = ScholarshipExtraction.model_validate_json(pinned(packet['candidate_path'], packet['candidate_sha256']))
    pinned(packet['provenance_path'], packet['provenance_sha256'])
    bundle = load_bundle(root / 'data/runtime/bundles' / (candidate.bundle_id + '.json'), project_root=root)
    if bundle.bundle_id != packet['bundle_id']:
        raise ValueError('Cached extraction belongs to another bundle.')
    return candidate, bundle


def refresh(packet, seed, events, pilot, root):
    from scholarship_intelligence.confidence import redate_source_review, score_source_evidence
    from scholarship_intelligence.semantic_assessment import SemanticAssessment
    candidate, bundle = checked(packet, seed, root)
    value = deepcopy(packet)
    times = {}
    for ref in bundle.documents:
        matches = [e['snapshot']['retrieved_at'] for e in events if e.get('event') == 'fetched'
                   and e.get('http_status') == 200 and e.get('seed_id') == seed['seed_id']
                   and e['snapshot']['source_url'] == ref.source_url and e['snapshot']['content_hash'] == ref.content_hash]
        if not matches:
            raise ValueError('No successful live check supports every cached document.')
        times[ref.snapshot_id] = max(matches)
    value['source_review']['retrieval_times'] = times
    value['source_review'] = redate_source_review(candidate, value['source_review'])
    value['source_last_verified_at'] = (min(times.values())
        if times != packet['source_review']['retrieval_times']
        else packet.get('source_last_verified_at', packet['source_review']['assessed_at']))
    value['score'] = score_source_evidence(candidate, SemanticAssessment.model_validate(value['assessment']),
                                         evidence_checks_passed=True, source_review=value['source_review'])
    value['lifecycle_status'] = value['source_review']['lifecycle_status']
    value['pilot_file'] = Path(pilot).relative_to(root).as_posix()
    value['pilot_sha256'] = sha256(Path(pilot).read_bytes()).hexdigest()
    stamp(value, seed, root)
    return value
