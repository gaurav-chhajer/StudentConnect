"""Explicit read-only live verification; never touches SQLite, Groq or email."""

import argparse
import json
import logging
import time

import requests

from backend import career_sources
from backend.cloud_worker import MAX_BODY_BYTES

logger = logging.getLogger(__name__)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('sources', nargs='*', choices=tuple(career_sources.SOURCES))
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
    failed = False
    for key in args.sources or [key for key, value in career_sources.SOURCES.items() if value[1] != 'blocked']:
        started = time.monotonic()
        logger.info('Checking career source=%s', key)
        try:
            jobs = career_sources.fetch(key, started + 300)
            size = len(json.dumps(dict(company_id=1, fingerprint='0' * 64, jobs=jobs)).encode())
            if size > MAX_BODY_BYTES:
                raise ValueError('Snapshot exceeds the hosted transfer limit')
            if jobs:
                candidate = dict(jobs[0], raw_json=json.dumps(jobs[0]['raw_json']))
                prepared = career_sources.prepare_job(candidate)
                from backend.pipeline_skeleton import extract_job_description
                extract_job_description(prepared)
            logger.info('Verified career source=%s jobs=%s snapshot_bytes=%s seconds=%.1f',
                        key, len(jobs), size, time.monotonic() - started)
        except (requests.RequestException, ValueError, KeyError, TypeError, IndexError) as error:
            response = error.response if isinstance(error, requests.RequestException) else None
            logger.error('Verification failed source=%s error=%s http_status=%s seconds=%.1f', key,
                         type(error).__name__, response.status_code if response is not None else 'unavailable',
                         time.monotonic() - started)
            if type(error) is ValueError and str(error).startswith((
                    'Career ', 'Workday ', 'Oracle ', 'Nomura ', 'Full ', 'Unsupported ',
                    'Snapshot ', 'Publicis ', 'IDFC ', 'Morgan ')):
                logger.error('Source validation: %s', error)
            if type(error) is KeyError and error.args and error.args[0] in (
                    'total', 'jobPostings', 'jobPostingInfo', 'jobDescription', 'title', 'location'):
                logger.error('Missing expected public feed field: %s', error.args[0])
            failed = True
    return int(failed)


if __name__ == '__main__':
    raise SystemExit(main())
