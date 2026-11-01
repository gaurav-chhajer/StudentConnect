"""Publish local jobs additively and score authoritative hosted profile inputs.

Never copies local account IDs, profiles, email history or application records.
"""

import json
import logging
import time
from contextlib import closing

from backend import local_embeddings
from backend.job_ingestion import observation_batches
from scripts import run_cloud_worker as remote, run_similarity

logger = logging.getLogger(__name__)
MAX_LOCAL_JOBS = 100000


def identity(company):
    source = company['career_source']
    return ('career', source) if source else (company['ats_type'], company['board_slug'])


def synchronize(conn, client, deadline, stop):
    """Bounded idempotent uploads; hosted fingerprints protect concurrent changes."""
    if stop.is_set() or time.monotonic() >= deadline - 120:
        return False
    sources = [row[0] for row in conn.execute(
        'SELECT DISTINCT career_source FROM companies WHERE active=1 AND career_source IS NOT NULL LIMIT ?',
        (remote.MAX_COMPANIES + 1,))]
    if len(sources) > remote.MAX_COMPANIES:
        raise ValueError('Local company catalog exceeds sync limit')
    if sources and client.call('sync-catalog', {'sources': sources}).get('saved') is not True:
        raise RuntimeError('Hosted company catalog was not acknowledged')
    companies = client.call('companies').get('companies')
    if not isinstance(companies, list) or len(companies) > remote.MAX_COMPANIES:
        raise ValueError('Invalid hosted company catalog')
    catalog = {}
    for company in companies:
        if (not isinstance(company, dict) or type(company.get('id')) is not int or
                company['id'] <= 0 or not isinstance(company.get('fingerprint'), str) or
                len(company['fingerprint']) != 64 or
                company.get('ats_type') not in ('greenhouse', 'lever', 'ashby') or
                not isinstance(company.get('board_slug'), str) or
                not (company.get('career_source') is None or isinstance(company['career_source'], str))):
            raise ValueError('Invalid hosted company identity')
        key = identity(company)
        if key in catalog:
            raise ValueError('Ambiguous hosted company identity')
        catalog[key] = company
    saved, scanned = 0, 0
    for local in conn.execute('SELECT id,ats_type,board_slug,career_source FROM companies WHERE active=1 ORDER BY id'):
        hosted = catalog.get(identity(local))
        if hosted is None:
            logger.warning('Hosted catalog lacks local company_id=%s; jobs retained locally.', local['id'])
            continue
        after = 0
        while True:
            if stop.is_set() or time.monotonic() >= deadline - 120:
                return False
            rows = conn.execute('''SELECT id,external_id,title,location,department,url,raw_json FROM jobs
                WHERE company_id=? AND is_active=1 AND id>? ORDER BY id LIMIT 100''', (local['id'], after)).fetchall()
            if not rows:
                break
            scanned += len(rows)
            if scanned > MAX_LOCAL_JOBS:
                raise ValueError('Local sync job budget exceeded')
            after = rows[-1]['id']
            jobs = []
            for row in rows:
                job = dict(row)
                job.pop('id')
                job['raw_json'] = json.loads(job['raw_json'])
                jobs.append(job)
            envelope = dict(company_id=hosted['id'], fingerprint=hosted['fingerprint'], mode='observed')
            for batch in observation_batches(envelope, jobs, remote.MAX_JOBS, remote.MAX_BODY_BYTES):
                if stop.is_set() or time.monotonic() >= deadline - 120:
                    return False
                reply = client.call('observations', batch)
                if reply.get('saved') != len(batch['jobs']):
                    raise RuntimeError('Hosted jobs were not acknowledged')
                saved += len(batch['jobs'])
    logger.info('Hosted additive job sync completed: observations=%s.', saved)
    if stop.is_set():
        return False
    # Profiles and preferences come from the hosted work endpoint. Recompute
    # rather than attach a local user's score to a different hosted identity.
    with closing(local_embeddings.LocalScorer()) as scorer:
        return run_similarity.score_pending(client, deadline, scorer) == 0


def run(database, stop):
    from scripts.local_worker import connect
    with closing(connect(database)) as conn, closing(remote.HostedClient()) as client:
        return synchronize(conn, client, time.monotonic() + remote.MATCHING_SECONDS, stop)
