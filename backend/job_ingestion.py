"""Source-owned ingestion policy for complete feeds and paginated observations."""

import json
import logging


def mode_for_source(source):
    if not source:
        return 'snapshot'
    from backend.career_sources import SOURCES
    if source not in SOURCES or SOURCES[source][1] == 'blocked':
        raise ValueError('Unsupported career source')
    return 'snapshot' if SOURCES[source][1] in ('morgan', 'sitemap') else 'observed'


def deduplicate(jobs, mode):
    """Conflicting observations leave the stored job untouched for this run."""
    if mode not in ('snapshot', 'observed'):
        raise ValueError('Invalid ingestion mode')
    unique, signatures, conflicts = {}, {}, set()
    for job in jobs:
        identifier = job['external_id']
        signature = json.dumps(job, sort_keys=True, allow_nan=False)
        if identifier in signatures and signatures[identifier] != signature:
            conflicts.add(identifier)
        signatures[identifier] = signature
        unique[identifier] = job
    if conflicts:
        if mode == 'snapshot':
            raise ValueError('Conflicting snapshot jobs')
        logging.getLogger(__name__).warning(
            'Conflicting observations withheld: jobs=%s', len(conflicts))
    return [job for identifier, job in unique.items() if identifier not in conflicts]


def observation_batches(envelope, jobs, max_jobs, max_bytes):
    """Bound transfers by the exact JSON representation used by the worker."""
    base = len(json.dumps(dict(envelope, jobs=[]), allow_nan=False).encode())
    batch, size = [], base
    for job in jobs:
        length = len(json.dumps(job, allow_nan=False).encode())
        if base + length > max_bytes:
            raise ValueError('Job exceeds transfer limit')
        addition = length + (2 if batch else 0)
        if batch and (len(batch) >= max_jobs or size + addition > max_bytes):
            yield dict(envelope, jobs=batch)
            batch, size, addition = [], base, length
        batch.append(job)
        size += addition
    if batch or not jobs:
        yield dict(envelope, jobs=batch)
