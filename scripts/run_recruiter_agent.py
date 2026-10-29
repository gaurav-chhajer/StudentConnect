"""Five-minute, resumable Lever application worker for one approved workflow.

Without --run this command only previews saved state. --run authorizes search,
AI preparation, and submission of explicitly selected Lever jobs in this workflow.
"""

import argparse
import json
import logging
from logging.handlers import RotatingFileHandler
import os
import sqlite3
import threading
import time
from contextlib import closing
from pathlib import Path
from urllib.parse import urlsplit

from backend import personal_applications as work, recruiter_workflow as flow, workflow_monitor as monitor
from backend.notion_tracker import NotionTracker
from scripts import run_applications
from scripts.run_pipeline import exclusive_cycle

logger=logging.getLogger(__name__)
INTERVAL_SECONDS=300
LEVER_HOSTS=frozenset(('jobs.lever.co','jobs.eu.lever.co'))


def selected_rows(conn,profile,workflow_id):
    flow.load(conn,profile,workflow_id)
    return [row for row in flow.rows(conn,workflow_id) if row['selected']]


def cycle(conn,profile,workflow_id,tracker=None,live=False):
    """Advance saved work once; never create approvals or retry failed actions."""
    _,workflow=flow.load(conn,profile,workflow_id)
    records=selected_rows(conn,profile,workflow_id)
    if not live:
        return {'status':'preview','stage':workflow['state'],'selected':len(records)}
    with conn:
        monitor.record(conn,profile['user_id'],'agent','cycle_start',workflow_id=workflow_id)
    logger.info('Agent workflow=%s stage=%s selected=%s',workflow_id,workflow['state'],len(records))
    if workflow['state'] in ('analyzed','searching'):
        work.gemini_resume.configuration()
        if not work.pipeline.GROQ_API_KEY.strip():
            raise ValueError('Configure GROQ_API_KEY before search scoring')
        flow.search(conn,profile,workflow_id)
        path=flow.export(conn,profile,workflow_id)
        logger.info('Agent workflow=%s search complete; inspect workbook=%s and approve numbers',workflow_id,path.name)
        return {'status':'awaiting_approval'}
    if workflow['state']=='awaiting_approval':
        if not flow.rows(conn,workflow_id):
            raise ValueError('Empty search needs a new explicit search attempt; no jobs can be approved')
        logger.info('Agent workflow=%s waiting for numbered approval; no provider or submission calls',workflow_id)
        with conn:
            monitor.record(conn,profile['user_id'],'agent','approval_wait',workflow_id=workflow_id,state='waiting',event=False)
        return {'status':'awaiting_approval'}
    if workflow['state'] not in ('approved','materials_ready') or not records:
        raise ValueError('Agent requires a workflow with explicitly selected jobs')
    # Reject unsupported selections before any paid preparation, rather than silently
    # interpreting a partial selection as permission to submit a different batch.
    for row in records:
        posting=json.loads(row['posting_json'])
        url=work.browser.application_url(posting.get('apply_url') or posting['url'])
        if urlsplit(url).hostname not in LEVER_HOSTS:
            raise ValueError('This agent supports Lever only; select only Lever numbers for this workflow')
        if row['run_state'] in ('submitting','unknown','failed') or row['run_notion_state']=='unknown':
            raise ValueError('Saved application or tracker failure needs reconciliation; no automatic retry')
    if workflow['state']=='approved':
        work.gemini_resume.configuration()
        if not work.pipeline.GROQ_API_KEY.strip():
            raise ValueError('Configure GROQ_API_KEY before resume scoring')
        flow.build_materials(conn,profile,workflow_id)
        flow.export_materials(conn,profile,workflow_id)
        flow.export(conn,profile,workflow_id)
    records=selected_rows(conn,profile,workflow_id)
    applied=0
    blocked=0
    for row in records:
        if row['run_state']=='applied':
            applied+=1
            continue
        if row['run_state']!='ready':
            logger.warning('Agent workflow=%s number=%s requires review; state=%s',workflow_id,row['number'],row['run_state'] or 'no_run')
            blocked+=1
            with conn:
                monitor.record(conn,profile['user_id'],'agent','blocked',workflow_id=workflow_id,number=row['number'],state='waiting')
            continue
        material=json.loads(row['materials_json'])
        approval=flow.approved_posting(conn,profile['user_id'],row['job_id'])
        if not approval or approval['workflow_id']!=workflow_id:
            raise ValueError('A newer workflow owns this approval; no submission attempted')
        logger.info('Agent workflow=%s number=%s submitting prepared run=%s',workflow_id,row['number'],material['run_id'])
        with conn:
            monitor.record(conn,profile['user_id'],'agent','submit_start',workflow_id=workflow_id,number=row['number'])
        work.send(conn,profile,material['run_id'],tracker)
        with conn:
            monitor.record(conn,profile['user_id'],'agent','applied',workflow_id=workflow_id,number=row['number'])
        applied+=1
    if tracker:
        work.sync_notion(conn,profile,tracker)
    flow.export(conn,profile,workflow_id)
    result={'status':'needs_review' if blocked else 'complete','applied':applied,'blocked':blocked}
    with conn:
        monitor.record(conn,profile['user_id'],'agent','blocked' if blocked else 'complete',workflow_id=workflow_id,
                       state='waiting' if blocked else 'complete')
    logger.info('Agent workflow=%s status=%s applied=%s blocked=%s',workflow_id,result['status'],applied,blocked)
    return result


def serve(tick,stop,once=False,clock=time.monotonic):
    """Serialize cycles, waiting only the remaining interval; errors propagate."""
    while not stop.is_set():
        started=clock()
        result=tick()
        if once or result['status'] in ('preview','complete','needs_review'):
            return result
        logger.info('Agent waiting for next five-minute check; Ctrl+C stops it')
        stop.wait(max(0,INTERVAL_SECONDS-(clock()-started)))
    return {'status':'stopped'}


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workflow-id',type=int,required=True)
    parser.add_argument('--profile',type=Path,default=run_applications.PROFILE)
    parser.add_argument('--run',action='store_true',help='Authorize live preparation and submission of selected Lever jobs')
    parser.add_argument('--once',action='store_true',help='Perform one cycle, without waiting for later approval')
    args=parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO,format='%(levelname)s: %(message)s')
    handler=None
    try:
        if args.workflow_id<=0:
            raise ValueError('Workflow ID must be positive')
        profile=work.load_profile(args.profile)
        if not run_applications.DATABASE.is_file():
            raise ValueError('Initialize the existing local database first')
        os.environ['PLAYWRIGHT_BROWSERS_PATH']=str(work.ROOT/'data/browser-binaries')
        tracker=NotionTracker.configured() if args.run else None
        if args.run:
            log_path=work.ROOT/'data/recruiter.log'
            if not log_path.resolve().is_relative_to(work.ROOT/'data'):
                raise ValueError('Recruiter log must remain inside private data/')
            handler=RotatingFileHandler(log_path,maxBytes=2*1024*1024,backupCount=3,encoding='utf-8')
            handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(name)s: %(message)s'))
            logging.getLogger().addHandler(handler)

        def tick():
            mode='rw' if args.run else 'ro'
            with closing(sqlite3.connect(run_applications.DATABASE.resolve().as_uri()+'?mode='+mode,uri=True,timeout=5)) as conn:
                conn.row_factory=sqlite3.Row
                conn.execute('PRAGMA foreign_keys=ON')
                if args.run:
                    with exclusive_cycle(run_applications.DATABASE.parent/'applications.lock'):
                        try:
                            result=cycle(conn,profile,args.workflow_id,tracker,live=True)
                        except BaseException as error:
                            if monitor.available(conn):
                                with conn:
                                    monitor.record(conn,profile['user_id'],'agent','stopped' if isinstance(error,KeyboardInterrupt)
                                                   else monitor.error_code(error),workflow_id=args.workflow_id,
                                                   state='stopped' if isinstance(error,KeyboardInterrupt) else 'error')
                            raise
                        if result['status']=='awaiting_approval' and not args.once:
                            from datetime import datetime,timedelta,timezone
                            with conn:
                                monitor.record(conn,profile['user_id'],'agent','approval_wait',workflow_id=args.workflow_id,
                                    state='waiting',next_check=(datetime.now(timezone.utc)+timedelta(seconds=INTERVAL_SECONDS)).isoformat(),event=False)
                        return result
                return cycle(conn,profile,args.workflow_id)

        if args.run:
            # Separate lifetime lock prevents two agents from taking turns on the
            # same queue. The CLI approval lock is held only during actual cycles.
            with exclusive_cycle(run_applications.DATABASE.parent/'recruiter-agent.lock'):
                result=serve(tick,threading.Event(),args.once)
        else:
            result=tick()
        logger.info('Agent result=%s',json.dumps(result))
        return 2 if result['status']=='needs_review' else 0
    except KeyboardInterrupt:
        logger.warning('Agent interrupted; saved submitting/unknown states must be reconciled before restart')
        return 130
    except Exception as error:
        # Provider/browser exception strings can contain secrets or personal data.
        logger.error('Agent stopped error_type=%s; inspect saved stage and recruiter log; no automatic retry',type(error).__name__)
        if type(error) is ValueError and str(error).startswith(('Configure ','Empty search ','This agent supports ',
                'Saved application ','A newer workflow ','Agent requires ','Workflow ID ','Initialize ')):
            logger.error('%s',error)
        return 1
    finally:
        if handler is not None:
            logging.getLogger().removeHandler(handler)
            handler.close()


if __name__=='__main__':
    raise SystemExit(main())
