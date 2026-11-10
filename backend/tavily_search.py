"""Bounded Tavily search for job-discovery leads.

Results are untrusted links and must be verified as job postings before scoring.
"""

import json
import hashlib
import logging
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit

import requests

ENDPOINT = 'https://api.tavily.com/search'
MAX_RESPONSE_BYTES = 1024 * 1024
MAX_RESULTS = 10
logger = logging.getLogger(__name__)


def search(query, api_key, *, deadline=None, domains=None):
    """Return HTTPS leads from one basic search, without sending resume data."""
    if not isinstance(query, str) or not query.strip() or len(query) > 400:
        raise ValueError('Invalid search query')
    if not isinstance(api_key, str) or not api_key.strip():
        raise ValueError('Tavily API key is required')
    if domains is not None:
        from backend.web_discovery import PLATFORMS
        if not isinstance(domains, list) or not domains or any(domain not in PLATFORMS.values() for domain in domains):
            raise ValueError('Invalid search domains')
    deadline = time.monotonic() + 45 if deadline is None else deadline
    remaining = deadline - time.monotonic()
    if remaining < 1:
        raise ValueError('Search time budget exhausted')
    payload = {'query': query.strip(), 'search_depth': 'basic',
               'max_results': MAX_RESULTS, 'auto_parameters': False}
    if domains is not None:
        payload['include_domains'] = domains
    with requests.post(ENDPOINT, headers={'Authorization': 'Bearer ' + api_key},
                       json=payload,
                       timeout=(min(15, remaining), min(30, remaining)), allow_redirects=False, stream=True) as response:
        response.raise_for_status()
        if response.status_code != 200:
            raise ValueError('Unexpected Tavily response status')
        chunks, size = [], 0
        for chunk in response.iter_content(65536):
            size += len(chunk)
            if size > MAX_RESPONSE_BYTES or time.monotonic() >= deadline:
                raise ValueError('Tavily response exceeds size limit')
            chunks.append(chunk)
    payload = json.loads(b''.join(chunks))
    if not isinstance(payload, dict) or not isinstance(payload.get('results'), list) or len(payload['results']) > MAX_RESULTS:
        raise ValueError('Invalid Tavily search response')
    urls = []
    for result in payload['results']:
        if not isinstance(result, dict) or not isinstance(result.get('url'), str):
            raise ValueError('Invalid Tavily search result')
        url = result['url']
        if len(url) > 2000 or any(ord(char) < 33 for char in url):
            raise ValueError('Invalid Tavily result URL')
        parsed = urlsplit(url)
        if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.port not in (None, 443):
            raise ValueError('Invalid Tavily result URL')
        if url not in urls:
            urls.append(url)
    return urls


def posting_scope(company):
    """Use configured company ownership, never a host supplied by search."""
    from backend.career_sources import SOURCES, WORKDAY
    source = company.get('career_source')
    if source:
        _, kind, origin = SOURCES[source]
        if kind == 'blocked':
            raise ValueError('Unsupported career source')
        if kind in ('greenhouse', 'ashby'):
            return ('boards.greenhouse.io' if kind == 'greenhouse' else 'jobs.ashbyhq.com'), '/' + source + '/'
        if kind == 'workday':
            return urlsplit(origin).hostname, '/en-US/' + WORKDAY[source][1] + '/job/'
        return urlsplit(origin).hostname, '/'
    hosts = {'greenhouse': 'boards.greenhouse.io', 'lever': 'jobs.lever.co',
             'ashby': 'jobs.ashbyhq.com'}
    slug = company['board_slug']
    if not isinstance(slug, str) or not slug or not all(c.isascii() and (c.isalnum() or c in '-_') for c in slug):
        raise ValueError('Invalid board slug')
    return hosts[company['ats_type']], '/' + slug + '/'


def canonical_url(url):
    """Keep identity-bearing query parameters while ignoring anchors and final slashes."""
    parsed = urlsplit(url)
    return parsed._replace(fragment='', path=parsed.path.rstrip('/')).geturl()


def verify_posting(url, company, deadline):
    """Verify one employer-owned JSON-LD posting without following redirects."""
    host, prefix = posting_scope(company)
    if not isinstance(url, str) or len(url) > 2000 or any(ord(char) < 33 for char in url):
        raise ValueError('Invalid posting URL')
    parsed = urlsplit(url)
    if (parsed.scheme != 'https' or parsed.hostname != host or parsed.port not in (None, 443)
            or parsed.username or parsed.password or not parsed.path.startswith(prefix)
            or '%' in parsed.path or '\\' in parsed.path or any(part in ('.', '..') for part in parsed.path.split('/'))):
        raise ValueError('Search lead is outside the configured company site')
    return posting_from_record(read_record(url, deadline), url, company['name'])


def read_record(url, deadline):
    """Read one bounded page; callers validate the destination before requesting it."""
    from backend.career_sources import Page
    from backend.recruiter_sources import schema_jobs
    remaining = deadline - time.monotonic()
    if remaining < 1:
        raise ValueError('Discovery time budget exhausted')
    with requests.get(url, timeout=(min(15, remaining), min(30, remaining)),
                      allow_redirects=False, stream=True) as response:
        response.raise_for_status()
        if response.status_code != 200:
            raise ValueError('Posting redirected or is unavailable')
        chunks, size = [], 0
        for chunk in response.iter_content(65536):
            size += len(chunk)
            if size > MAX_RESPONSE_BYTES or time.monotonic() >= deadline:
                raise ValueError('Posting exceeds discovery limits')
            chunks.append(chunk)
    records = [record for block in Page(b''.join(chunks).decode('utf-8')).structured
               for record in schema_jobs(block)]
    if len(records) != 1:
        raise ValueError('A single structured job posting is required')
    return records[0]


def posting_from_record(record, url, employer_name):
    """Validate structured evidence rather than treating a search snippet as a job."""
    from backend.career_sources import MAX_DESCRIPTION
    from backend.pipeline_skeleton import _clean_description
    employer = record.get('hiringOrganization')
    if not isinstance(employer, dict) or not isinstance(employer.get('name'), str) or employer['name'].strip().casefold() != employer_name.strip().casefold():
        raise ValueError('Posting employer does not match company')
    title, description = record.get('title'), record.get('description')
    if (not isinstance(title, str) or not title.strip() or len(title) > 1000
            or not isinstance(description, str) or not _clean_description(description)
            or len(description) > MAX_DESCRIPTION):
        raise ValueError('Posting lacks usable title or description')
    expiry = record.get('validThrough')
    if expiry:
        if not isinstance(expiry, str):
            raise ValueError('Invalid posting expiry')
        end = datetime.fromisoformat(expiry.replace('Z', '+00:00'))
        if end.tzinfo is None:
            raise ValueError('Posting expiry requires a timezone')
        if end <= datetime.now(timezone.utc):
            raise ValueError('Posting has expired')
    locations = record.get('jobLocation', [])
    if isinstance(locations, dict):
        locations = [locations]
    if not isinstance(locations, list) or len(locations) > 50:
        raise ValueError('Invalid posting locations')
    names = []
    for location in locations:
        if not isinstance(location, dict) or not isinstance(location.get('address'), dict):
            raise ValueError('Invalid posting address')
        for field in ('addressLocality', 'addressRegion', 'addressCountry'):
            value = location['address'].get(field)
            if value is not None:
                if not isinstance(value, str):
                    raise ValueError('Invalid location text')
                names.append(value)
    location = ', '.join(dict.fromkeys(names))
    employment = record.get('employmentType')
    if len(location) > 4000 or employment is not None and (not isinstance(employment, str) or len(employment) > 200):
        raise ValueError('Invalid posting metadata')
    canonical = canonical_url(url)
    raw = dict(descriptionHtml=description, discovery_source='tavily')
    if employment:
        raw['employmentType'] = employment
    return dict(external_id='tavily-' + hashlib.sha256(canonical.encode()).hexdigest(),
                title=title.strip(), location=location or None, department=None, url=canonical, raw_json=raw)


def discover(company, existing, api_key, deadline):
    """Return verified additional observations; invalid leads are reported and skipped."""
    host, prefix = posting_scope(company)
    name = company.get('name')
    if not isinstance(name, str) or not name.strip() or len(name) > 200:
        raise ValueError('Invalid company search name')
    if time.monotonic() >= deadline - 45:
        raise ValueError('Insufficient discovery time budget')
    urls = search('site:' + host + prefix + ' ' + name + ' jobs careers India', api_key, deadline=deadline)
    known = {canonical_url(job['url'])
             for job in existing if job.get('url')}
    additional = []
    for url in urls:
        canonical = canonical_url(url)
        if canonical in known:
            continue
        if time.monotonic() >= deadline - 1:
            raise ValueError('Discovery time budget exhausted')
        try:
            posting = verify_posting(url, company, deadline)
        except (requests.RequestException, ValueError, KeyError, TypeError, UnicodeError) as error:
            logger.warning('Tavily lead rejected: company_id=%s error=%s', company.get('id'), type(error).__name__)
            continue
        known.add(canonical)
        additional.append(posting)
    return additional
