"""Bounded no-alert reminders; the hosted email transaction owns delivery/state."""

import hashlib
import json
from datetime import datetime, timedelta, timezone

from backend import hosting, pipeline_skeleton as pipeline, store

FIRST_REMINDER_DELAY = timedelta(days=7)
FOLLOWUP_DELAY = timedelta(days=14)
MAX_REMINDERS = 2
MAX_SUBSCRIPTIONS = 1000


def timestamp(value):
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Reminder timestamps must include a timezone")
    return parsed.astimezone(timezone.utc)


def due(conn, user_id, now):
    """Prepare/reset state and return eligibility; never commit or send here."""
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("Reminder clock must include a timezone")
    now = now.astimezone(timezone.utc)
    user = conn.execute("""SELECT u.*,a.created_at FROM users u JOIN accounts a ON a.user_id=u.id
        WHERE u.id=? AND u.active=1 AND a.email_verified=1""", (user_id,)).fetchone()
    if user is None:
        return False
    preferences = store.settings(conn, user_id)
    if preferences['cadence'] == 'off':
        return False
    if preferences['cadence'] not in ('daily', 'weekly'):
        raise ValueError("Invalid reminder cadence")
    subscriptions = [row[0] for row in conn.execute("""SELECT company_id FROM company_subscriptions
        WHERE user_id=? ORDER BY company_id LIMIT ?""", (user_id, MAX_SUBSCRIPTIONS + 1))]
    if len(subscriptions) > MAX_SUBSCRIPTIONS:
        raise ValueError("Too many reminder subscriptions")
    profile = dict(resume=user['resume_text'], preferences=user['preferences_json'],
                   minimum=preferences['min_score'], subscriptions=subscriptions,
                   configured=preferences['subscriptions_configured'])
    digest = hashlib.sha256(json.dumps(profile, sort_keys=True).encode()).hexdigest()
    last = conn.execute("SELECT MAX(sent_at) FROM sent_log WHERE user_id=? AND channel='email'",
                        (user_id,)).fetchone()[0]
    anchor = timestamp(last or user['created_at'])
    if anchor > now:
        return False
    state = conn.execute('SELECT * FROM match_reminders WHERE user_id=?', (user_id,)).fetchone()
    if state is None:
        conn.execute("""INSERT INTO match_reminders(user_id,profile_fingerprint,anchor_at,last_job_email_at)
            VALUES(?,?,?,?)""", (user_id, digest, anchor.isoformat(), last))
    elif state['profile_fingerprint'] != digest or state['last_job_email_at'] != last:
        # Profile changes start a new seven-day window when first observed.
        if state['profile_fingerprint'] != digest:
            anchor = now
        conn.execute("""UPDATE match_reminders SET profile_fingerprint=?,anchor_at=?,last_job_email_at=?,
            last_reminder_at=NULL,sent_count=0 WHERE user_id=?""", (digest, anchor.isoformat(), last, user_id))
    state = conn.execute('SELECT * FROM match_reminders WHERE user_id=?', (user_id,)).fetchone()
    task = conn.execute('SELECT status FROM matching_tasks WHERE user_id=?', (user_id,)).fetchone()
    if task is not None and task['status'] != 'complete':
        return False
    if user['resume_text'].strip() and pipeline.filter_candidate_jobs(conn, user):
        return False
    if pipeline.get_unsent_top_matches(conn, user_id):
        return False
    if state['sent_count'] >= MAX_REMINDERS:
        return False
    reference = state['last_reminder_at'] if state['sent_count'] else state['anchor_at']
    if reference is None:
        raise ValueError("Reminder state is missing its delivery timestamp")
    elapsed = now - timestamp(reference)
    return elapsed >= (FOLLOWUP_DELAY if state['sent_count'] else FIRST_REMINDER_DELAY)


def deliver(user):
    """Send a helpful review prompt, without claiming why matches are absent."""
    origin = hosting.site_origin()
    if not origin.startswith('https://'):
        raise ValueError("Hosted reminders require an HTTPS website")
    text = (
        "Hi there,\n\nNo new job alerts have been sent to you recently. "
        "Visit StudentConnect to review your matches, refresh your resume, or consider "
        "broadening your company, role, location, or minimum fit-score preferences.\n\n"
        "Your preferences will only change when you update them.\n\n"
        f"Review my preferences: {origin}/settings\n\n"
        "To stop job alerts and these reminders, turn emails Off in Settings."
    )
    pipeline.deliver_account_email(user['email'], 'Still looking for your next opportunity?', text)


def mark_sent(conn, user_id, now):
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("Reminder delivery timestamp must include a timezone")
    updated = conn.execute("""UPDATE match_reminders SET sent_count=sent_count+1,last_reminder_at=?
        WHERE user_id=? AND sent_count<?""", (now.astimezone(timezone.utc).isoformat(), user_id, MAX_REMINDERS))
    if updated.rowcount != 1:
        raise RuntimeError("Reminder delivery state was not updated")
