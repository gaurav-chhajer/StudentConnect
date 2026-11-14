"""Student-owned application outcomes and durable first strong-match cohorts."""

from datetime import datetime, timedelta, timezone
from statistics import median

IST = timezone(timedelta(hours=5, minutes=30))


def snapshot(conn, user_id, now, weeks=8):
    """Aggregate in SQLite; load only bounded weekly buckets and median rows."""
    stamp = now.isoformat()
    cutoff = (now - timedelta(days=14)).isoformat()
    outcomes = """WITH outcomes AS (
        SELECT a.id,a.applied_at,
          EXISTS(SELECT 1 FROM application_events e WHERE e.application_id=a.id
                 AND e.status IN ('screen','assessment','interview','offer','rejected')) replied,
          EXISTS(SELECT 1 FROM application_events e WHERE e.application_id=a.id
                 AND e.status IN ('interview','offer')) interviewed,
          (SELECT MIN(julianday(e.at)) FROM application_events e WHERE e.application_id=a.id
             AND e.status IN ('screen','assessment','interview','offer','rejected') AND e.date_known=1
             AND julianday(e.at)>=julianday(a.applied_at)
             AND NOT EXISTS(SELECT 1 FROM application_events old WHERE old.application_id=a.id
                 AND old.date_known=0 AND old.status IN ('screen','assessment','interview','offer','rejected'))) first_reply
        FROM applications a WHERE a.user_id=? AND a.applied_at IS NOT NULL
          AND julianday(a.applied_at)<=julianday(?)
    ), eligible AS (SELECT * FROM outcomes WHERE replied=1 OR julianday(applied_at)<=julianday(?)) """
    parameters = (user_id, stamp, cutoff)
    counts = dict(conn.execute(outcomes + """SELECT COUNT(*) eligible,COALESCE(SUM(replied),0) responses,
        COALESCE(SUM(interviewed),0) interviews FROM eligible""", parameters).fetchone())
    # SQLite orders the delays and selects only the middle one/two rows.
    delays = conn.execute(outcomes + """, delays AS (
        SELECT first_reply-julianday(applied_at) days FROM eligible WHERE first_reply IS NOT NULL
    ) SELECT days FROM delays ORDER BY days LIMIT 2-(SELECT COUNT(*) FROM delays)%2
        OFFSET MAX(0,((SELECT COUNT(*) FROM delays)-1)/2)""", parameters).fetchall()
    counts['median_days'] = round(median(row['days'] for row in delays), 1) if delays else None
    counts['response_rate'] = round(100 * counts['responses'] / counts['eligible'], 1) if counts['eligible'] else None
    counts['interview_rate'] = round(100 * counts['interviews'] / counts['eligible'], 1) if counts['eligible'] else None
    monday = now.astimezone(IST).date() - timedelta(days=now.astimezone(IST).weekday())
    start = monday - timedelta(weeks=weeks - 1)
    buckets = {str(start + timedelta(weeks=index)): dict(week=str(start + timedelta(weeks=index)), count=0, applied=0,
               current=index == weeks-1) for index in range(weeks)}
    rows = conn.execute("""SELECT date(match_first_at,'+330 minutes','weekday 0','-6 days') week,COUNT(*) count,
        SUM(EXISTS(SELECT 1 FROM applications a WHERE a.user_id=f.user_id AND a.job_id=f.job_id
                   AND a.applied_at IS NOT NULL)) applied
        FROM strong_match_firsts f WHERE f.user_id=? AND julianday(match_first_at)>=julianday(?)
          AND julianday(match_first_at)<=julianday(?) GROUP BY week""",
        (user_id, datetime.combine(start, datetime.min.time(), IST).isoformat(), stamp)).fetchall()
    for row in rows:
        if row['week'] in buckets:
            buckets[row['week']].update(count=row['count'], applied=row['applied'])
    legacy = conn.execute('SELECT COUNT(*) FROM strong_match_firsts WHERE user_id=? AND historical=1', (user_id,)).fetchone()[0]
    return dict(outcomes=counts, strong_weeks=list(buckets.values()), historical_strong=legacy)
