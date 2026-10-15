"""Install the genuine saved-results bundle once, without changing its dates."""
from contextlib import closing
from datetime import datetime, timezone
from hashlib import sha256
import json
from io import BytesIO
import os
from pathlib import Path
import shutil
import sqlite3
from uuid import uuid4
import zipfile

from scholarship_intelligence.settings import PROJECT_ROOT, DATABASE_PATH


def ensure_saved_results(*, project_root=PROJECT_ROOT, database_path=DATABASE_PATH):
    root, database = Path(project_root).resolve(), Path(database_path).resolve()
    if database != root / 'data/runtime/scholarships.sqlite3':
        return False  # Test/custom databases are never seeded implicitly.
    if database.exists():
        with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as db:
            if db.execute('SELECT count(*) FROM scholarships').fetchone()[0]:
                return False
    archive = root / 'data/samples/saved-results.zip'
    manifest_path = root / 'data/samples/saved-results.manifest.json'
    if not manifest_path.exists():
        return False
    manifest = json.loads(manifest_path.read_bytes())
    if manifest.get('archive_parts'):
        joined = BytesIO()
        for part in manifest['archive_parts']:
            name = part['name']
            if Path(name).name != name or not name.startswith('saved-results.zip.part'):
                raise ValueError('Invalid saved-results part name.')
            content = (manifest_path.parent / name).read_bytes()
            if sha256(content).hexdigest() != part['sha256']:
                raise ValueError('Saved-results archive part failed its integrity check.')
            joined.write(content)
        joined.seek(0)
        archive = joined
        digest = sha256(joined.getbuffer()).hexdigest()
    else:
        if not archive.exists():
            return False
        digest = sha256(archive.read_bytes()).hexdigest()
    if digest != manifest['archive_sha256']:
        raise ValueError('Saved-results archive failed its integrity check.')
    runtime = root / 'data/runtime'
    runtime.mkdir(parents=True, exist_ok=True)
    lock = runtime / 'bootstrap.lock'
    from scholarship_intelligence.workflow_service import _alive, _recover
    _recover(root)
    if lock.exists():
        try:
            owner = int(lock.read_text(encoding='utf-8'))
        except ValueError:
            raise RuntimeError('Saved evidence preparation has an invalid lock; inspect it before retrying.')
        if not _alive(owner):
            lock.unlink()
        else:
            raise RuntimeError('Another process is preparing the saved evidence.')
    if any((runtime / name).exists() for name in ('workflow.lock', 'crawl.lock', 'dashboard-crawl.lock')):
        return False  # Never replace even an empty database during a live run.
    temporary = root / 'data' / ('bootstrap-' + uuid4().hex)
    with lock.open('x', encoding='utf-8') as stream:
        stream.write(str(os.getpid()))
    try:
        temporary.mkdir()
        with zipfile.ZipFile(archive) as source:
            if sum(entry.file_size for entry in source.infolist()) > 600 * 1024 * 1024:
                raise ValueError('Saved-results archive exceeds the supported size.')
            if set(source.namelist()) != set(manifest['files']):
                raise ValueError('Saved-results file inventory differs from its manifest.')
            for name in source.namelist():
                target = (temporary / name).resolve()
                if not name.startswith('data/runtime/') or not target.is_relative_to(temporary / 'data/runtime'):
                    raise ValueError('Saved-results path is outside runtime storage.')
                raw = source.read(name)
                if sha256(raw).hexdigest() != manifest['files'][name]:
                    raise ValueError('Saved-results file failed its integrity check.')
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(raw)
        prepared_db = temporary / 'data/runtime/scholarships.sqlite3'
        with closing(sqlite3.connect(prepared_db)) as db:
            if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok' or db.execute('PRAGMA foreign_key_check').fetchall():
                raise ValueError('Saved-results database failed integrity validation.')
        # Copy source artifacts first, publishing the database last.
        for name, digest in manifest['files'].items():
            if name == 'data/runtime/scholarships.sqlite3':
                continue
            destination = root / name
            if destination.exists():
                if sha256(destination.read_bytes()).hexdigest() != digest:
                    raise ValueError('Existing runtime artifact differs; saved results were not installed.')
            else:
                destination.parent.mkdir(parents=True, exist_ok=True)
                os.replace(temporary / name, destination)
        os.replace(prepared_db, database)
        (runtime / 'saved-results-origin.json').write_text(json.dumps({
            'kind': 'genuine_saved_crawl', 'source_checked_at': manifest['source_checked_at'],
            'loaded_at': datetime.now(timezone.utc).isoformat(),
            'note': 'Loading this snapshot does not perform a live source check or reset verification dates.'
        }, indent=2), encoding='utf-8')
        return True
    finally:
        if temporary.exists() and temporary.is_relative_to(root / 'data'):
            shutil.rmtree(temporary)
        lock.unlink(missing_ok=True)
