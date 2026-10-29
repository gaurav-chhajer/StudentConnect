"""Load a verified PythonAnywhere web release without a live console."""

from contextlib import contextmanager
import hashlib
import importlib.util
import json
import logging
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import time
import uuid
import zipfile

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / 'data'
RELEASES = ROOT / '.releases'
REQUEST = DATA / 'deploy-request.json'
STATUS = DATA / 'deployment-status.json'
ACTIVE = DATA / 'active-release.json'
ARCHIVE = DATA / 'deploy-release.zip'
MAX_ARCHIVE_BYTES = 12 * 1024 * 1024
MAX_EXTRACTED_BYTES = 24 * 1024 * 1024
MAX_FILES = 300
logger = logging.getLogger(__name__)


def _json_file(path):
    if not path.is_file() or path.stat().st_size > 2048:
        raise ValueError('Deployment metadata is missing or too large')
    value = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(value, dict):
        raise ValueError('Deployment metadata must be an object')
    return value


def _metadata(value, request=False):
    expected = {'sha', 'archive_sha256', 'run_id'} if request else {'sha'}
    if set(value) != expected or not isinstance(value['sha'], str) or not re.fullmatch(r'[0-9a-f]{40}', value['sha']):
        raise ValueError('Deployment metadata has an invalid revision')
    if request and (not isinstance(value['archive_sha256'], str) or
                    not re.fullmatch(r'[0-9a-f]{64}', value['archive_sha256']) or
                    not isinstance(value['run_id'], str) or
                    not re.fullmatch(r'[0-9a-f]{32}', value['run_id'])):
        raise ValueError('Deployment request has invalid integrity fields')
    return value


def _atomic_json(path, value):
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    with temporary.open('x', encoding='utf-8') as handle:
        json.dump(value, handle, separators=(',', ':'))
    temporary.replace(path)


@contextmanager
def _pipeline_lock():
    """Use the same advisory lock as the scheduled worker."""
    with (DATA / 'pipeline.lock').open('a+b') as handle:
        if handle.tell() == 0:
            handle.write(b'0')
            handle.flush()
        handle.seek(0)
        if os.name == 'nt':
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            if os.name == 'nt':
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _extract(request):
    target = RELEASES / request['sha']
    if target.is_dir():
        return target
    if not ARCHIVE.is_file() or ARCHIVE.stat().st_size > MAX_ARCHIVE_BYTES:
        raise ValueError('Release archive is missing or too large')
    with ARCHIVE.open('rb') as source:
        checksum = hashlib.file_digest(source, 'sha256').hexdigest()
    if checksum != request['archive_sha256']:
        raise ValueError('Release archive checksum does not match the request')
    RELEASES.mkdir(exist_ok=True)
    temporary = RELEASES / (request['sha'] + '.staging')
    if temporary.exists():
        raise ValueError('A previous staging directory requires inspection')
    temporary.mkdir()
    try:
        with zipfile.ZipFile(ARCHIVE) as archive:
            files = archive.infolist()
            if len(files) > MAX_FILES or sum(item.file_size for item in files) > MAX_EXTRACTED_BYTES:
                raise ValueError('Release contains too many or oversized files')
            for item in files:
                name = PurePosixPath(item.filename)
                if (item.is_dir() or name.is_absolute() or '..' in name.parts or '\\' in item.filename or
                        not name.parts or name.parts[0] not in {'backend', 'frontend', 'scripts'} or
                        (item.external_attr >> 16) & 0o170000 not in (0, 0o100000)):
                    raise ValueError('Release archive contains an unsafe path')
                destination = temporary.joinpath(*name.parts)
                destination.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(item) as source, destination.open('xb') as output:
                    while chunk := source.read(65536):
                        output.write(chunk)
        for required in ('backend/app.py', 'backend/migrate.py', 'frontend/templates', 'frontend/static'):
            if not (temporary / required).exists():
                raise ValueError('Release is missing an application component')
        (temporary / 'data').symlink_to(DATA, target_is_directory=True)
        (temporary / '.env').symlink_to(ROOT / '.env')
        temporary.rename(target)
    except BaseException:
        # A partial extraction is left for inspection; it is never activated.
        raise
    return target


def _import_application(release):
    sys.path.insert(0, str(release))
    import scripts
    scripts.__path__.insert(0, str(release / 'scripts'))
    from backend.hosting import application_settings, load_hosted_environment
    load_hosted_environment(ROOT / '.env')
    if not application_settings()[1] or not os.environ.get('GROQ_API_KEY', '').strip():
        raise ValueError('Hosted application configuration is incomplete')
    from backend.app import app
    from backend.pipeline_skeleton import validate_email_configuration
    validate_email_configuration()
    return app


def _preflight(release):
    result = subprocess.run([sys.executable, '-B', '-m', 'scripts.hosted_release', '--preflight', str(release)],
                            cwd=ROOT, capture_output=True, text=True, timeout=60)
    if result.returncode != 0:
        raise RuntimeError('Release application failed its isolated startup check')


def _migration_module(release):
    path = release / 'scripts/deploy_pythonanywhere.py'
    if not path.is_file() or path.is_symlink():
        raise ValueError('Release migration helper is missing')
    spec = importlib.util.spec_from_file_location('_verified_release_migrations', path)
    if spec is None or spec.loader is None:
        raise ValueError('Release migration helper cannot be loaded')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def application():
    active = _metadata(_json_file(ACTIVE)) if ACTIVE.exists() else None
    request = _metadata(_json_file(REQUEST), request=True) if REQUEST.exists() else None
    if request and active and request['sha'] == active['sha']:
        app = _import_application(RELEASES / active['sha'])
        _atomic_json(STATUS, {'run_id': request['run_id'], 'sha': request['sha'],
                              'success': True, 'stage': None})
        return app
    if request and (active is None or request['sha'] != active['sha']):
        stage = 'extract'
        importing = False
        try:
            with _pipeline_lock():
                release = _extract(request)
                stage = 'migrations'
                _migration_module(release).migrate_pending(
                    database=DATA / 'jobs.db', migration_dir=release / 'backend/migrations',
                    backup_dir=DATA / 'backups', deadline=time.time() + 90,
                    deployment_sha=request['sha'], runtime_root=release)
                stage = 'startup'
                _preflight(release)
                importing = True
                app = _import_application(release)
                _atomic_json(ACTIVE, {'sha': request['sha']})
                _atomic_json(STATUS, {'run_id': request['run_id'], 'sha': request['sha'],
                                      'success': True, 'stage': None})
                return app
        except (OSError, ValueError, RuntimeError, TypeError, ImportError,
                subprocess.SubprocessError, zipfile.BadZipFile) as error:
            logger.error('Release activation failed stage=%s error=%s', stage, type(error).__name__)
            _atomic_json(STATUS, {'run_id': request['run_id'], 'sha': request['sha'],
                                  'success': False, 'stage': stage, 'error_code': type(error).__name__})
            if importing:
                raise
    return _import_application(RELEASES / active['sha'] if active else ROOT)


if __name__ == '__main__':
    if len(sys.argv) != 3 or sys.argv[1] != '--preflight':
        raise SystemExit(2)
    release = Path(sys.argv[2])
    if not release.is_dir() or release.parent != RELEASES:
        raise SystemExit(2)
    _import_application(release)
