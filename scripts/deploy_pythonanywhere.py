"""Deploy reviewed main commits through a dedicated PythonAnywhere Bash console."""

import argparse
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import time
import uuid

import requests

ROOT = Path('/home/GauravChhajer/StudentConnect')
API = 'https://www.pythonanywhere.com/api/v0/user/GauravChhajer/'
DOMAIN = 'gauravchhajer.pythonanywhere.com'
STATUS = ROOT / 'data/deployment-status.json'
logger = logging.getLogger(__name__)
MAX_BACKUP_SECONDS = 60
MAX_MIGRATION_SECONDS = 90
SAFE_RUNTIME_FAILURES = {
    'Unexpected deployment directory': 'unexpected_deployment_directory',
    'Deployment requires a clean main checkout': 'dirty_or_non_main_checkout',
    'Requested commit is no longer the current remote main': 'commit_not_remote_main',
    'Private runtime paths cannot be deployed from Git': 'private_path_in_commit',
    'Dependency changes require manual deployment': 'dependency_changes_require_manual_install',
    'Deployment deadline expired before update': 'deployment_deadline_expired',
    'Deployment revision verification failed': 'revision_verification_failed',
    'Hosted database is missing; migrations stopped': 'hosted_database_missing',
    'No migration files are available': 'migration_files_missing',
    'Deployment deadline expired before database backup': 'backup_deadline_expired',
    'Database backup time budget expired': 'backup_timeout',
    'Database backup integrity check failed': 'backup_integrity_check_failed',
    'Deployment deadline expired before migrations': 'migration_deadline_expired',
    'Database migration command failed; reload withheld': 'migration_command_failed',
    'Database migration verification failed; reload withheld': 'migration_verification_failed',
    'Another pipeline cycle is active or locking failed': 'pipeline_lock_busy',
}


def revision(value):
    if not re.fullmatch(r'[0-9a-f]{40}', value):
        raise ValueError('Expected a full commit SHA')
    return value


def failure_code(error):
    if isinstance(error, RuntimeError):
        if str(error).startswith('An applied migration was modified: '):
            return 'applied_migration_checksum_mismatch'
        return SAFE_RUNTIME_FAILURES.get(str(error), 'runtime_error')
    if isinstance(error, subprocess.TimeoutExpired):
        return 'subprocess_timeout'
    if isinstance(error, sqlite3.Error):
        return 'sqlite_error'
    if isinstance(error, subprocess.SubprocessError):
        return 'subprocess_error'
    if isinstance(error, ValueError):
        return 'invalid_value'
    if isinstance(error, OSError):
        return 'filesystem_error'
    return 'deployment_error'


def pending_migrations(database=None, migration_dir=None):
    database = Path(database or ROOT / 'data/jobs.db')
    migration_dir = Path(migration_dir or ROOT / 'backend/migrations')
    if not database.is_file():
        raise RuntimeError('Hosted database is missing; migrations stopped')
    files = sorted(migration_dir.glob('*.sql'))
    if not files:
        raise RuntimeError('No migration files are available')
    with closing(sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True, timeout=5)) as conn:
        has_migration_table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
        ).fetchone()
        applied = dict(conn.execute('SELECT name,checksum FROM schema_migrations')) if has_migration_table else {}
    pending = []
    for path in files:
        checksum = hashlib.sha256(path.read_text(encoding='utf-8').encode('utf-8')).hexdigest()
        if path.name in applied and applied[path.name] != checksum:
            raise RuntimeError('An applied migration was modified: ' + path.name)
        if path.name not in applied:
            pending.append(path)
    return pending


def backup_database(database, label, deadline, backup_dir=None):
    database = Path(database)
    backup_dir = Path(backup_dir or ROOT / 'data/backups')
    if not re.fullmatch(r'[A-Za-z0-9-]{1,40}', label):
        raise ValueError('Invalid database backup label')
    if time.time() >= deadline:
        raise RuntimeError('Deployment deadline expired before database backup')
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    backup = backup_dir / ('jobs-before-deploy-' + label + '-' + stamp + '-' + uuid.uuid4().hex[:8] + '.db')
    with backup.open('xb'):
        pass
    try:
        with closing(sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True, timeout=5)) as source, \
                closing(sqlite3.connect(backup, timeout=5)) as target:
            backup_deadline = min(deadline, time.time() + MAX_BACKUP_SECONDS)

            def progress(status, remaining, total):
                if time.time() >= backup_deadline:
                    raise RuntimeError('Database backup time budget expired')

            source.backup(target, pages=100, progress=progress, sleep=0.1)
            if target.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                raise RuntimeError('Database backup integrity check failed')
    except BaseException:
        backup.unlink(missing_ok=True)
        raise
    logger.info('Created verified pre-migration database backup: %s', backup.name)
    return backup


def migrate_pending(database=None, migration_dir=None, backup_dir=None, deadline=None, deployment_sha=None):
    database = Path(database or ROOT / 'data/jobs.db')
    migration_dir = Path(migration_dir or ROOT / 'backend/migrations')
    deadline = deadline if deadline is not None else time.time() + MAX_MIGRATION_SECONDS
    pending = pending_migrations(database, migration_dir)
    if not pending:
        logger.info('No pending database migrations')
        return []
    backup_label = deployment_sha[:12] if deployment_sha else 'manual'
    backup_database(database, backup_label, deadline, backup_dir)
    remaining = deadline - time.time()
    if remaining <= 0:
        raise RuntimeError('Deployment deadline expired before migrations')
    timeout = min(MAX_MIGRATION_SECONDS, remaining)
    try:
        subprocess.run([sys.executable, '-B', '-m', 'backend.migrate'], cwd=ROOT, check=True,
                       capture_output=True, text=True, timeout=timeout)
    except subprocess.SubprocessError as error:
        raise RuntimeError('Database migration command failed; reload withheld') from error
    unapplied = pending_migrations(database, migration_dir)
    if unapplied:
        raise RuntimeError('Database migration verification failed; reload withheld')
    names = [path.name for path in pending]
    logger.info('Applied and verified database migrations: %s', ','.join(names))
    return names


def git(*args):
    # Git output can contain private remote URLs; never forward it to CI logs.
    return subprocess.run(['git', *args], cwd=ROOT, check=True, capture_output=True,
                          text=True, timeout=60, env=dict(os.environ, GIT_TERMINAL_PROMPT='0')).stdout.strip()


def update_checkout(sha, deadline):
    if Path.cwd().resolve() != ROOT or git('rev-parse', '--show-toplevel') != str(ROOT):
        raise RuntimeError('Unexpected deployment directory')
    if git('branch', '--show-current') != 'main' or git('status', '--porcelain'):
        raise RuntimeError('Deployment requires a clean main checkout')
    git('fetch', 'origin', 'main')
    if git('rev-parse', 'FETCH_HEAD') != sha:
        raise RuntimeError('Requested commit is no longer the current remote main')
    git('merge-base', '--is-ancestor', 'HEAD', sha)
    changed = git('diff', '--name-only', 'HEAD', sha).splitlines()
    if any(path == '.env' or path.startswith('.env.') and path != '.env.example' or
           path.startswith(('.venv/', 'data/uploads/')) or
           path.startswith('data/') and not path.startswith('data/boards/') for path in changed):
        raise RuntimeError('Private runtime paths cannot be deployed from Git')
    if any(path == 'requirements.txt' for path in changed):
        raise RuntimeError('Dependency changes require manual deployment')
    if time.time() >= deadline:
        raise RuntimeError('Deployment deadline expired before update')
    # Fast-forward only: never reset, discard local changes or overwrite private data.
    git('merge', '--ff-only', '--no-overwrite-ignore', sha)
    if git('rev-parse', 'HEAD') != sha:
        raise RuntimeError('Deployment revision verification failed')


def hosted(sha, run_id, deadline):
    from scripts.run_pipeline import exclusive_cycle

    revision(sha)
    if not re.fullmatch(r'[0-9a-f]{32}', run_id):
        raise ValueError('Invalid deployment run identifier')
    result = dict(run_id=run_id, sha=sha, success=False, stage='lock', error_code=None)
    try:
        # Share ownership with workers, so deployment never overlaps a pipeline cycle.
        with exclusive_cycle(ROOT / 'data/pipeline.lock'):
            result['stage'] = 'checkout'
            update_checkout(sha, deadline)
            result['stage'] = 'migrations'
            migrate_pending(deadline=deadline, deployment_sha=sha)
            result['success'] = True
            result['stage'] = None
    except (OSError, ValueError, RuntimeError, sqlite3.Error, subprocess.SubprocessError) as error:
        result['error_code'] = failure_code(error)
        logger.error('Hosted deployment stopped stage=%s error_code=%s; reload withheld',
                     result['stage'], result['error_code'])
    finally:
        # One bounded status file; replace atomically so CI cannot read half a result.
        temporary = STATUS.with_suffix('.tmp')
        with temporary.open('w', encoding='utf-8') as handle:
            json.dump(result, handle)
        temporary.replace(STATUS)
    return 0 if result['success'] else 1


def deploy(sha):
    revision(sha)
    token = os.environ.get('PYTHONANYWHERE_API_TOKEN', '').strip()
    console = os.environ.get('PYTHONANYWHERE_CONSOLE_ID', '')
    if not token or not re.fullmatch(r'[1-9][0-9]{0,15}', console):
        raise ValueError('Configure the PythonAnywhere token and dedicated console ID')
    run_id = uuid.uuid4().hex
    deadline = int(time.time()) + 180
    command = (f'cd {ROOT} && .venv/bin/python -B -m scripts.deploy_pythonanywhere '
               f'--hosted {sha} {run_id} {deadline}\n')
    with requests.Session() as session:
        session.headers.update(Authorization='Token ' + token)

        def call(method, path, **kwargs):
            response = session.request(method, API + path, timeout=(10, 20),
                                       allow_redirects=False, stream=True, **kwargs)
            return response

        # Mutations are sent once. A timeout is uncertain and must stop deployment.
        with call('POST', f'consoles/{console}/send_input/', data={'input': command}) as response:
            if response.status_code != 200:
                raise RuntimeError('Console input was not accepted')
        for _ in range(36):
            if time.time() >= deadline:
                break
            with call('GET', 'files/path' + str(STATUS)) as response:
                if response.status_code == 200:
                    chunks, size = [], 0
                    for chunk in response.iter_content(1024):
                        size += len(chunk)
                        if size > 1024:
                            raise ValueError('Deployment status exceeds its limit')
                        chunks.append(chunk)
                    result = json.loads(b''.join(chunks))
                    if not isinstance(result, dict):
                        raise ValueError('Invalid deployment status')
                    if result.get('run_id') == run_id:
                        if result.get('sha') != sha:
                            raise RuntimeError('Hosted update failed; reload withheld')
                        if result.get('success') is not True:
                            stage, error_code = result.get('stage'), result.get('error_code')
                            if (stage not in ('lock', 'checkout', 'migrations') or not isinstance(error_code, str) or
                                    not re.fullmatch(r'[a-z][a-z0-9_]{1,60}', error_code)):
                                raise RuntimeError('Hosted update failed; reload withheld')
                            logger.error('Hosted update failed stage=%s error_code=%s; reload withheld', stage, error_code)
                            return 1
                        with call('POST', f'webapps/{DOMAIN}/reload/') as reload_response:
                            if reload_response.status_code != 200:
                                raise RuntimeError('Web reload was not acknowledged')
                        logger.info('Updated requested commit and requested web reload')
                        return 0
                elif response.status_code != 404:
                    raise RuntimeError('Could not read deployment status')
            time.sleep(5)
    raise RuntimeError('No deployment confirmation before deadline; reload withheld')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--hosted', nargs=3, metavar=('SHA', 'RUN_ID', 'DEADLINE'))
    parser.add_argument('--sha')
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
    try:
        if args.hosted:
            sha, run_id, deadline = args.hosted
            return hosted(sha, run_id, int(deadline))
        return deploy(args.sha or '')
    except (OSError, ValueError, RuntimeError, requests.RequestException) as error:
        logger.error('Deployment stopped (%s); no automatic retry', type(error).__name__)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
