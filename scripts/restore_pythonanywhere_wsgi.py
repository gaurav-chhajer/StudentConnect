"""Restore the last tested WSGI entry point after a failed hosted release."""

import logging
import os
from pathlib import Path
import subprocess

import requests

from scripts.deploy_pythonanywhere import API, DOMAIN, ROOT, revision

logger = logging.getLogger(__name__)
KNOWN_GOOD = '0d59a8442c8490d84547471a017f37b48fa5ccd6'


def restore():
    token = os.environ.get('PYTHONANYWHERE_API_TOKEN', '').strip()
    if not token:
        raise ValueError('PythonAnywhere API token is unavailable')
    revision(KNOWN_GOOD)
    old = subprocess.run(['git', 'show', KNOWN_GOOD + ':wsgi.py'], cwd=Path(__file__).resolve().parents[1],
                         capture_output=True, check=True, timeout=15).stdout
    if not old or len(old) > 131072:
        raise ValueError('Known good WSGI file is missing or oversized')
    with requests.Session() as session:
        session.headers.update(Authorization='Token ' + token)
        with session.post(API + 'files/path' + str(ROOT / 'wsgi.py'),
                          files={'content': ('wsgi.py', old)}, timeout=(10, 30),
                          allow_redirects=False) as upload:
            if upload.status_code not in (200, 201):
                raise RuntimeError('WSGI restore upload was rejected: HTTP ' + str(upload.status_code))
        with session.post(API + f'webapps/{DOMAIN}/reload/', timeout=(10, 30),
                          allow_redirects=False) as reload:
            if reload.status_code != 200:
                raise RuntimeError('Web reload was rejected: HTTP ' + str(reload.status_code))
    logger.info('Restored last successful WSGI entry point and requested reload')


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
    try:
        restore()
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError, requests.RequestException) as error:
        logger.error('Emergency WSGI restore stopped (%s)', type(error).__name__)
        raise SystemExit(1) from error
