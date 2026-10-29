"""Upload a tested web release through the PythonAnywhere Files API and reload it."""

import argparse
import hashlib
from io import BytesIO
import json
import logging
import os
from pathlib import Path
import re
import subprocess
import time
import uuid
import zipfile

import requests

from scripts import deploy_pythonanywhere as legacy

ROOT = Path(__file__).resolve().parents[1]
logger = logging.getLogger(__name__)
MAX_STATUS_BYTES = 2048


def release_archive(sha):
    legacy.revision(sha)
    head = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=ROOT, capture_output=True,
                          text=True, check=True, timeout=15).stdout.strip()
    if head != sha:
        raise ValueError('Requested release does not match the checked-out commit')
    changes = subprocess.run(['git', 'status', '--porcelain', '--untracked-files=no'], cwd=ROOT,
                             capture_output=True, text=True, check=True, timeout=15).stdout
    if changes:
        raise ValueError('Release requires a clean tracked checkout')
    files = subprocess.run(['git', 'ls-files', '-z', 'backend', 'frontend', 'scripts'],
                           cwd=ROOT, capture_output=True, check=True, timeout=15).stdout.split(b'\0')
    output = BytesIO()
    with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        for name in files:
            if not name:
                continue
            relative = Path(os.fsdecode(name))
            path = ROOT / relative
            if not path.is_file() or path.is_symlink():
                raise ValueError('Release contains a missing or linked tracked file')
            archive.write(path, relative.as_posix())
    return output.getvalue()


def _bounded(response, limit=MAX_STATUS_BYTES):
    result = bytearray()
    for chunk in response.iter_content(1024):
        result.extend(chunk)
        if len(result) > limit:
            raise legacy.RemoteDeploymentError('confirmation', 'oversized_response')
    return bytes(result)


def deploy(sha):
    legacy.revision(sha)
    token = os.environ.get('PYTHONANYWHERE_API_TOKEN', '').strip()
    if not token:
        raise ValueError('Configure PYTHONANYWHERE_API_TOKEN in GitHub Actions secrets')
    content = release_archive(sha)
    if len(content) > 12 * 1024 * 1024:
        raise ValueError('Release exceeds the hosted archive limit')
    run_id = uuid.uuid4().hex
    request = json.dumps({'sha': sha, 'archive_sha256': hashlib.sha256(content).hexdigest(),
                          'run_id': run_id}, separators=(',', ':')).encode()
    with requests.Session() as session:
        session.headers.update(Authorization='Token ' + token)

        def call(method, endpoint, stage, **kwargs):
            try:
                response = session.request(method, legacy.API + endpoint, timeout=(10, 30),
                                           allow_redirects=False, stream=True, **kwargs)
            except requests.RequestException as error:
                raise legacy.RemoteDeploymentError(stage, 'network_error') from error
            return response

        def upload(path, payload, stage):
            with call('POST', 'files/path' + str(path), stage,
                      files={'content': (path.name, payload)}) as response:
                if response.status_code not in (200, 201):
                    raise legacy.RemoteDeploymentError(stage, 'http_rejected', response.status_code)

        def backup(path):
            with call('GET', 'files/path' + str(path), 'entry_backup') as response:
                if response.status_code == 404:
                    return
                if response.status_code != 200:
                    raise legacy.RemoteDeploymentError('entry_backup', 'http_rejected', response.status_code)
                original = _bounded(response, 131072)
            upload(legacy.ROOT / 'data/deployment-backups' / run_id / path.name,
                   original, 'entry_backup')

        with call('GET', 'files/path' + str(legacy.ROOT / 'requirements.txt'), 'dependencies') as response:
            if response.status_code != 200:
                raise legacy.RemoteDeploymentError('dependencies', 'http_rejected', response.status_code)
            if _bounded(response, 65536) != (ROOT / 'requirements.txt').read_bytes():
                raise legacy.RemoteDeploymentError('dependencies', 'manual_dependency_install_required')

        # Preserve the two root entry files before replacing either one.
        backup(legacy.ROOT / 'scripts/hosted_release.py')
        backup(legacy.ROOT / 'wsgi.py')
        # The loader is uploaded before the entry point. No running process is changed until reload.
        upload(legacy.ROOT / 'scripts/hosted_release.py',
               (ROOT / 'scripts/hosted_release.py').read_bytes(), 'loader_upload')
        upload(legacy.ROOT / 'data/deploy-release.zip', content, 'release_upload')
        upload(legacy.ROOT / 'data/deploy-request.json', request, 'request_upload')
        upload(legacy.ROOT / 'wsgi.py', (ROOT / 'wsgi.py').read_bytes(), 'entry_upload')
        with call('POST', f'webapps/{legacy.DOMAIN}/reload/', 'web_reload') as response:
            if response.status_code != 200:
                raise legacy.RemoteDeploymentError('web_reload', 'http_rejected', response.status_code)
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            with call('GET', 'files/path' + str(legacy.STATUS), 'confirmation') as response:
                if response.status_code == 200:
                    try:
                        status = json.loads(_bounded(response))
                    except (UnicodeError, ValueError) as error:
                        raise legacy.RemoteDeploymentError('confirmation', 'invalid_status') from error
                    if isinstance(status, dict) and status.get('run_id') == run_id:
                        if status.get('sha') != sha or type(status.get('success')) is not bool:
                            raise legacy.RemoteDeploymentError('confirmation', 'invalid_status')
                        if not status['success']:
                            stage = status.get('stage')
                            code = status.get('error_code')
                            if stage not in ('extract', 'migrations', 'startup') or not isinstance(code, str) or not re.fullmatch(r'[A-Za-z]{2,40}', code):
                                raise legacy.RemoteDeploymentError('confirmation', 'invalid_status')
                            logger.error('Hosted release failed stage=%s error_code=%s; previous release retained', stage, code)
                            return 1
                        logger.info('Verified release %s activated; web reload completed', sha[:12])
                        return 0
                elif response.status_code != 404:
                    raise legacy.RemoteDeploymentError('confirmation', 'http_rejected', response.status_code)
            time.sleep(5)
    raise legacy.RemoteDeploymentError('confirmation', 'deadline_expired')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sha', required=True)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
    try:
        return deploy(args.sha)
    except legacy.RemoteDeploymentError as error:
        logger.error('Deployment stopped stage=%s reason=%s http_status=%s; no automatic retry',
                     error.stage, error.reason, error.http_status if error.http_status is not None else 'unavailable')
        return 1
    except (OSError, ValueError, subprocess.SubprocessError, requests.RequestException) as error:
        logger.error('Deployment stopped (%s); no automatic retry', type(error).__name__)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
