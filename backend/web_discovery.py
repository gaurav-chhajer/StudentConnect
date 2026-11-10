"""Role-only discovery with hosted, atomic credit reservations and verified jobs."""

import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

from flask import current_app

from backend import matching_tasks, pipeline_skeleton as pipeline, role_titles, tavily_search

MONTHLY_CREDITS = 1000
DAILY_SEARCHES = MONTHLY_CREDITS // 30
INDIA = timezone(timedelta(hours=5, minutes=30))
PLATFORMS = {
    'LinkedIn': 'linkedin.com', 'Indeed': 'indeed.com', 'Wellfound': 'wellfound.com',
    'Greenhouse': 'greenhouse.io', 'Lever': 'lever.co', 'Ashby': 'ashbyhq.com',
    'HiringCafe': 'hiringcafe.com',
}
MAX_PROFILES = 1000


def public_url(url, platform):
    """Keep verification on the requested platform; never follow redirects or credentials."""
    if platform not in PLATFORMS or not isinstance(url, str) or len(url) > 2000 or any(ord(c) < 33 for c in url):
        raise ValueError('Invalid discovery destination')
    parsed = urlsplit(url)
    root = PLATFORMS[platform]
    host = parsed.hostname or ''
    if (parsed.scheme != 'https' or not (host == root or host.endswith('.' + root)) or
            parsed.username or parsed.password or parsed.port not in (None, 443) or
            '%' in parsed.path or '\\' in url or any(part in ('.', '..') for part in parsed.path.split('/'))):
        raise ValueError('Discovery destination is outside its platform')
    path = parsed.path.rstrip('/')
    if platform == 'Lever':
        path = path.removesuffix('/apply')
    elif platform == 'Ashby':
        path = path.removesuffix('/application')
    return parsed._replace(path=path, fragment='').geturl()


def verify(url, platform, deadline):
    url = public_url(url, platform)
    record = tavily_search.read_record(url, deadline)
    organization = record.get('hiringOrganization')
    employer = organization.get('name') if isinstance(organization, dict) else None
    if (not isinstance(employer, str) or not employer.strip() or len(employer) > 200 or
            any(ord(c) < 32 for c in employer)):
        raise ValueError('Posting has no usable employer')
    posting = tavily_search.posting_from_record(record, url, employer)
    posting['employer'] = employer.strip()
    posting['raw_json']['discovery_platform'] = platform
    return posting


def titles(profile):
    if profile is None or profile['status'] != 'ready':
        return []
    # Use demonstrated aliases, not resume evidence, preferences or explanatory prose.
    return list(dict.fromkeys(entry['aliases'][0].strip() for entry in profile['families']))


def broad_user(conn, user_id):
    return conn.execute("""SELECT u.id,u.resume_text,u.preferences_json FROM users u
        JOIN accounts a ON a.user_id=u.id WHERE u.id=? AND u.active=1
        AND a.email_verified=1 AND trim(u.resume_text)<>''
        AND NOT EXISTS(SELECT 1 FROM user_settings WHERE user_id=u.id AND subscriptions_configured=1)""",
        (user_id,)).fetchone()


def reserve(conn, now):
    """Caller holds BEGIN IMMEDIATE. Reservations are charged even if work is interrupted."""
    from backend.cloud_worker import fingerprint
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError('Discovery budget requires an aware clock')
    day = now.astimezone(INDIA).date().isoformat()
    month = day[:7]
    conn.execute("INSERT OR IGNORE INTO tavily_budget(id,day,month,daily_used,monthly_used) VALUES(1,?,?,0,0)",
                 (day, month))
    state = conn.execute('SELECT * FROM tavily_budget WHERE id=1').fetchone()
    # An older clock must not reset already spent credits.
    if day < state['day']:
        raise ValueError('Discovery clock moved backwards')
    daily = state['daily_used'] if state['day'] == day else 0
    monthly = state['monthly_used'] if state['month'] == month else 0
    if daily >= DAILY_SEARCHES or monthly >= MONTHLY_CREDITS:
        return dict(search=None, reason='budget_exhausted', daily_used=daily, monthly_used=monthly)
    users = conn.execute("""SELECT u.id FROM users u JOIN accounts a ON a.user_id=u.id
        WHERE u.active=1 AND a.email_verified=1 AND trim(u.resume_text)<>''
        AND NOT EXISTS(SELECT 1 FROM user_settings WHERE user_id=u.id AND subscriptions_configured=1)
        ORDER BY (u.id<=?),u.id LIMIT ?""", (state['last_user'], MAX_PROFILES)).fetchall()
    for row in users:
        user = broad_user(conn, row['id'])
        aliases = titles(role_titles.current(conn, user))
        if not aliases:
            continue
        position = conn.execute('SELECT position FROM tavily_cursors WHERE user_id=?', (user['id'],)).fetchone()
        position = position[0] if position else 0
        platform = tuple(PLATFORMS)[position % len(PLATFORMS)]
        title = aliases[(position // len(PLATFORMS)) % len(aliases)]
        identity = fingerprint(dict(user))
        search_id = conn.execute("""INSERT INTO tavily_searches(user_id,fingerprint,title,platform,created_at)
            VALUES(?,?,?,?,?)""", (user['id'], identity, title, platform, now.astimezone(timezone.utc).isoformat())).lastrowid
        conn.execute("""INSERT INTO tavily_cursors(user_id,position) VALUES(?,?)
            ON CONFLICT(user_id) DO UPDATE SET position=excluded.position""", (user['id'], position + 1))
        conn.execute('UPDATE tavily_budget SET day=?,month=?,daily_used=?,monthly_used=?,last_user=? WHERE id=1',
                     (day, month, daily + 1, monthly + 1, user['id']))
        return dict(search=dict(id=search_id, title=title, platform=platform, domain=PLATFORMS[platform]),
                    daily_used=daily + 1, monthly_used=monthly + 1)
    return dict(search=None, reason='no_ready_role_titles', daily_used=daily, monthly_used=monthly)


def validate_posting(posting, platform):
    if not isinstance(posting, dict) or set(posting) != {'employer', 'external_id', 'title', 'location', 'department', 'url', 'raw_json'}:
        raise ValueError('Invalid discovered posting fields')
    employer = posting['employer']
    if not isinstance(employer, str) or not employer.strip() or len(employer) > 200 or any(ord(c) < 32 for c in employer):
        raise ValueError('Invalid discovered employer')
    url = public_url(posting['url'], platform)
    if url != posting['url']:
        raise ValueError('Posting URL must be canonical')
    raw = posting['raw_json']
    if (not isinstance(raw, dict) or set(raw) - {'descriptionHtml', 'discovery_source', 'discovery_platform', 'employmentType'} or
            raw.get('discovery_source') != 'tavily' or raw.get('discovery_platform') != platform):
        raise ValueError('Invalid discovery evidence')
    # Reuse the same metadata bounds as worker-side verification.
    record = dict(title=posting['title'], description=raw.get('descriptionHtml'),
                  hiringOrganization={'name': employer}, employmentType=raw.get('employmentType'))
    expected = tavily_search.posting_from_record(record, url, employer)
    if posting['external_id'] != expected['external_id'] or posting['department'] is not None:
        raise ValueError('Invalid discovery identity')
    location = posting['location']
    if location is not None and (not isinstance(location, str) or len(location) > 4000 or any(ord(c) < 32 for c in location)):
        raise ValueError('Invalid discovery location')
    if any(ord(c) < 32 for c in posting['title']):
        raise ValueError('Invalid discovery title')


def save_posting(conn, posting, now):
    """Caller owns the transaction. Discovery never closes jobs missing from search results."""
    employer = posting['employer'].strip()
    company = conn.execute('SELECT id,active FROM companies WHERE lower(trim(name))=lower(?) ORDER BY id LIMIT 1',
                           (employer,)).fetchone()
    if company and not company['active']:
        return False
    if company is None:
        slug = 'discovery-' + hashlib.sha256(employer.casefold().encode()).hexdigest()
        company_id = conn.execute("""INSERT INTO companies(name,ats_type,board_slug,discovery_only)
            VALUES(?,'greenhouse',?,1)""", (employer, slug)).lastrowid
    else:
        company_id = company['id']
    description = pipeline._clean_description(posting['raw_json']['descriptionHtml'])
    signature = hashlib.sha256(json.dumps([company_id, posting['title'].casefold(),
        posting['location'], re.sub(r'\s+', ' ', description).strip()], ensure_ascii=False).encode()).hexdigest()
    url_job = conn.execute("SELECT id,external_id,raw_json FROM jobs WHERE company_id=? AND rtrim(url,'/')=? ORDER BY id LIMIT 1",
                          (company_id, posting['url'].rstrip('/'))).fetchone()
    same = conn.execute('SELECT job_id FROM discovery_job_signatures WHERE signature=?', (signature,)).fetchone()
    if same and (not url_job or url_job['id'] != same['job_id']):
        return False
    if url_job and not (url_job['external_id'].startswith('tavily-') and
                       json.loads(url_job['raw_json'] or '{}').get('discovery_source') == 'tavily'):
        return False  # A native ATS record stays authoritative.
    stamp = now.astimezone(timezone.utc).isoformat()
    if url_job:
        job_id = url_job['id']
        previous = conn.execute('SELECT title,location,raw_json,is_active FROM jobs WHERE id=?', (job_id,)).fetchone()
        raw = json.dumps(posting['raw_json'])
        changed = tuple(previous) != (posting['title'], posting['location'], raw, 1)
        conn.execute('UPDATE jobs SET title=?,location=?,raw_json=?,last_seen_at=?,is_active=1 WHERE id=?',
                     (posting['title'], posting['location'], raw, stamp, job_id))
        if changed:
            conn.execute('UPDATE scores SET is_stale=1 WHERE job_id=?', (job_id,))
    else:
        job_id = conn.execute("""INSERT INTO jobs(company_id,external_id,title,location,url,raw_json,first_seen_at,last_seen_at,is_active)
            VALUES(?,?,?,?,?,?,?,?,1)""", (company_id, posting['external_id'], posting['title'], posting['location'],
            posting['url'], json.dumps(posting['raw_json']), stamp, stamp)).lastrowid
        changed = True
    conn.execute('INSERT OR IGNORE INTO discovery_job_signatures(signature,job_id) VALUES(?,?)', (signature, job_id))
    if changed:
        matching_tasks.enqueue_company(conn, company_id, now)
    return changed


def register_routes(routes, connection, database):
    from backend.cloud_worker import body, fingerprint, positive_id
    from scripts.run_pipeline import exclusive_cycle

    @routes.post('/discovery/reserve')
    def reserve_search():
        if body() != {}:
            raise ValueError('Unexpected search reservation fields')
        conn = connection()
        with exclusive_cycle(Path(database).parent / 'pipeline.lock'), conn:
            conn.execute('BEGIN IMMEDIATE')
            return reserve(conn, datetime.now(timezone.utc))

    @routes.post('/discovery/results')
    def discovery_results():
        data = body()
        if set(data) != {'id', 'status', 'postings', 'rejected'}:
            raise ValueError('Invalid discovery result fields')
        search_id = positive_id(data['id'])
        postings, rejected, status = data['postings'], data['rejected'], data['status']
        if (status not in ('complete', 'failed') or not isinstance(postings, list) or
                len(postings) > tavily_search.MAX_RESULTS or type(rejected) is not int or
                not 0 <= rejected <= tavily_search.MAX_RESULTS or len(postings) + rejected > tavily_search.MAX_RESULTS or
                status == 'failed' and postings):
            raise ValueError('Invalid discovery outcome')
        conn = connection()
        with exclusive_cycle(Path(database).parent / 'pipeline.lock'), conn:
            conn.execute('BEGIN IMMEDIATE')
            search = conn.execute('SELECT * FROM tavily_searches WHERE id=?', (search_id,)).fetchone()
            if search is None:
                raise ValueError('Unknown search reservation')
            if search['status'] != 'reserved':
                return dict(saved=search['saved'], already_completed=True)
            for posting in postings:
                validate_posting(posting, search['platform'])
            user = broad_user(conn, search['user_id'])
            if user is None or fingerprint(dict(user)) != search['fingerprint']:
                conn.execute("UPDATE tavily_searches SET status='failed' WHERE id=?", (search_id,))
                return dict(saved=0, inputs_changed=True)
            profile = role_titles.current(conn, user)
            if profile is None or profile['status'] != 'ready':
                conn.execute("UPDATE tavily_searches SET status='failed' WHERE id=?", (search_id,))
                return dict(saved=0, inputs_changed=True)
            saved = 0
            for posting in postings:
                if role_titles.match_index(posting['title'], profile) is not None:
                    saved += int(save_posting(conn, posting, datetime.now(timezone.utc)))
            conn.execute('UPDATE tavily_searches SET status=?,verified=?,rejected=?,saved=? WHERE id=?',
                         (status, len(postings), rejected, saved, search_id))
        current_app.logger.info('Web discovery completed: search_id=%s platform=%s verified=%s rejected=%s saved=%s status=%s',
                                search_id, search['platform'], len(postings), rejected, saved, status)
        return dict(saved=saved)
