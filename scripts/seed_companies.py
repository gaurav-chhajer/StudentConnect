"""Back up an existing database, apply migrations and import companies offline."""

import logging
import sqlite3
import time
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from backend import pipeline_skeleton as pipeline
from scripts.run_local_fetch import seed_companies

logger = logging.getLogger(__name__)


def main():
    logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
    database = Path(pipeline.DB_PATH)
    try:
        if database.exists():
            directory = database.parent / 'backups'
            directory.mkdir(exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
            backup = directory / ('jobs-before-company-import-' + stamp + '.db')
            with backup.open('xb'):
                pass
            with closing(sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True, timeout=5)) as source, \
                    closing(sqlite3.connect(backup, timeout=5)) as target:
                deadline = time.monotonic() + 60

                def progress(status, remaining, total):
                    if time.monotonic() >= deadline:
                        raise RuntimeError('Backup time budget exhausted; migration stopped')

                source.backup(target, pages=100, progress=progress, sleep=0.1)
                if target.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                    raise ValueError('Backup integrity check failed; migration stopped')
            logger.info('Created a new private database backup before importing companies')
        pipeline.init_db()
        with closing(sqlite3.connect(database, timeout=5)) as conn:
            conn.execute('PRAGMA foreign_keys=ON')
            seed_companies(conn)
        logger.info('Company import finished; no jobs fetched, scoring calls or emails sent')
        return 0
    except (sqlite3.Error, OSError, ValueError, RuntimeError) as error:
        logger.error('Company import stopped (%s); inspect local configuration and backup storage', type(error).__name__)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
