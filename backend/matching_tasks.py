"""Coalesced matching tasks; callers own transactions and worker ownership."""

from datetime import timezone

MAX_TASKS_PER_CYCLE = 100
REASONS = {'companies', 'preferences', 'resume', 'postings', 'backlog'}
STATES = {'pending', 'running', 'complete', 'failed'}


def enqueue(conn, user_id, reason, now):
    if type(user_id) is not int or user_id <= 0 or reason not in REASONS:
        raise ValueError("Invalid matching task")
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("Matching task timestamps must be timezone-aware")
    conn.execute("""INSERT INTO matching_tasks(user_id,reason,updated_at) VALUES(?,?,?)
        ON CONFLICT(user_id) DO UPDATE SET revision=matching_tasks.revision+1,
        reason=excluded.reason,status='pending',last_error=NULL,updated_at=excluded.updated_at""",
        (user_id, reason, now.astimezone(timezone.utc).isoformat()))


def companies_for_user(conn, user_id):
    return {row[0] for row in conn.execute("""SELECT id FROM companies c WHERE active=1
        AND (NOT EXISTS(SELECT 1 FROM user_settings WHERE user_id=? AND subscriptions_configured=1)
             OR EXISTS(SELECT 1 FROM company_subscriptions WHERE user_id=? AND company_id=c.id))""",
        (user_id, user_id))}


def enqueue_company(conn, company_id, now):
    users = conn.execute("""SELECT u.id FROM users u JOIN accounts a ON a.user_id=u.id
        WHERE u.active=1 AND a.email_verified=1
        AND (NOT EXISTS(SELECT 1 FROM user_settings WHERE user_id=u.id AND subscriptions_configured=1)
             OR EXISTS(SELECT 1 FROM company_subscriptions WHERE user_id=u.id AND company_id=?))""",
        (company_id,)).fetchall()
    for user in users:
        enqueue(conn, user[0], 'postings', now)


def ready(conn):
    return conn.execute("""SELECT t.* FROM matching_tasks t JOIN users u ON u.id=t.user_id
        JOIN accounts a ON a.user_id=u.id WHERE t.status IN ('pending','running','failed')
        AND u.active=1 AND a.email_verified=1 AND trim(u.resume_text)<>''
        ORDER BY t.updated_at,t.user_id LIMIT ?""", (MAX_TASKS_PER_CYCLE,)).fetchall()


def claim(conn):
    # The pipeline's OS lock must be held. 'running' includes work abandoned
    # after a crash; revisions prevent completion from erasing a newer edit.
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        tasks = ready(conn)
        for task in tasks:
            conn.execute("UPDATE matching_tasks SET status='running',last_error=NULL WHERE user_id=? AND revision=?",
                         (task['user_id'], task['revision']))
    return tasks


def finish(conn, task, state, now, error=None):
    if state not in STATES or error not in (None, 'fetch_failed', 'scoring_failed'):
        raise ValueError("Invalid matching task result")
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("Matching task timestamps must be timezone-aware")
    with conn:
        conn.execute("""UPDATE matching_tasks SET status=?,last_error=?,updated_at=?
            WHERE user_id=? AND revision=? AND status='running'""",
            (state, error, now.astimezone(timezone.utc).isoformat(), task['user_id'], task['revision']))


def status(conn, user_id):
    task = conn.execute("SELECT status,last_error FROM matching_tasks WHERE user_id=?", (user_id,)).fetchone()
    return dict(task) if task else None
