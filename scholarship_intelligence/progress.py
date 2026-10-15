"""Small progress receipts for the dashboard's background workflow."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path


def report_progress(root, seed, phase, **details):
    path = Path(root) / 'data/runtime/live-progress.json'
    old = json.loads(path.read_bytes()) if path.exists() else {}
    count = (0 if phase == 'Starting live update' else old.get('pages_checked', 0)) + int(phase == 'Checking live pages')
    value = {'seed_id': seed, 'phase': phase, 'pages_checked': count,
             'updated_at': datetime.now(timezone.utc).isoformat(), **details}
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value), encoding='utf-8')
    os.replace(temporary, path)
    if old.get('phase') != phase or old.get('seed_id') != seed or count % 10 == 0:
        print(f'[{seed or "workflow"}] {phase}: {count} live page checks', flush=True)
