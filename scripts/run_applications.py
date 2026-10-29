"""Personal local browser worker. Preview is offline; --run authorizes live work."""

import argparse
import logging
import os
import sqlite3
import time
import uuid
from contextlib import closing
from pathlib import Path

import requests

from backend import application_browser as browser, personal_applications as work
from backend.migrate import apply_migrations
from backend.notion_tracker import NotionTracker
from scripts.run_pipeline import exclusive_cycle

logger = logging.getLogger(__name__)
DATABASE = work.ROOT / 'data/jobs.db'
PROFILE = work.ROOT / 'data/application-profile.json'


def initialize(database):
    """Back up the existing database; never bootstrap an empty account database."""
    database = database.resolve()
    if not database.is_relative_to(work.ROOT / 'data'):
        raise ValueError('Application database must stay inside private repository data/')
    if not database.is_file():
        raise ValueError('Set up StudentConnect and your account before the application worker')
    backups = database.parent / 'backups'
    backups.mkdir(exist_ok=True)
    destination = backups / ('jobs-before-applications-' + uuid.uuid4().hex + '.db')
    if not backups.resolve().is_relative_to(work.ROOT / 'data'):
        raise ValueError('Application backups must stay inside private repository data/')
    destination.touch(exist_ok=False)
    deadline = time.monotonic() + 60
    def backup_progress(status, remaining, total):
        if time.monotonic() > deadline:
            raise TimeoutError('Database backup exceeded 60 seconds; migration withheld')
    with closing(sqlite3.connect(database.as_uri() + '?mode=rw',uri=True,timeout=5)) as conn:
        with closing(sqlite3.connect(destination,timeout=5)) as backup:
            conn.backup(backup,pages=100,progress=backup_progress)
            if backup.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise RuntimeError('Database backup did not pass its integrity check')
        apply_migrations(conn)
    logger.info('Created a private database backup and applied additive migrations')


def show_status(conn, user_id):
    after = 0
    while True:
        rows = conn.execute('''SELECT id,job_id,state,original_score,final_score,target_score,notion_state,detail
            FROM application_runs WHERE user_id=? AND id>? ORDER BY id LIMIT ?''',
            (user_id,after,work.PAGE_SIZE)).fetchall()
        if not rows:
            break
        for row in rows:
            logger.info('run=%s job=%s state=%s original=%s final=%s target=%s notion=%s detail=%s',
                row['id'],row['job_id'],row['state'],row['original_score'],row['final_score'],
                row['target_score'],row['notion_state'],row['detail'])
        after = rows[-1]['id']


def cycle(conn, profile, tracker, live=False, prepare_only=False, job_id=None):
    work.user_for_profile(conn,profile)
    if live and not work.pipeline.GROQ_API_KEY.strip():
        raise ValueError('Configure GROQ_API_KEY before live preparation')
    failed = False
    if live and not prepare_only and job_id is None:
        ready_after = 0
        while True:
            ready = conn.execute("""SELECT id FROM application_runs WHERE user_id=? AND state='ready'
                AND id>? ORDER BY id LIMIT ?""",(profile['user_id'],ready_after,work.PAGE_SIZE)).fetchall()
            if not ready:
                break
            for row in ready:
                try:
                    work.send(conn,profile,row['id'],tracker)
                except browser.NeedsReview as error:
                    logger.warning('Run=%s needs review: %s',row['id'],str(error))
            ready_after = ready[-1]['id']
    after = 0
    while True:
        rows = [{'id':job_id}] if job_id else work.candidates(conn,profile['user_id'],after)
        if not rows:
            break
        for item in rows:
            try:
                if not live:
                    user = work.user_for_profile(conn,profile)
                    job, _ = work.job_inputs(conn,user,item['id'])
                    if work.already_tracked(conn,user['id'],job):
                        raise browser.NeedsReview('Existing application history blocks this posting')
                    logger.info('Preview job=%s score=%s target=%s; no external calls',
                                item['id'],job['fit_score'],90 if job['fit_score'] >= 80 else 80)
                    continue
                run_id = work.prepare(conn,profile,item['id'],tracker)
                state = conn.execute('SELECT state FROM application_runs WHERE id=?',(run_id,)).fetchone()[0]
                if state == 'ready' and not prepare_only:
                    work.send(conn,profile,run_id,tracker)
                logger.info('Processed application run=%s',run_id)
            except browser.NeedsReview as error:
                logger.warning('Job=%s needs review: %s',item['id'],str(error))
            except requests.RequestException as error:
                logger.error('Provider operation failed (%s); stopping cycle without retries',type(error).__name__)
                return 1
            except (ValueError,RuntimeError,KeyError,TypeError) as error:
                logger.error('Job=%s stopped (%s); inspect local status; no automatic retry',item['id'],type(error).__name__)
                failed = True
        if job_id:
            break
        after = rows[-1]['id']
    return 1 if failed else 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=('init','preview','prepare','export','submit','run','status','resume','sync-notion'),nargs='?',default='preview')
    parser.add_argument('--profile',type=Path,default=PROFILE)
    parser.add_argument('--job-id',type=int)
    parser.add_argument('--run-id',type=int)
    parser.add_argument('--run',action='store_true',help='Authorize provider/browser/Notion operations')
    parser.add_argument('--watch',action='store_true',help='Check new eligible jobs every 60 seconds, until Ctrl+C')
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO,format='%(levelname)s: %(message)s')
    os.environ['PLAYWRIGHT_BROWSERS_PATH'] = str(work.ROOT / 'data/browser-binaries')
    try:
        if args.watch and (args.command != 'run' or not args.run):
            raise ValueError('--watch requires run --run')
        if any(value is not None and value <= 0 for value in (args.job_id,args.run_id)):
            raise ValueError('IDs must be positive')
        if args.command == 'init':
            if not args.run:
                raise ValueError('Use init --run to authorize backup and additive migration')
            with exclusive_cycle(DATABASE.parent / 'pipeline.lock'):
                initialize(DATABASE.resolve())
            return 0
        profile = work.load_profile(args.profile)
        if not DATABASE.is_file():
            raise ValueError('Local StudentConnect database is missing')
        # Separate OS lock gives this personal worker single ownership across commands.
        with exclusive_cycle(DATABASE.parent / 'applications.lock'), closing(sqlite3.connect(
                DATABASE.resolve().as_uri() + '?mode=rw',uri=True,timeout=5)) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute('PRAGMA foreign_keys=ON')
            work.user_for_profile(conn,profile)
            if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='application_runs'").fetchone() is None:
                raise ValueError('Run init --run after reviewing the migration')
            if args.command == 'status':
                show_status(conn,profile['user_id'])
                return 0
            if args.command == 'preview':
                return cycle(conn,profile,None,job_id=args.job_id)
            if args.command == 'export':
                if not args.run_id:
                    raise ValueError('export requires --run-id')
                path = work.export_document(conn,profile['user_id'],args.run_id)
                logger.info('Exported exact upload document: data/application-documents/%s',path.name)
                return 0
            if not args.run:
                raise ValueError('Live operations require --run; preview makes no external calls')
            tracker = NotionTracker.configured()
            if not tracker and not profile['history_checked']:
                raise ValueError('Reconcile existing Notion history locally or configure a Notion connection')
            if args.command == 'sync-notion':
                logger.info('Synced %s confirmed application records',work.sync_notion(conn,profile,tracker))
                return 0
            if args.command == 'resume':
                if not args.run_id:
                    raise ValueError('resume requires --run-id')
                with conn:
                    changed = conn.execute("""UPDATE application_runs SET state='ready',detail='Explicitly resumed by owner',
                        updated_at=? WHERE id=? AND user_id=? AND state IN ('failed','needs_review')
                        AND final_score>=80 AND document IS NOT NULL""",
                        (work.now(),args.run_id,profile['user_id'])).rowcount
                    if changed != 1:
                        raise ValueError('Only pre-submission pauses with a validated 80+ document can resume')
                return 0
            if args.command == 'submit':
                if not args.run_id:
                    raise ValueError('submit requires --run-id')
                work.send(conn,profile,args.run_id,tracker)
                return 0
            while True:
                # Reload the private profile between cycles so owner changes take effect.
                current = work.load_profile(args.profile)
                if current['user_id'] != profile['user_id'] or current['email'] != profile['email']:
                    raise ValueError('Personal account changed; restart the worker explicitly')
                result = cycle(conn,current,tracker,live=True,prepare_only=args.command == 'prepare',job_id=args.job_id)
                if tracker:
                    work.sync_notion(conn,current,tracker)
                if not args.watch or result:
                    return result
                logger.info('Cycle finished; no daily cap. Checking new jobs in 60 seconds. Ctrl+C stops.')
                time.sleep(60)
    except KeyboardInterrupt:
        logger.warning('Stopped. Any submitting/unknown record must be reconciled before another attempt.')
        return 130
    except Exception as error:
        # Do not expose provider responses, browser errors, answers or credentials.
        logger.error('Application worker stopped (%s); check configuration and application status',type(error).__name__)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
