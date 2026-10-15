"""Dashboard jobs and one daily crawl while the local dashboard is running."""
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import threading
from uuid import uuid4

from scholarship_intelligence.settings import PROJECT_ROOT

IST = timezone(timedelta(hours=5, minutes=30))
_GUARD = threading.RLock()
_SERVICES = {}


def _alive(pid):
    if not isinstance(pid, int) or pid <= 0:
        return True  # Unknown ownership must not be cleared.
    if os.name == 'nt':
        import ctypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.argtypes = [ctypes.c_uint, ctypes.c_int, ctypes.c_uint]
        kernel.OpenProcess.restype = ctypes.c_void_p
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return ctypes.get_last_error() == 5
        kernel.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint)]
        code = ctypes.c_uint()
        try:
            return not kernel.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value == 259
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _recover(root):
    """Recover only fixed project lock files whose recorded process is dead."""
    for name in ['dashboard-crawl.lock', 'workflow.lock', 'crawl.lock']:
        path = root / 'data/runtime' / name
        if path.exists():
            try:
                receipt = json.loads(path.read_bytes())
                if not _alive(receipt.get('pid')):
                    path.unlink()
            except (OSError, ValueError):
                pass


def next_daily(now):
    local = now.astimezone(IST)
    due = local.replace(hour=9, minute=0, second=0, microsecond=0)
    return (due if due > local else due + timedelta(days=1)).astimezone(timezone.utc)


def _write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.' + uuid4().hex + '.tmp')
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def status(project_root=PROJECT_ROOT):
    path = Path(project_root) / 'data/runtime/crawl-control.json'
    with _GUARD:
        if not path.exists():
            value = {'daily_enabled': True, 'next_daily_at': next_daily(datetime.now(timezone.utc)).isoformat(), 'job': None}
            _write(path, value)
        value = json.loads(path.read_bytes())
        job = value.get('job')
        if job and job['status'] == 'running' and not _alive(job.get('pid')):
            _recover(Path(project_root))
            value['job'] = {**job, 'status': 'failed', 'error': 'The previous dashboard process stopped before the workflow finished. Saved sources and completed records are retained.'}
            _write(path, value)
        return value


def set_daily(enabled, project_root=PROJECT_ROOT):
    with _GUARD:
        value = status(project_root)
        value['daily_enabled'] = bool(enabled)
        _write(Path(project_root) / 'data/runtime/crawl-control.json', value)
    return value


def launch(seed_ids=None, *, action='crawl', trigger='manual', project_root=PROJECT_ROOT):
    """Return immediately; one job owns the controller and pipeline locks."""
    root = Path(project_root).resolve()
    from scholarship_intelligence import preview
    if preview.preparing(root):
        raise RuntimeError('The saved evidence is being prepared. Live crawling will be available shortly.')
    if action != 'crawl':
        raise ValueError('Unknown dashboard operation.')
    configured = json.loads((root / 'config/seeds.json').read_bytes())['seeds']
    if seed_ids and any(key not in {item['seed_id'] for item in configured} for key in seed_ids):
        raise ValueError('Unknown crawl source.')
    lock = root / 'data/runtime/dashboard-crawl.lock'
    with _GUARD:
        _recover(root)
        if (root / 'data/runtime/workflow.lock').exists():
            raise RuntimeError('A project workflow is already running. Wait for it to finish.')
        job = {'id': uuid4().hex, 'action': action, 'trigger': trigger, 'seed_ids': seed_ids,
               'status': 'running', 'started_at': datetime.now(timezone.utc).isoformat(), 'pid': os.getpid()}
        try:
            with lock.open('x', encoding='utf-8') as stream:
                json.dump(job, stream)
        except FileExistsError as error:
            raise RuntimeError('A dashboard job is already running. Overlapping jobs are blocked.') from error
        value = status(root)
        value['job'] = job
        if trigger == 'daily':
            # Advance before execution so a failed run does not retry every poll.
            value['next_daily_at'] = next_daily(datetime.now(timezone.utc)).isoformat()
        _write(root / 'data/runtime/crawl-control.json', value)

    from scholarship_intelligence.progress import report_progress
    report_progress(root, None, 'Starting live update')
    def worker():
        outcome = {'status': 'failed', 'error': 'The operation was interrupted.'}
        try:
            from scholarship_intelligence.storage import initialize_database
            database = root / 'data/runtime/scholarships.sqlite3'
            if not database.exists():
                initialize_database(database)
            from scholarship_intelligence.pipeline import run_live_workflows
            result = run_live_workflows(seed_ids, database_path=database, project_root=root)
            report = root / 'data/runtime/live-source-workflows.json'
            _write(report, result)
            outcome = {'status': 'completed_with_review' if result.get('stage') == 'completed_with_review' or any(source.get('attention') or source.get('stage') == 'failed' or source.get('counts', {}).get('failed') or source.get('coverage', {}).get('failed_pages') for source in result.get('sources', [])) else 'completed',
                       'report_file': report.relative_to(root).as_posix()}
        except Exception as error:
            outcome = {'status': 'failed', 'error_type': type(error).__name__, 'error': str(error)}
        finally:
            with _GUARD:
                current = status(root)
                current['job'] = {**job, **outcome, 'finished_at': datetime.now(timezone.utc).isoformat()}
                _write(root / 'data/runtime/crawl-control.json', current)
                lock.unlink(missing_ok=True)
                report_progress(root, None, 'Crawl finished: ' + outcome['status'])
                print('Crawl finished. The dashboard server remains running.', flush=True)
    threading.Thread(target=worker, name='scholarship-workflow', daemon=True).start()
    return job


def tick(*, project_root=PROJECT_ROOT, now=None):
    now = now or datetime.now(timezone.utc)
    value = status(project_root)
    if value['daily_enabled'] and datetime.fromisoformat(value['next_daily_at']) <= now:
        if value.get('job') and value['job']['status'] == 'running':
            return False
        try:
            launch(trigger='daily', project_root=project_root)
            return True
        except RuntimeError:
            return False
    return False


def start(project_root=PROJECT_ROOT):
    root = Path(project_root).resolve()
    with _GUARD:
        if root in _SERVICES and _SERVICES[root].is_alive():
            return
        def loop():
            while True:
                try:
                    tick(project_root=root)
                except (OSError, ValueError, KeyError):
                    pass
                threading.Event().wait(30)
        thread = threading.Thread(target=loop, name='scholarship-daily-scheduler', daemon=True)
        _SERVICES[root] = thread
        thread.start()
