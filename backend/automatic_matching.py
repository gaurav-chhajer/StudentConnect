"""Process-owned matching service; durable tasks survive web process restarts."""

import logging
import sqlite3
import threading
import time
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from backend import matching_tasks, pipeline_skeleton as pipeline
from scripts import run_pipeline

logger = logging.getLogger(__name__)
POLL_SECONDS = 30
REFRESH_SECONDS = 900
_services = {}
_services_lock = threading.Lock()


class MatchingService:
    def __init__(self, database):
        self.database = Path(database).resolve()
        self.wake = threading.Event()
        self.stop = threading.Event()
        self.next_refresh = 0
        self.thread = threading.Thread(target=self.run, name="automatic-matching", daemon=True)

    def cycle(self):
        # Use the same OS lock as CLI workers for refresh scheduling, too.
        if time.monotonic() >= self.next_refresh:
            with run_pipeline.exclusive_cycle(self.database.parent / "pipeline.lock"):
                with closing(sqlite3.connect(self.database.as_uri() + "?mode=rw", uri=True, timeout=5)) as conn:
                    conn.row_factory = sqlite3.Row
                    conn.execute("PRAGMA foreign_keys=ON")
                    with conn:
                        users = conn.execute("""SELECT u.id FROM users u JOIN accounts a ON a.user_id=u.id
                            WHERE u.active=1 AND a.email_verified=1 AND trim(u.resume_text)<>''""").fetchall()
                        for user in users:
                            matching_tasks.enqueue(conn, user[0], 'postings', datetime.now(timezone.utc))
            self.next_refresh = time.monotonic() + REFRESH_SECONDS
        return run_pipeline.main(['--run', '--matching-only'], database_path=self.database)

    def run(self):
        while not self.stop.is_set():
            self.wake.clear()
            try:
                self.cycle()
            except (sqlite3.Error, OSError, RuntimeError, ValueError) as error:
                logger.error("Automatic matching cycle stopped (%s); retrying later", type(error).__name__)
            # Continue budget-limited and failed work without another web request.
            self.wake.wait(POLL_SECONDS)


def ensure_started(database, notify=False):
    if not pipeline.GROQ_API_KEY.strip() or not Path(database).is_file():
        return
    key = str(Path(database).resolve())
    with _services_lock:
        service = _services.get(key)
        if service is None or not service.thread.is_alive():
            service = MatchingService(database)
            _services[key] = service
            service.thread.start()
        if notify:
            service.wake.set()
