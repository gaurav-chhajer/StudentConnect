"""Preview saved match counts, or explicitly send a digest through Gmail SMTP."""

import argparse
import logging
import sqlite3
import re
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

from backend import pipeline_skeleton as pipeline
from backend.store import settings

logger = logging.getLogger(__name__)


def schedule_due(conn, user_id, now):
    """Daily local-date cadence, or seven local dates between weekly digests."""
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("Scheduled delivery requires a timezone-aware clock")
    preferences = settings(conn, user_id)
    cadence = preferences["cadence"]
    if cadence == "off":
        return False
    if cadence not in ("daily", "weekly"):
        raise ValueError("Invalid email cadence")
    # Settings currently supports only these fixed-offset zones. Avoid requiring
    # a new timezone-data package on Windows for an unsupported zone.
    zones = {"UTC": timezone.utc, "Asia/Kolkata": timezone(timedelta(hours=5, minutes=30))}
    zone = zones.get(preferences["timezone"])
    send_time = preferences["send_time"]
    if zone is None or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", send_time):
        raise ValueError("Invalid saved email timezone or send time")
    local = now.astimezone(zone)
    if local.strftime("%H:%M") < send_time:
        return False
    last = conn.execute("SELECT MAX(sent_at) FROM sent_log WHERE user_id=? AND channel='email'",
                        (user_id,)).fetchone()[0]
    if not last:
        return True
    previous = datetime.fromisoformat(last)
    if previous.tzinfo is None or previous.utcoffset() is None:
        raise ValueError("Saved email timestamp must include its timezone")
    elapsed_dates = (local.date() - previous.astimezone(zone).date()).days
    return elapsed_dates >= (7 if cadence == "weekly" else 1)


def send_digest(conn, send=False, scheduled=False, now=None):
    """Send at most five matches per verified active account; preview by default."""
    # Serialize selection/send/logging across local runs. The network wait is
    # bounded by delivery's timeout; this deliberately holds a SQLite write lock.
    user_ids = conn.execute("SELECT u.id FROM users u JOIN accounts a ON a.user_id=u.id WHERE u.active=1 AND a.email_verified=1 ORDER BY u.id").fetchall()
    if not user_ids:
        raise ValueError("Create a verified account before sending job digests")
    total = 0
    for selected in user_ids:
        with conn:
            if send:
                conn.execute("BEGIN IMMEDIATE")
            # Recheck eligibility inside the send transaction.
            user = conn.execute("SELECT u.* FROM users u JOIN accounts a ON a.user_id=u.id WHERE u.id=? AND u.active=1 AND a.email_verified=1",(selected[0],)).fetchone()
            if user is None:
                logger.info("Account no longer eligible (user_id=%s); skipped",selected[0])
                continue
            if scheduled and not schedule_due(conn,user["id"],now or datetime.now(timezone.utc)):
                logger.info("Email schedule is not due (user_id=%s); skipped",user["id"])
                continue
            matches = pipeline.get_unsent_top_matches(conn,user["id"])
            if not matches:
                logger.info("No eligible unsent matches (user_id=%s); email skipped",user["id"])
                continue
            pipeline._render_email_html(user,matches)
            pipeline._render_email_text(user,matches)
            if not send:
                logger.info("Preview: user_id=%s has %d match(es) ready; no email sent",user["id"],len(matches))
            else:
                pipeline.deliver_email(user,matches)
                pipeline.mark_sent(conn,user["id"],[match["job_id"] for match in matches])
                logger.info("Gmail SMTP accepted digest (user_id=%s) containing %d match(es)",user["id"],len(matches))
            total += len(matches)
    return total


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--send", action="store_true", help="Send a real email; default only previews counts")
    parser.add_argument("--scheduled", action="store_true", help="Respect saved cadence, time, and timezone")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    pause_path = Path(pipeline.DB_PATH).parent / "email_schedule.paused"
    claimed = False
    try:
        if args.scheduled and args.send:
            # Exclusive marker prevents overlapping automatic calls. Leave it
            # after errors or crashes so an uncertain send is never auto-retried.
            try:
                with pause_path.open("x", encoding="utf-8") as marker:
                    marker.write("Scheduled email attempt in progress or requires provider review.\n")
                claimed = True
            except FileExistsError:
                logger.error("Scheduled mail is paused or another run is active; review provider status before clearing the pause marker")
                return 1
        # Never create or migrate a database as a side effect of previewing mail.
        with closing(sqlite3.connect(Path(pipeline.DB_PATH).as_uri() + "?mode=rw", uri=True, timeout=5)) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys=ON")
            send_digest(conn, send=args.send, scheduled=args.scheduled)
        if claimed:
            pause_path.unlink()
    except (ValueError, RuntimeError, OSError, sqlite3.Error) as error:
        logger.error("Email command failed (%s); inspect configuration and provider status before retrying", type(error).__name__)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
