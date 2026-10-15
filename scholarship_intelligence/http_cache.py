"""Conditional HTTP checks with verified immutable response bodies."""
from dataclasses import asdict
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
from types import SimpleNamespace

from scholarship_intelligence.snapshots import save_raw_snapshot


class ResponseCache:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.path = self.root / 'data/runtime/http-cache.json'
        self.entries = json.loads(self.path.read_bytes()) if self.path.exists() else {}
        # Older genuine logs can supply bodies, but never invented validators.
        if not self.entries:
            for log in sorted((self.root / 'data/runtime').glob('pilot-*.jsonl')):
                for line in log.read_text(encoding='utf-8').splitlines():
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue  # A process may be writing the last line.
                    if event.get('event') == 'fetched' and event.get('http_status') == 200:
                        self.entries[event.get('request_key', event['snapshot']['source_url'])] = {
                            'snapshot': event['snapshot'], 'method': event.get('request_method', 'GET'),
                            'form_data': event.get('form_data'), 'headers': event.get('response_validators', {}),
                            'seed_id': event['seed_id'], 'last_success_at': event['snapshot']['retrieved_at']}

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix('.tmp')
        temporary.write_text(json.dumps(self.entries, ensure_ascii=False), encoding='utf-8')
        os.replace(temporary, self.path)

    def body(self, item):
        if not item or 'snapshot' not in item:
            return None
        ref = item['snapshot']
        path = (self.root / ref['raw_file_path']).resolve()
        if not path.is_relative_to(self.root / 'data/runtime/snapshots/raw'):
            return None
        try:
            content = path.read_bytes()
        except OSError:
            return None
        return content if sha256(content).hexdigest() == ref['content_hash'] else None

    def fetch(self, session, method, url, form, key, seed_id):
        old = self.entries.get(key)
        content = self.body(old)
        headers = {}
        if method == 'GET' and content is not None:
            validators = old.get('headers', {})
            if validators.get('ETag'):
                headers['If-None-Match'] = validators['ETag']
            if validators.get('Last-Modified'):
                headers['If-Modified-Since'] = validators['Last-Modified']
        options = dict(data=form, timeout=(10, 30), allow_redirects=False)
        response = session.request(method, url, **options, **({'headers': headers} if headers else {}))
        checked = datetime.now(timezone.utc)
        network_status = response.status_code
        if response.status_code == 304:
            # A 304 never proves the content of a missing/corrupt body.
            if method != 'GET' or content is None or not headers:
                response = session.request(method, url, **options)
                network_status = response.status_code
            else:
                response = SimpleNamespace(status_code=200, url=url, content=content,
                    headers={'Content-Type': old['snapshot'].get('content_type') or '',
                             **old.get('headers', {}), **dict(response.headers)})
        if response.status_code == 304:
            raise ValueError('Server returned 304 without a verified saved response body.')
        if len(response.content) > 10 * 1024 * 1024:
            raise ValueError('Response exceeds 10 MB source limit.')
        snapshot = save_raw_snapshot(response.url, response.content, checked,
                                     response.headers.get('Content-Type'), project_root=self.root)
        state = 'not_modified' if network_status == 304 else (
            'unchanged_hash' if old and old.get('snapshot', {}).get('content_hash') == snapshot.content_hash else 'changed_or_new')
        if response.status_code == 200:
            self.entries[key] = {'snapshot': asdict(snapshot), 'method': method, 'form_data': form,
                'seed_id': seed_id, 'headers': {k: response.headers[k] for k in ('ETag', 'Last-Modified') if k in response.headers},
                'last_success_at': checked.isoformat(), 'last_check_at': checked.isoformat(), 'status': 200,
                'body_downloaded_at': (old.get('body_downloaded_at', old['snapshot']['retrieved_at'])
                                       if network_status == 304 else checked.isoformat())}
        else:
            self.failure(key, url, seed_id, str(response.status_code), save=False)
        self.save()
        return response, snapshot, network_status, state

    def failure(self, key, url, seed_id, error, *, save=True):
        self.entries.setdefault(key, {'seed_id': seed_id, 'url': url}).update(
            last_check_at=datetime.now(timezone.utc).isoformat(), status='failed', error=error)
        if save:
            self.save()

    def known(self, seed_id):
        # Listing links can disappear; known GET sources must still be checked.
        return [item['snapshot']['source_url'] for item in self.entries.values()
                if item.get('seed_id') == seed_id and item.get('method') == 'GET' and item.get('snapshot')]
