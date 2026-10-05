"""Preview saved match counts, or explicitly send a digest through Resend."""

import argparse
import logging
import sqlite3
import re
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from backend import pipeline_skeleton as pipeline
from backend.store import profile, settings

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
    """Send at most five matches for the sole active profile; preview by default."""
    # Serialize selection/send/logging across local runs. The network wait is
    # bounded by delivery's timeout; this deliberately holds a SQLite write lock.
    with conn:
        if send:
            conn.execute("BEGIN IMMEDIATE")
        user = profile(conn)
        if not user:
            raise ValueError("Create an active profile in Settings first")
        if scheduled and not schedule_due(conn, user["id"], now or datetime.now(timezone.utc)):
            logger.info("Email schedule is not due; skipped")
            return 0
        matches = pipeline.get_unsent_top_matches(conn, user["id"])
        if not matches:
            logger.info("No eligible unsent matches; email skipped")
            return 0
        # Validate both templates during preview without printing personal data.
        pipeline._render_email_html(user, matches)
        pipeline._render_email_text(user, matches)
        if not send:
            logger.info("Preview: %d match(es) ready; no email sent", len(matches))
            return len(matches)
        pipeline.deliver_email(user, matches)
        pipeline.mark_sent(conn, user["id"], [match["job_id"] for match in matches])
        logger.info("Resend accepted one digest containing %d match(es)", len(matches))
        return len(matches)


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
    except (ValueError, RuntimeError, OSError, sqlite3.Error, requests.RequestException) as error:
        logger.error("Email command failed (%s); inspect configuration and provider status before retrying", type(error).__name__)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
