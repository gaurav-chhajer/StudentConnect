"""Six-stage local recruiter workflow. Analysis/search stop before job approval."""

import argparse
import json
import logging
from logging.handlers import RotatingFileHandler
import os
import sqlite3
from contextlib import closing
from pathlib import Path

from backend import personal_applications as work, recruiter_workflow as flow
from backend.notion_tracker import NotionTracker
from scripts import run_applications
from scripts.run_pipeline import exclusive_cycle

logger=logging.getLogger(__name__)


def number_list(value):
    try:
        numbers=[int(part.strip()) for part in value.split(',')]
    except (ValueError,AttributeError) as error:
        raise argparse.ArgumentTypeError('Use comma-separated spreadsheet numbers') from error
    if not numbers or len(numbers)>1000 or len(set(numbers))!=len(numbers) or any(number<=0 for number in numbers):
        raise argparse.ArgumentTypeError('Choose distinct positive spreadsheet numbers')
    return numbers


def summary(conn,profile,workflow_id):
    _,workflow=flow.load(conn,profile,workflow_id)
    logger.info('Workflow=%s stage=%s',workflow_id,workflow['state'])
    shown=0
    for row in flow.rows(conn,workflow_id):
        facts=json.loads(row['facts_json'])
        if facts['search_status']!='shortlisted' or shown>=10:
            continue
        posting=json.loads(row['posting_json'])
        score=json.loads(row['evaluation_json']).get('fit_score')
        logger.info('#%s %s — %s; original score=%s pay=%s; %s',row['number'],posting['company_name'],
                    posting['title'],score,facts['salary_status'],posting.get('apply_url') or posting['url'])
        shown+=1
    for site,coverage in json.loads(workflow['coverage_json']).items():
        logger.info('Source=%s status=%s verified=%s limits=%s',site,coverage['status'],
                    coverage.get('verified',0),coverage.get('limits',''))


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=('init','analyze','search','export','status','approve','review','materials','outreach','schedule','followups','submit','sync-notion'))
    parser.add_argument('--profile',type=Path,default=run_applications.PROFILE)
    parser.add_argument('--workflow-id',type=int)
    parser.add_argument('--numbers',type=number_list)
    parser.add_argument('--application-id',type=int)
    parser.add_argument('--date',help='Explicit local follow-up date, YYYY-MM-DD')
    parser.add_argument('--run',action='store_true',help='Authorize this command\'s live provider/browser/Notion operations')
    args=parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO,format='%(levelname)s: %(message)s')
    os.environ['PLAYWRIGHT_BROWSERS_PATH']=str(work.ROOT/'data/browser-binaries')
    file_handler=None
    try:
        log_path=work.ROOT/'data/recruiter.log'
        if not log_path.resolve().is_relative_to(work.ROOT/'data'):
            raise ValueError('Recruiter log must remain inside private data/')
        file_handler=RotatingFileHandler(log_path,maxBytes=2*1024*1024,backupCount=3,encoding='utf-8')
        file_handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(name)s: %(message)s'))
        logging.getLogger().addHandler(file_handler)
        logger.info('Starting recruiter command=%s workflow=%s; progress log=data/recruiter.log',args.command,args.workflow_id)
        if args.workflow_id is not None and args.workflow_id<=0:
            raise ValueError('Workflow ID must be positive')
        if args.command=='init':
            if not args.run:
                raise ValueError('init --run authorizes backup and additive migration')
            with exclusive_cycle(run_applications.DATABASE.parent/'pipeline.lock'):
                run_applications.initialize(run_applications.DATABASE)
            return 0
        profile=work.load_profile(args.profile)
        if not run_applications.DATABASE.is_file():
            raise ValueError('Local StudentConnect database is missing')
        with exclusive_cycle(run_applications.DATABASE.parent/'applications.lock'),closing(sqlite3.connect(
                run_applications.DATABASE.resolve().as_uri()+'?mode=rw',uri=True,timeout=5)) as conn:
            conn.row_factory=sqlite3.Row
            conn.execute('PRAGMA foreign_keys=ON')
            work.user_for_profile(conn,profile)
            if conn.execute("SELECT 1 FROM sqlite_master WHERE name='recruiter_workflows'").fetchone() is None:
                raise ValueError('Run init --run before the recruiter workflow')
            offline=args.command in ('status','export','approve','schedule')
            if not offline and not args.run:
                raise ValueError('Live stages require --run; status/export/approve are offline')
            if args.command not in ('analyze','schedule','followups','sync-notion') and args.workflow_id is None:
                raise ValueError('Choose an explicit --workflow-id')
            if args.command in ('analyze','search','review','materials','outreach','followups'):
                work.gemini_resume.configuration()
            if args.command in ('search','materials') and not work.pipeline.GROQ_API_KEY.strip():
                raise ValueError('Configure GROQ_API_KEY before search scoring or application preparation')
            if args.command=='analyze':
                workflow_id=flow.create(conn,profile)
                logger.info('Analysis complete. workflow-id=%s; run search next.',workflow_id)
                return 0
            if args.command=='search':
                flow.search(conn,profile,args.workflow_id)
                path=flow.export(conn,profile,args.workflow_id)
                summary(conn,profile,args.workflow_id)
                logger.info('STOP after Task 2. Workbook: %s. Select explicit numbers using approve.',path.relative_to(work.ROOT))
                return 0
            if args.command=='status':
                summary(conn,profile,args.workflow_id)
                return 0
            if args.command=='export':
                path=flow.export(conn,profile,args.workflow_id)
                logger.info('Workbook: %s',path.relative_to(work.ROOT))
                user,workflow=flow.load(conn,profile,args.workflow_id)
                if workflow['state']=='materials_ready':
                    for artifact in flow.export_materials(conn,profile,args.workflow_id):
                        logger.info('Application materials: %s',artifact.relative_to(work.ROOT))
                return 0
            if args.command=='approve':
                if not args.numbers:
                    raise ValueError('approve requires explicit --numbers')
                flow.approve(conn,profile,args.workflow_id,args.numbers)
                logger.info('Saved your selected spreadsheet numbers; no application submitted')
                return 0
            if args.command=='schedule':
                if not args.application_id or args.application_id<=0 or not args.date:
                    raise ValueError('schedule requires --application-id and --date')
                flow.schedule_followup(conn,profile,args.application_id,args.date)
                logger.info('Saved explicit local follow-up date; existing Notion due dates are managed in Notion')
                return 0
            tracker=NotionTracker.configured()
            if args.command=='review':
                flow.review_jobs(conn,profile,args.workflow_id)
            elif args.command=='materials':
                flow.build_materials(conn,profile,args.workflow_id)
                flow.export_materials(conn,profile,args.workflow_id)
            elif args.command=='outreach':
                flow.draft_outreach(conn,profile,args.workflow_id)
                flow.export_materials(conn,profile,args.workflow_id)
            elif args.command=='followups':
                result=flow.followups(conn,profile,tracker)
                directory=work.ROOT/'data/recruiter-workflows'
                if not directory.parent.resolve().is_relative_to(work.ROOT):
                    raise ValueError('Follow-up output must remain private')
                directory.mkdir(exist_ok=True)
                if not directory.resolve().is_relative_to(work.ROOT/'data'):
                    raise ValueError('Follow-up output points outside private data')
                content=json.dumps(result,ensure_ascii=False,indent=2).encode()
                path=directory/('followups-'+result['week_start']+'-'+work.digest(content)[:16]+'.json')
                if not path.exists():
                    with path.open('xb') as output:
                        output.write(content)
                logger.info('Follow-up drafts: %s; %s applications; nothing sent',path.relative_to(work.ROOT),len(result['records']))
                return 0
            elif args.command=='submit':
                flow.load(conn,profile,args.workflow_id)
                if not args.numbers:
                    raise ValueError('submit requires explicit --numbers; daily application count is uncapped')
                records=flow.rows(conn,args.workflow_id)
                selected={row['number'] for row in records if row['selected']}
                if not set(args.numbers).issubset(selected):
                    raise ValueError('Submit only explicitly selected numbers from this workflow')
                for row in records:
                    if row['number'] not in args.numbers:
                        continue
                    material=json.loads(row['materials_json'] or '{}')
                    if not material.get('run_id'):
                        logger.warning('#%s requires manual application; no supported ready run',row['number'])
                        continue
                    approval=flow.approved_posting(conn,profile['user_id'],row['job_id'])
                    if not approval or approval['workflow_id']!=args.workflow_id:
                        raise ValueError('A newer workflow approval exists; submit through that workflow')
                    work.send(conn,profile,material['run_id'],tracker)
                if tracker:
                    work.sync_notion(conn,profile,tracker)
            elif args.command=='sync-notion':
                logger.info('Synced confirmed records: %s',work.sync_notion(conn,profile,tracker))
                return 0
            flow.export(conn,profile,args.workflow_id)
            summary(conn,profile,args.workflow_id)
            return 0
    except KeyboardInterrupt:
        logger.warning('Stopped; reconcile submitting/unknown records before another attempt')
        return 130
    except Exception as error:
        # Known policy errors originate in this program; provider exception bodies stay private.
        if isinstance(error,work.pipeline.requests.RequestException):
            status=error.response.status_code if error.response is not None else 'unavailable'
            logger.error('Provider request stopped (%s, HTTP %s); no automatic retry',type(error).__name__,status)
            if isinstance(error,work.gemini_resume.ProviderResponseError):
                logger.error('%s',error.safe_hint)
        elif type(error) is ValueError or isinstance(error,work.browser.NeedsReview):
            logger.error('Workflow stopped: %s',str(error))
        else:
            logger.error('Workflow stopped (%s); inspect private configuration and saved stage',type(error).__name__)
        return 1
    finally:
        if file_handler is not None:
            logging.getLogger().removeHandler(file_handler)
            file_handler.close()


if __name__=='__main__':
    raise SystemExit(main())
