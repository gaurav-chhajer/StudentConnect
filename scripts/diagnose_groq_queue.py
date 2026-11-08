"""Read-only Groq queue exclusion counts; no provider requests or private text output.

Run against the authoritative database with the same environment/model as the web
app: python -B -m scripts.diagnose_groq_queue /absolute/path/to/data/jobs.db
"""

import argparse
from contextlib import closing, redirect_stdout
import json
import logging
from pathlib import Path
import sqlite3
import sys

from backend import pipeline_skeleton as pipeline, role_titles

logger = logging.getLogger(__name__)
PAGE_SIZE = 500
MAX_PAGES = 1000
MAX_ACCOUNTS = 1000
STAGES = ('active_jobs', 'unscored', 'subscribed', 'employment_and_seniority',
          'role_preferences', 'location_preferences', 'stipend', 'role_family')


def inspect_queue(conn):
    """Reuse actual filters in bounded pages; emit only IDs, statuses and counts."""
    users = conn.execute('''SELECT u.id,u.resume_text,u.preferences_json FROM users u
        JOIN accounts a ON a.user_id=u.id WHERE u.active=1 AND a.email_verified=1
        AND trim(u.resume_text)<>'' ORDER BY u.id LIMIT ?''', (MAX_ACCOUNTS + 1,)).fetchall()
    if len(users) > MAX_ACCOUNTS:
        raise ValueError('Diagnostic account limit exceeded')
    reports = []
    for user in users:
        profile = role_titles.current(conn, user)
        effective = role_titles.filtering_user(user, profile)
        preferences = json.loads(effective['preferences_json'])
        remaining = dict.fromkeys(STAGES, 0)
        after = 0
        for _ in range(MAX_PAGES):
            page = conn.execute('''SELECT j.*,
                EXISTS(SELECT 1 FROM scores s WHERE s.user_id=? AND s.job_id=j.id
                    AND s.is_stale=0) AS current_score,
                (NOT EXISTS(SELECT 1 FROM user_settings WHERE user_id=? AND subscriptions_configured=1)
                    OR EXISTS(SELECT 1 FROM company_subscriptions WHERE user_id=? AND company_id=j.company_id)) AS subscribed
                FROM jobs j JOIN companies c ON c.id=j.company_id
                WHERE j.is_active=1 AND c.active=1 AND j.id>?
                ORDER BY j.id LIMIT ?''', (user['id'], user['id'], user['id'], after, PAGE_SIZE)).fetchall()
            if not page:
                break
            after = page[-1]['id']
            remaining['active_jobs'] += len(page)
            page = [job for job in page if not job['current_score']]
            remaining['unscored'] += len(page)
            page = [job for job in page if job['subscribed']]
            remaining['subscribed'] += len(page)
            selected = {'employment_types': preferences.get('employment_types', [])}
            # The existing filter prints aggregate diagnostics. Keep those on stderr,
            # preserving stdout as a machine-readable report without suppressing errors.
            with redirect_stdout(sys.stderr):
                page = pipeline.filter_jobs_for_user(page, dict(user, preferences_json=json.dumps(selected)))
                remaining['employment_and_seniority'] += len(page)
                for stage, key in (('role_preferences', 'roles'), ('location_preferences', 'locations'),
                                   ('stipend', 'min_stipend')):
                    if key in preferences:
                        selected[key] = preferences[key]
                    page = pipeline.filter_jobs_for_user(page, dict(user, preferences_json=json.dumps(selected)))
                    remaining[stage] += len(page)
            if profile is not None:
                remaining['role_family'] += len(role_titles.select(page, profile))
        else:
            raise ValueError('Diagnostic job limit exceeded; report withheld')
        if remaining['role_family']:
            reason = 'ready_for_scoring'
        elif not remaining['active_jobs']:
            reason = 'no_active_jobs'
        elif not remaining['unscored']:
            reason = 'all_active_jobs_already_scored'
        elif not remaining['subscribed']:
            reason = 'no_unscored_jobs_in_subscribed_companies'
        elif not remaining['stipend']:
            reason = 'excluded_by_job_preferences_or_seniority'
        elif profile is None:
            reason = 'role_profile_generation_required'
        elif profile['status'] != 'ready':
            reason = 'resume_profile_has_insufficient_evidence'
        else:
            reason = 'no_matching_role_family_aliases'
        reports.append(dict(user_id=user['id'],
            profile_status=profile['status'] if profile is not None else 'missing',
            remaining=remaining,
            excluded={stage: remaining[previous] - remaining[stage]
                      for previous, stage in zip(STAGES, STAGES[1:])},
            profiles_needed=remaining['stipend'] if profile is None else 0, reason=reason))
    return dict(eligible_accounts=len(users), accounts=reports,
                scoreable_jobs=sum(report['remaining']['role_family'] for report in reports))


def report_database(database):
    """Take one read snapshot; never create, migrate, or modify the database."""
    path = Path(database).resolve(strict=True)
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=5)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute('BEGIN')
        return inspect_queue(conn)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('database', help='Authoritative SQLite database path (read-only)')
    args = parser.parse_args(argv)
    try:
        report = report_database(args.database)
    except (OSError, sqlite3.Error, ValueError, KeyError, TypeError) as error:
        logger.error('Queue diagnosis failed: error=%s; no changes made', type(error).__name__)
        return 1
    print(json.dumps(report, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
