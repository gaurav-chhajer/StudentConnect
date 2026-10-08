"""Process-owned matching service; durable tasks survive web process restarts."""

import logging
import sqlite3
import threading
import time
from contextlib import closing
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path

from backend import matching_tasks, pipeline_skeleton as pipeline
from scripts import run_pipeline

logger = logging.getLogger(__name__)
POLL_SECONDS = 30
REFRESH_SECONDS = 900
_services = {}
_services_lock = threading.Lock()
_logging_lock = threading.Lock()


def configure_logging(database):
    """Keep service and pipeline progress visible even under WSGI logging."""
    log_path = str(Path(database).resolve().parent / "matching.log")
    with _logging_lock:
        for target in (logger, run_pipeline.logger):
            target.setLevel(logging.INFO)
            if any(isinstance(handler, RotatingFileHandler) and handler.baseFilename == log_path
                   for handler in target.handlers):
                continue
            handler = RotatingFileHandler(log_path, maxBytes=2 * 1024 * 1024,
                                          backupCount=3, encoding="utf-8")
            handler.setLevel(logging.INFO)
            handler.setFormatter(logging.Formatter(
                "%(asctime)s %(levelname)s %(name)s: %(message)s"))
            target.addHandler(handler)


class MatchingService:
    def __init__(self, database):
        self.database = Path(database).resolve()
        configure_logging(self.database)
        self.wake = threading.Event()
        self.stop = threading.Event()
        self.next_refresh = 0
        self.thread = threading.Thread(target=self.run, name="automatic-matching", daemon=True)

    def cycle(self):
        logger.info("Automatic matching cycle started; database=%s", self.database)
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
        result = run_pipeline.main(['--run', '--matching-only'], database_path=self.database)
        logger.info("Automatic matching cycle finished; result_code=%s", result)
        return result

    def run(self):
        while not self.stop.is_set():
            self.wake.clear()
            try:
                self.cycle()
            except Exception as error:
                logger.exception("Automatic matching cycle stopped (%s): %.200s; retrying later",
                                 type(error).__name__, str(error))
            # Continue budget-limited and failed work without another web request.
            self.wake.wait(POLL_SECONDS)


def ensure_started(database, notify=False):
    # The external worker owns hosted matching when its credential is configured.
    # PythonAnywhere web workers cannot own a durable background thread.
    from backend.cloud_worker import worker_key
    if worker_key():
        return
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
