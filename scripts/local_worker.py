"""Local SQLite queue, offline similarity inference, and Gmail SMTP delivery."""

import json
import logging
import smtplib
import sqlite3
import threading
import time
from contextlib import closing, contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

from backend import career_sources, matching_tasks, pipeline_skeleton as pipeline, similarity, store, workflow_monitor as monitor
from backend.job_ingestion import deduplicate, mode_for_source, observation_batches
from backend.local_embeddings import LocalScorer
from scripts.run_pipeline import exclusive_cycle

logger = logging.getLogger(__name__)
MIN_EMAIL_SIMILARITY = 60  # Explicitly selected for this local flow by the owner.
FETCH_INTERVAL_SECONDS = 300
POLL_SECONDS = 30
CYCLE_SECONDS = 50 * 60
LOCK_SECONDS = 120
MAX_COMPANIES = 1000
MAX_ACCOUNTS = 1000
PAGE_SIZE = 100
MAX_PAGES = 10000
MAX_EVALUATIONS = 1000
MAX_SNAPSHOT_JOBS = 5000
MAX_SNAPSHOT_BYTES = 2 * 1024 * 1024


def connect(database):
    conn = sqlite3.connect(Path(database).resolve().as_uri() + '?mode=rw', uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys=ON')
    similarity.register_sql(conn)
    return conn


@contextmanager
def writing(database, writes, conn):
    if not writes.acquire(timeout=LOCK_SECONDS):
        raise RuntimeError('Local write lock timed out')
    try:
        with exclusive_cycle(Path(database).parent / 'pipeline.lock'), conn:
            conn.execute('BEGIN IMMEDIATE')
            yield
    finally:
        writes.release()


def eligible_user(conn, user_id):
    row = conn.execute('''SELECT u.* FROM users u JOIN accounts a ON a.user_id=u.id
        WHERE u.id=? AND u.active=1 AND a.email_verified=1 AND trim(u.resume_text)<>'' ''',
                       (user_id,)).fetchone()
    return dict(row) if row else None


def report(database,writes,worker,code,**details):
    with closing(connect(database)) as conn:
        with writing(database,writes,conn):
            monitor.local_record(conn,worker,code,**details)


def eligible_job(conn, user, job):
    return (job['is_active'] and job['company_id'] in matching_tasks.companies_for_user(conn, user['id'])
            and bool(pipeline.filter_jobs_for_user([job], user)))


def validate_jobs(jobs):
    if not isinstance(jobs, list) or len(jobs) > MAX_SNAPSHOT_JOBS:
        raise ValueError('Invalid snapshot size')
    for job in jobs:
        if not isinstance(job, dict) or not isinstance(job.get('raw_json'), dict):
            raise ValueError('Invalid job metadata')
        for field, maximum in (('external_id', 300), ('title', 1000)):
            value = job.get(field)
            if not isinstance(value, str) or not value.strip() or len(value) > maximum:
                raise ValueError('Invalid job text')
        for field in ('location', 'department', 'url'):
            value = job.get(field)
            if value is not None and (not isinstance(value, str) or len(value) > 4000):
                raise ValueError('Invalid optional job text')
        if job.get('url') and store.safe_url(job['url']) is None:
            raise ValueError('Invalid application URL')


def fetch_cycle(database, writes, stop, ready):
    deadline = time.monotonic() + CYCLE_SECONDS
    with closing(connect(database)) as conn:
        companies = conn.execute('''SELECT id,name,ats_type,board_slug,career_source,active
            FROM companies WHERE active=1 ORDER BY id LIMIT ?''', (MAX_COMPANIES + 1,)).fetchall()
        if len(companies) > MAX_COMPANIES:
            raise ValueError('Company catalog exceeds cycle limit')
        for row in companies:
            if stop.is_set() or time.monotonic() >= deadline:
                logger.warning('Fetch cycle interrupted or budget reached; saved jobs remain queued.')
                break
            company = dict(row)
            try:
                source = company['career_source']
                jobs = (career_sources.fetch(source, deadline) if source else
                        pipeline.fetch_jobs_for_company(company))
                mode = mode_for_source(source)
                jobs = deduplicate(jobs, mode)
                batches = (observation_batches({}, jobs, MAX_SNAPSHOT_JOBS, MAX_SNAPSHOT_BYTES)
                           if mode == 'observed' else [dict(jobs=jobs)])
                for batch in batches:
                    if stop.is_set():
                        return
                    validate_jobs(batch['jobs'])
                    if len(json.dumps(batch, allow_nan=False).encode()) > MAX_SNAPSHOT_BYTES:
                        raise ValueError('Snapshot exceeds size limit')
                    with writing(database, writes, conn):
                        current = conn.execute('''SELECT id,name,ats_type,board_slug,career_source,active
                            FROM companies WHERE id=?''', (company['id'],)).fetchone()
                        if current is None or dict(current) != company:
                            raise ValueError('Company changed during fetching')
                        pipeline.upsert_jobs(conn, company['id'], batch['jobs'])
                        monitor.local_record(conn,'fetch','fetch_done',value=len(batch['jobs']))
                    ready.set()
                logger.info('Local fetch saved: company_id=%s jobs=%s', company['id'], len(jobs))
            except (requests.RequestException, ValueError, KeyError, TypeError) as error:
                # A failed read/invalid snapshot cannot replace saved jobs. The next
                # regular fetch remains scheduled; SMTP is never retried this way.
                logger.error('Local board fetch failed: company_id=%s error=%s; saved jobs retained.',
                             company['id'], type(error).__name__)
                report(database,writes,'fetch','fetch_failed',state='error')


def save_result(database, writes, conn, user, job, prepared, value):
    with writing(database, writes, conn):
        current_user = eligible_user(conn, user['id'])
        current_job = conn.execute('SELECT * FROM jobs WHERE id=?', (job['id'],)).fetchone()
        if (current_user is None or current_job is None or
                current_user['resume_text'] != user['resume_text'] or
                current_user['preferences_json'] != user['preferences_json'] or
                similarity.job_hash(dict(current_job)) != similarity.job_hash(job) or
                not eligible_job(conn, current_user, dict(current_job))):
            logger.info('Local scoring input changed; result discarded, eligible work remains queued.')
            return False
        if prepared != job:
            conn.execute('UPDATE jobs SET raw_json=?,title=?,location=? WHERE id=?',
                         (prepared['raw_json'], prepared['title'], prepared.get('location'), job['id']))
            conn.execute('UPDATE scores SET is_stale=1 WHERE job_id=?', (job['id'],))
            matching_tasks.enqueue_company(conn, job['company_id'], datetime.now(timezone.utc))
        if not eligible_job(conn, current_user, prepared):
            return False
        value = similarity.validate_result(value, user['resume_text'], pipeline.extract_job_description(prepared))
        conn.execute('''INSERT INTO local_similarity(user_id,job_id,resume_hash,job_hash,preferences_hash,
            model_revision,scoring_version,cosine,skill_overlap,rank_score,matched_skills,jd_skills_not_mentioned,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(user_id,job_id) DO UPDATE SET
            resume_hash=excluded.resume_hash,job_hash=excluded.job_hash,preferences_hash=excluded.preferences_hash,
            model_revision=excluded.model_revision,scoring_version=excluded.scoring_version,cosine=excluded.cosine,
            skill_overlap=excluded.skill_overlap,rank_score=excluded.rank_score,matched_skills=excluded.matched_skills,
            jd_skills_not_mentioned=excluded.jd_skills_not_mentioned,updated_at=excluded.updated_at''',
            (user['id'], job['id'], similarity.text_hash(user['resume_text']), similarity.job_hash(prepared),
             similarity.text_hash(user['preferences_json']), value['model_revision'], value['scoring_version'],
             value['cosine'], value['skill_overlap'], value['rank_score'], json.dumps(value['matched_skills']),
             json.dumps(value['jd_skills_not_mentioned']), datetime.now(timezone.utc).isoformat()))
        monitor.record(conn,user['id'],'score','score_done',job_id=job['id'],value=value['rank_score'])
    return True


def score_batch(database, writes, stop, scorer):
    deadline, after, evaluated, saved = time.monotonic() + CYCLE_SECONDS, 0, 0, 0
    with closing(connect(database)) as conn:
        for _ in range(MAX_ACCOUNTS):
            row = conn.execute('''SELECT u.id FROM users u JOIN accounts a ON a.user_id=u.id
                WHERE u.id>? AND u.active=1 AND a.email_verified=1
                AND trim(u.resume_text)<>'' ORDER BY u.id LIMIT 1''', (after,)).fetchone()
            if row is None:
                break
            after, job_after = row['id'], 0
            for _ in range(MAX_PAGES):
                user = eligible_user(conn, after)
                if user is None:
                    break
                jobs = conn.execute('''SELECT j.*,c.name company_name FROM jobs j JOIN companies c ON c.id=j.company_id
                    WHERE j.id>? AND j.is_active=1 AND c.active=1
                    AND (NOT EXISTS(SELECT 1 FROM user_settings WHERE user_id=? AND subscriptions_configured=1)
                         OR EXISTS(SELECT 1 FROM company_subscriptions WHERE user_id=? AND company_id=c.id))
                    AND NOT EXISTS(SELECT 1 FROM local_similarity s WHERE s.user_id=? AND s.job_id=j.id
                        AND s.resume_hash=? AND s.preferences_hash=?
                        AND s.job_hash=sc_job_hash(j.title,j.department,j.location,j.raw_json)
                        AND s.model_revision=? AND s.scoring_version=?)
                    ORDER BY j.id LIMIT ?''',
                    (job_after, user['id'], user['id'], user['id'], similarity.text_hash(user['resume_text']),
                     similarity.text_hash(user['preferences_json']), similarity.MODEL_REVISION,
                     similarity.SCORING_VERSION, PAGE_SIZE)).fetchall()
                if jobs:
                    job_after = jobs[-1]['id']
                candidates = pipeline.filter_jobs_for_user([dict(row) for row in jobs], user)
                for job in candidates:
                    if stop.is_set() or evaluated >= MAX_EVALUATIONS or time.monotonic() >= deadline:
                        return saved
                    evaluated += 1
                    try:
                        prepared = career_sources.prepare_job(job, deadline)
                    except (requests.RequestException, ValueError, KeyError, TypeError) as error:
                        logger.error('Local description failed: job_id=%s error=%s; candidate remains pending.',
                                     job['id'], type(error).__name__)
                        continue
                    if not pipeline.filter_jobs_for_user([prepared], user):
                        save_result(database, writes, conn, user, job, prepared, None)
                        continue
                    value = scorer.score(user, prepared, job['company_name'])
                    if save_result(database, writes, conn, user, job, prepared, value):
                        saved += 1
                        logger.info('Local similarity saved: user_id=%s job_id=%s score=%.2f',
                                    user['id'], job['id'], value['rank_score'])
                if len(jobs) < PAGE_SIZE:
                    break
    return saved


def email_text(user, matches, include_gaps):
    lines = ['Hi ' + (user['name'] or 'there') + ',', '', 'New jobs ranked by local resume/job similarity:', '']
    for match in matches:
        lines.extend([match['title'] + ' @ ' + match['company_name'],
                      'Local similarity: %.2f/100' % match['rank_score'],
                      'Skills mentioned in both: ' + (', '.join(json.loads(match['matched_skills'])) or '-'),
                      match['url'] or '', ''])
        if include_gaps:
            lines.append('JD skills not mentioned in your resume: ' +
                         (', '.join(json.loads(match['jd_skills_not_mentioned'])) or '-'))
    lines.extend(['', 'Similarity is a relevance ranking, not a fit percentage or hiring probability.',
                  'Review the full job requirements. Change preferences or turn emails Off in local StudentConnect Settings.'])
    return '\n'.join(lines)


def send_digests(database, writes, stop, minimum=MIN_EMAIL_SIMILARITY):
    if type(minimum) not in (int, float) or not 0 <= minimum <= 100:
        raise ValueError('Similarity email threshold must be between 0 and 100')
    pause = Path(database).parent / 'email_schedule.paused'
    sent, after = 0, 0
    with closing(connect(database)) as conn:
        for _ in range(MAX_ACCOUNTS):
            if stop.is_set():
                break
            with writing(database, writes, conn):
                if pause.exists():
                    raise RuntimeError('Email delivery is paused; reconcile provider and sent history')
                row = conn.execute('''SELECT u.* FROM users u JOIN accounts a ON a.user_id=u.id
                    WHERE u.id>? AND u.active=1 AND a.email_verified=1 ORDER BY u.id LIMIT 1''', (after,)).fetchone()
                if row is None:
                    break
                user, after = dict(row), row['id']
                preferences = store.settings(conn, after)
                if preferences['cadence'] == 'off':
                    continue
                if preferences['cadence'] not in ('daily', 'weekly'):
                    raise ValueError('Invalid email cadence')
                matches = conn.execute('''SELECT s.*,j.title,j.department,j.location,j.raw_json,j.url,c.name company_name ''' +
                    similarity.current_where() + ''' AND s.rank_score>=?
                    AND NOT EXISTS(SELECT 1 FROM sent_log l WHERE l.user_id=s.user_id AND l.job_id=s.job_id AND l.channel='email')
                    ORDER BY s.rank_score DESC,s.job_id LIMIT 5''',
                    (after, similarity.MODEL_REVISION, similarity.SCORING_VERSION, minimum)).fetchall()
                if not matches:
                    continue
                for match in matches:
                    if match['url'] and store.safe_url(match['url']) is None:
                        raise ValueError('Invalid application URL in digest')
                    similarity.validate_result({key: match[key] if key not in ('matched_skills','jd_skills_not_mentioned')
                                                else json.loads(match[key]) for key in similarity.result(0, '', '')},
                                               user['resume_text'], pipeline.extract_job_description(match))
                text = email_text(user, matches, preferences['include_gaps'])
                if len(text) > 10000:
                    raise ValueError('Similarity digest exceeds email text limit')
                pipeline.validate_email_configuration()
                with pause.open('x', encoding='utf-8') as marker:
                    marker.write('Local similarity email attempt requires review unless completed.\n')
                pipeline.deliver_account_email(user['email'], 'StudentConnect: new local similarity matches', text)
                pipeline.mark_sent(conn, after, [match['job_id'] for match in matches])
                for match in matches:
                    monitor.record(conn,after,'email','email_sent',job_id=match['job_id'],state='idle')
                pause.unlink()
                sent += 1
                logger.info('Local similarity email accepted: user_id=%s jobs=%s', after, len(matches))
    return sent


def fetch_loop(database, writes, stop, ready):
    while not stop.is_set():
        started = time.monotonic()
        logger.info('Local independent fetch started.')
        report(database,writes,'fetch','fetch_start')
        fetch_cycle(database, writes, stop, ready)
        delay = max(0, started + FETCH_INTERVAL_SECONDS - time.monotonic())
        logger.info('Local fetch cycle completed; next fetch in %.1f seconds.', delay)
        report(database,writes,'fetch','waiting',state='waiting',
               next_check=(datetime.now(timezone.utc)+timedelta(seconds=delay)).isoformat())
        stop.wait(delay)


def matching_loop(database, writes, stop, ready, minimum):
    emails_enabled = True
    with closing(LocalScorer()) as scorer:
        logger.info('Offline local model loaded; queue scoring started.')
        while not stop.is_set():
            ready.clear()
            report(database,writes,'score','score_start',event=False)
            saved = score_batch(database, writes, stop, scorer)
            report(database,writes,'score','score_idle',state='idle',value=saved,event=False)
            logger.info('Local similarity batch completed: saved=%s', saved)
            if emails_enabled and not stop.is_set():
                try:
                    report(database,writes,'email','email_check',event=False)
                    sent=send_digests(database, writes, stop, minimum)
                    report(database,writes,'email','email_idle',state='idle',value=sent,event=False)
                except (smtplib.SMTPException, OSError, RuntimeError, ValueError, sqlite3.Error) as error:
                    emails_enabled = False
                    logger.error('Local emails stopped (%s); inspect provider, configuration and pause marker before restarting. Scoring continues.',
                                 type(error).__name__)
                    code=monitor.error_code(error)
                    report(database,writes,'email',code if code!='unknown_error' else 'email_error',state='error')
            ready.wait(POLL_SECONDS)


def run(database=None, minimum=MIN_EMAIL_SIMILARITY):
    database = Path(database or pipeline.DB_PATH).resolve()
    if type(minimum) not in (int, float) or not 0 <= minimum <= 100:
        raise ValueError('Similarity email threshold must be between 0 and 100')
    with closing(connect(database)) as conn:
        conn.execute('SELECT rank_score FROM local_similarity LIMIT 1').fetchone()
    stop, ready, writes = threading.Event(), threading.Event(), threading.Lock()
    statuses = [0, 0]

    def loop(index):
        try:
            if index == 0:
                fetch_loop(database, writes, stop, ready)
            else:
                matching_loop(database, writes, stop, ready, minimum)
        except Exception as error:
            statuses[index] = 1
            logger.error('Local %s loop stopped (%s); other loop remains independent.',
                         'fetch' if index == 0 else 'matching', type(error).__name__)
            report(database,writes,'fetch' if index==0 else 'score',monitor.error_code(error),state='error')

    # Share the previous startup worker's lock so old and local modes cannot overlap.
    with exclusive_cycle(database.parent / 'hosted_worker.lock'):
        threads = [threading.Thread(target=loop, args=(index,), name=name, daemon=True)
                   for index, name in enumerate(('local-fetch', 'local-similarity-email'))]
        started = []
        try:
            for thread in threads:
                thread.start()
                started.append(thread)
            while any(thread.is_alive() for thread in started):
                for thread in started:
                    thread.join(timeout=0.5)
        except KeyboardInterrupt:
            logger.warning('Local worker stopping; pending jobs remain stored.')
            return 130
        finally:
            stop.set()
            ready.set()
            deadline = time.monotonic() + LOCK_SECONDS
            for thread in started:
                thread.join(timeout=max(0, deadline - time.monotonic()))
                if thread.is_alive():
                    statuses[0] = 1
                    logger.error('Local shutdown timed out; inspect email state before restarting.')
    return int(any(statuses))
