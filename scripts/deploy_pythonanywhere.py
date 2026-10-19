"""Deploy reviewed main commits through a dedicated PythonAnywhere Bash console."""

import argparse
import json
import logging
import os
from pathlib import Path
import re
import subprocess
import time
import uuid

import requests

ROOT = Path('/home/GauravChhajer/StudentConnect')
API = 'https://www.pythonanywhere.com/api/v0/user/GauravChhajer/'
DOMAIN = 'gauravchhajer.pythonanywhere.com'
STATUS = ROOT / 'data/deployment-status.json'
logger = logging.getLogger(__name__)


def revision(value):
    if not re.fullmatch(r'[0-9a-f]{40}', value):
        raise ValueError('Expected a full commit SHA')
    return value


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
    if any(path == 'requirements.txt' or path.startswith('backend/migrations/') for path in changed):
        raise RuntimeError('Dependency or migration changes require manual deployment')
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
    # Share ownership with workers, so deployment never overlaps a pipeline cycle.
    with exclusive_cycle(ROOT / 'data/pipeline.lock'):
        result = dict(run_id=run_id, sha=sha, success=False)
        try:
            update_checkout(sha, deadline)
            result['success'] = True
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
            logger.error('Hosted deployment stopped (%s); inspect the checkout before retrying', type(error).__name__)
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
                        if result.get('sha') != sha or result.get('success') is not True:
                            raise RuntimeError('Hosted update failed; reload withheld')
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
