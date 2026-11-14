"""Delete only an explicitly approved inactive-release manifest through the Files API."""

import argparse
import hashlib
import json
import logging
import os
from pathlib import Path, PurePosixPath
import re
import time

import requests

from scripts import deploy_pythonanywhere as deployment
from scripts.inspect_pythonanywhere_storage import bounded_json

logger = logging.getLogger(__name__)


def validate_plan(content, expected_hash):
    if len(content) > 16384 or hashlib.sha256(content).hexdigest() != expected_hash:
        raise ValueError('Approved cleanup manifest does not match the confirmation')
    plan = json.loads(content)
    if not isinstance(plan, dict) or set(plan) != {'root','preserve','remove_candidates','active_release','pending_release'}:
        raise ValueError('Invalid cleanup manifest')
    root = PurePosixPath(deployment.ROOT.as_posix()) / '.releases'
    if plan['root'] != str(root):
        raise ValueError('Cleanup root is outside the approved release directory')
    for field in ('preserve','remove_candidates'):
        values = plan[field]
        if (not isinstance(values,list) or not 1 <= len(values) <= 40 or len(values) != len(set(values)) or
                any(not isinstance(value,str) or not re.fullmatch(r'[0-9a-f]{40}',value) for value in values)):
            raise ValueError('Invalid approved release identities')
    if set(plan['preserve']) & set(plan['remove_candidates']):
        raise ValueError('Protected releases cannot be deleted')
    if plan['active_release'] not in plan['preserve'] or plan['pending_release'] not in plan['preserve']:
        raise ValueError('Active and pending releases must be preserved')
    return plan


def prune(session, plan):
    root = PurePosixPath(plan['root'])
    project = root.parent
    next_request = 0.0
    deadline = time.monotonic() + 7 * 60

    def call(method, endpoint, **kwargs):
        nonlocal next_request
        delay = max(0.0,next_request-time.monotonic())
        if time.monotonic()+delay>=deadline:
            raise RuntimeError('Cleanup inspection time limit reached')
        if delay:
            time.sleep(delay)
        # The service allows 40 requests/minute; mutations are never retried.
        next_request=time.monotonic()+1.6
        return session.request(method,deployment.API+endpoint,timeout=(10,30),allow_redirects=False,stream=True,**kwargs)

    def identity(filename):
        with call('GET','files/path'+str(project/'data'/filename)) as response:
            if response.status_code!=200:
                raise RuntimeError('Cannot verify current release identity; cleanup stopped')
            metadata=bounded_json(response)
        sha=metadata.get('sha') if isinstance(metadata,dict) else None
        if not isinstance(sha,str) or not re.fullmatch(r'[0-9a-f]{40}',sha):
            raise ValueError('Invalid current release identity')
        return sha

    deleted=0
    absent=0
    for sha in plan['remove_candidates']:
        # Recheck immediately before every deletion, including the pending release.
        active=identity('active-release.json')
        pending=identity('deploy-request.json')
        if active!=plan['active_release'] or pending!=plan['pending_release']:
            raise RuntimeError('Deployment state changed; cleanup stopped before another deletion')
        target=root/sha
        if target.parent!=root or sha in plan['preserve'] or sha in (active,pending):
            raise ValueError('Cleanup target is protected')
        with call('DELETE','files/path'+str(target)) as response:
            if response.status_code==204:
                deleted+=1
                logger.info('Deleted approved inactive code release=%s',sha)
            elif response.status_code==404:
                absent+=1
                logger.info('Approved inactive code release already absent=%s',sha)
            else:
                raise RuntimeError('Deletion was rejected; cleanup stopped without retry')
    logger.info('Approved cleanup complete: deleted=%s already_absent=%s; protected releases, shared data and backups untouched',deleted,absent)
    return dict(deleted=deleted,already_absent=absent)


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan',required=True)
    parser.add_argument('--confirm-sha256',required=True)
    args=parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO,format='%(levelname)s: %(message)s')
    try:
        path=Path(args.plan).resolve()
        if path.parent != Path(__file__).resolve().parent or path.stat().st_size>16384:
            raise ValueError('Use the reviewed manifest alongside this maintenance script')
        plan=validate_plan(path.read_bytes(),args.confirm_sha256)
        token=os.environ.get('PYTHONANYWHERE_API_TOKEN','').strip()
        if not token:
            raise ValueError('Hosting API credential is required')
        with requests.Session() as session:
            session.headers.update(Authorization='Token '+token)
            prune(session,plan)
        return 0
    except (OSError,requests.RequestException,ValueError,RuntimeError,KeyError,TypeError) as error:
        logger.error('Approved cleanup stopped (%s); reconcile remaining release directories before another run; no automatic retry',type(error).__name__)
        return 1


if __name__=='__main__':
    raise SystemExit(main())
