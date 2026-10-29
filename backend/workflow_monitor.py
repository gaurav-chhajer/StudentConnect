"""Owner-scoped monitoring. Events contain codes and numbers, never raw logs."""

from datetime import datetime, timedelta, timezone
import json
import math
import sqlite3

import requests

from backend import store

WORKERS={'fetch':'Job discovery','score':'Local scoring','email':'Email delivery','agent':'Application agent'}
SOURCE_ISSUES={
    'login_or_verification_required':'This site asked for login or verification. Sign in manually to inspect it; automated discovery stopped here.',
    'no_accessible_job_links':'The accessible search page had no usable posting links for this title.',
    'wellfound_search_box_not_found':'The expected search box was unavailable. The site may have changed its layout.',
    'http_page_unavailable':'The site returned an unavailable or failed page.',
    'unsupported_destination':'The posting redirected outside the currently allowed public job sites.',
    'missing_or_ambiguous_jobposting_data':'The page lacked one clear structured job record, so its details were not trusted.',
    'invalid_jobposting_json':'The posting contained invalid structured job data.',
    'missing_posting_fields':'The posting omitted a required title, employer, or usable description.',
}
# Title, explanation, recovery action, severity. Unknown exceptions never become UI copy.
MESSAGES={
    'fetch_start':('Checking company boards','Looking for new or updated openings.','No action needed.','info'),
    'fetch_done':('Openings saved','The company-board results were saved locally.','Open a job below to inspect its saved stages.','info'),
    'fetch_failed':('A company board could not be checked','Saved openings were retained; this board may be incomplete.','Check connectivity and the company board. The regular fetch schedule will check again.','warning'),
    'score_start':('Scoring jobs','The local model is evaluating saved openings.','No action needed.','info'),
    'score_done':('Job scored','A local relevance ranking was saved. This is not the application fit score.','Review the job requirements before applying.','info'),
    'score_idle':('Scoring check finished','The local scoring pass finished. New eligible work remains queued.','No action needed.','info'),
    'email_start':('Sending a job digest','The email provider is being asked to accept this digest.','Wait for confirmation; do not send a second copy.','info'),
    'email_check':('Checking scheduled digests','Saved schedules and unsent jobs are being checked.','No action needed.','info'),
    'email_idle':('Email check finished','The scheduled digest check finished. Sent jobs appear in job history.','No action needed.','info'),
    'email_sent':('Digest accepted by email provider','The included jobs were recorded in sent history. Inbox delivery is not confirmed.','Check your inbox and spam folder.','info'),
    'email_paused':('Email delivery paused','A previous email attempt needs reconciliation. Its delivery may be uncertain.','Check the inbox and provider history before clearing the existing email pause marker.','error'),
    'email_auth':('Email sign-in was rejected','The SMTP provider rejected authentication.','Review the sender account and app password in private local configuration.','error'),
    'email_error':('Email sending failed','The email provider did not confirm a successful send.','Check the inbox and provider history before restarting email delivery.','error'),
    'cycle_start':('Application check started','The agent is reading the saved workflow and its approvals.','No action needed.','info'),
    'search_start':('Searching and verifying jobs','Public sources and configured ATS boards are being checked.','Keep the research browser open. Some sources may require manual access.','info'),
    'job_verified':('Posting verified','An accessible posting was saved to this workflow.','Review its source and eligibility in the spreadsheet.','info'),
    'fit_start':('Evaluating job fit','The saved resume and job requirements are being scored.','Wait for the provider response.','info'),
    'fit_done':('Fit score saved','The original resume evaluation was saved.','The final document must reach 80 before submission.','info'),
    'approval_wait':('Waiting for your selection','Search is complete; no application is approved automatically.','Inspect the spreadsheet and approve explicit Lever job numbers with the recruiter CLI.','warning'),
    'resume_start':('Preparing a resume','The agent is preparing truthful materials and checking their score.','Wait for factuality and score checks to finish.','info'),
    'resume_changed':('Tailored resume prepared','A revised document was selected after validation and rescoring.','Review its final score and application status below.','info'),
    'resume_retained':('Original resume retained','Materials were prepared using the original document.','A rewrite is used only when it provides a validated improvement.','info'),
    'submit_start':('Application in progress','The agent is checking and filling the approved application form.','Do not manually submit another copy.','info'),
    'applied':('Application confirmed','The browser confirmed submission and saved the application locally.','Check employer confirmation and the tracker status below.','info'),
    'blocked':('A job needs your review','This job has not passed every submission check.','Inspect its job details; resolve missing answers, eligibility, or score issues before another attempt.','warning'),
    'complete':('Selected batch completed','All selected jobs in this batch have confirmed applications.','Create a new workflow for another search.','info'),
    'waiting':('Next check scheduled','The worker is waiting between checks.','The next time is a schedule, not a guarantee of execution.','info'),
    'stopped':('Worker stopped','The worker reported a stop. Saved progress remains available.','Reconcile any uncertain application before restarting.','warning'),
    'missing_gemini':('Gemini settings are missing','This worker process cannot see both the Gemini key and model.','Set GEMINI_API_KEY and GEMINI_MODEL in the same terminal that starts the agent. Never paste the key into this page.','error'),
    'missing_groq':('Scoring key is missing','This worker process cannot see the Groq scoring key.','Configure GROQ_API_KEY privately before restarting the agent.','error'),
    'provider_timeout':('The AI provider took too long','No complete provider response was received before the timeout.','Check provider usage before retrying; the request may have been processed or billed.','error'),
    'network_error':('A service connection failed','The request could not complete over the network; the precise network cause is not known.','Check your connection and service availability. Reconcile any uncertain write before retrying.','error'),
    'provider_auth':('The AI provider rejected access','The provider returned an authentication or permission error.','Check the private key, project access, and model permissions.','error'),
    'provider_quota':('The AI provider limited the request','The provider reported a rate or quota limit. This does not identify the account balance.','Check project quota and billing, then restart when capacity is available.','error'),
    'provider_request':('The AI provider rejected the request','The provider reported a request error; its private response is withheld.','Inspect the safe provider diagnostics in the local recruiter log for model or schema information.','error'),
    'browser_closed':('The browser closed unexpectedly','The browser target closed or crashed before the operation finished.','Check whether the browser exited. Reconcile uncertain submissions before restarting.','error'),
    'storage_error':('Progress could not be saved','A local database operation failed.','Check disk space, database access, and competing workers. Saved confirmations must be checked before retrying.','error'),
    'unknown_error':('The worker encountered an unexpected error','The precise cause is not known from safe telemetry.','Inspect the terminal and private local log. Do not retry an uncertain submission.','error'),
    'no_verified':('No postings could be verified','Search did not produce usable postings. No approval workbook was created.','Review source coverage and local browser diagnostics before a new explicit search attempt.','error'),
    'lever_only':('The selection includes an unsupported site','This agent submits Lever applications only.','Approve only Lever numbers in this batch, or use the separate manual workflow for other sites.','error'),
    'reconcile':('An earlier attempt needs reconciliation','A submission or tracker write failed or has an uncertain outcome.','Check employer confirmation and tracker history. Never retry an uncertain submission automatically.','error'),
}


def available(conn):
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='worker_events'").fetchone() is not None


def record(conn,user_id,worker,code,*,workflow_id=None,job_id=None,number=None,value=None,state='running',next_check=None,event=True):
    """Caller owns the transaction; do not commit or swallow storage errors here."""
    if worker not in WORKERS or code not in MESSAGES or state not in ('running','waiting','idle','error','complete','stopped'):
        raise ValueError('Invalid monitor event')
    if type(user_id) is not int or user_id<=0:
        raise ValueError('Invalid monitor owner')
    for identifier in (workflow_id,job_id,number):
        if identifier is not None and (type(identifier) is not int or identifier<=0):
            raise ValueError('Invalid monitor resource')
    if value is not None and (type(value) not in (int,float) or not math.isfinite(value) or not 0<=value<=1000000000):
        raise ValueError('Invalid monitor metric')
    if workflow_id is not None and not conn.execute('SELECT 1 FROM recruiter_workflows WHERE id=? AND user_id=?',(workflow_id,user_id)).fetchone():
        raise ValueError('Monitor workflow owner mismatch')
    now=datetime.now(timezone.utc).isoformat()
    if next_check is not None:
        parsed=datetime.fromisoformat(next_check)
        if parsed.tzinfo is None:
            raise ValueError('Monitor schedule must include timezone')
    conn.execute('''INSERT INTO worker_pulses(user_id,worker,workflow_id,state,code,updated_at,next_check_at)
        VALUES(?,?,?,?,?,?,?) ON CONFLICT(user_id,worker,workflow_id) DO UPDATE SET
        state=excluded.state,code=excluded.code,updated_at=excluded.updated_at,next_check_at=excluded.next_check_at''',
        (user_id,worker,workflow_id or 0,state,code,now,next_check))
    if event:
        conn.execute('INSERT INTO worker_events(user_id,worker,workflow_id,job_id,number,code,value,created_at) VALUES(?,?,?,?,?,?,?,?)',
                     (user_id,worker,workflow_id,job_id,number,code,value,now))


def error_code(error):
    if str(error)=='Configure GEMINI_API_KEY and an explicit GEMINI_MODEL':
        return 'missing_gemini'
    if str(error).startswith('Configure GROQ_API_KEY'):
        return 'missing_groq'
    if str(error).startswith(('No postings could be verified','Empty search needs')):
        return 'no_verified'
    if str(error).startswith('This agent supports Lever only'):
        return 'lever_only'
    if str(error).startswith('Saved application or tracker failure'):
        return 'reconcile'
    if type(error).__name__=='TargetClosedError' or str(error).startswith('Research browser/session closed'):
        return 'browser_closed'
    if isinstance(error,requests.Timeout):
        return 'provider_timeout'
    if isinstance(error,requests.ConnectionError):
        return 'network_error'
    if isinstance(error,requests.HTTPError):
        status=error.response.status_code if error.response is not None else None
        return 'provider_auth' if status in (401,403) else 'provider_quota' if status==429 else 'provider_request'
    if isinstance(error,sqlite3.Error):
        return 'storage_error'
    if type(error).__name__=='SMTPAuthenticationError':
        return 'email_auth'
    if str(error).startswith('Email delivery is paused'):
        return 'email_paused'
    return 'unknown_error'


def local_record(conn,worker,code,**kwargs):
    """Shared fetch health is visible to admins; personal events specify an owner."""
    admins=conn.execute('''SELECT u.id FROM users u JOIN accounts a ON a.user_id=u.id
        WHERE u.active=1 AND a.email_verified=1 AND a.is_admin=1 ORDER BY u.id LIMIT 1000''').fetchall()
    for row in admins:
        record(conn,row['id'],worker,code,**kwargs)


def message(code):
    title,description,action,severity=MESSAGES.get(code,MESSAGES['unknown_error'])
    return dict(title=title,description=description,action=action,severity=severity)


def stamp(value):
    if not value:
        return None
    parsed=datetime.fromisoformat(value)
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def job_view(row):
    item=dict(row)
    material=json.loads(item.get('materials_json') or '{}')
    item['resume_status']=('Revised resume' if material.get('resume_changed') is True else 'Original retained'
                           if material.get('resume_changed') is False else 'Prepared; revision not recorded'
                           if material else 'Not prepared')
    item['final_score']=material.get('final_score',item.get('final_score'))
    item['url']=store.safe_url(item.get('url') or '')
    item['state']=item.get('run_state') or ('Selected' if item.get('selected') else 'Not selected')
    item['needs_review']=item['state'] in ('needs_review','unknown','failed','submitting')
    item['resume_prepared']=bool(material) or bool(item.get('document_hash'))
    item['email_sent']=bool(item.get('email_at'))
    item['reason']={'needs_review':'Submission checks or required answers need review.',
        'unknown':'Submission may have happened. Check employer confirmation before retrying.',
        'submitting':'Submission was started. Do not send another copy.',
        'failed':'An attempt failed before confirmation. Inspect the activity log.',
        'ready':'Prepared document passed the score checks and is waiting for submission.',
        'applied':'Submission confirmed and recorded locally.'}.get(item['state'],'No confirmed application yet.')
    return item


def snapshot(conn,user_id,workflow_id=0,*,page=1,stage='all',severity='all',before=0,now=None):
    now=now or datetime.now(timezone.utc)
    if stage not in ('all','scored','prepared','applied','review') or severity not in ('all','info','warning','error'):
        raise ValueError('Invalid monitor filter')
    data=dict(available=available(conn),workflows=[],jobs=[],events=[],workers=[],counts={},sources=[],issues=[],page=page,pages=1,
              workflow_id=workflow_id,stage=stage,severity=severity,before=before,next_before=None,updated_at=now.isoformat())
    if not data['available']:
        return data
    data['workflows']=conn.execute('SELECT id,state,updated_at FROM recruiter_workflows WHERE user_id=? ORDER BY id DESC LIMIT 50',(user_id,)).fetchall()
    if workflow_id:
        workflow=conn.execute('SELECT * FROM recruiter_workflows WHERE id=? AND user_id=?',(workflow_id,user_id)).fetchone()
        if workflow is None:
            raise LookupError('Workflow not found')
        data['workflow_state']=workflow['state']
        raw=conn.execute('''SELECT r.number,r.selected,r.materials_json,
            json_extract(r.posting_json,'$.title') title,json_extract(r.posting_json,'$.company_name') company_name,
            json_extract(r.posting_json,'$.url') url,json_extract(r.posting_json,'$.source') source,
            json_extract(r.posting_json,'$.verified_at') fetched_at,
            json_extract(r.evaluation_json,'$.fit_score') original_score,a.state run_state,a.notion_state,
            a.document_hash,a.updated_at application_at,NULL relevance,NULL email_at
            FROM recruiter_jobs r JOIN recruiter_workflows w ON w.id=r.workflow_id
            LEFT JOIN application_runs a ON a.id=json_extract(r.materials_json,'$.run_id') AND a.user_id=w.user_id
            WHERE w.id=? AND w.user_id=? ORDER BY r.number LIMIT 1000''',(workflow_id,user_id)).fetchall()
        coverage=json.loads(workflow['coverage_json'])
        data['sources']=[dict(name=name,**details,
            explanations=list(dict.fromkeys(SOURCE_ISSUES.get(issue.partition(':')[2],
                'This source was unavailable; the saved search did not record a more precise safe cause.')
                for issue in details.get('issues',[])[:20]))) for name,details in coverage.items()]
    else:
        raw=conn.execute('''SELECT j.id number,0 selected,NULL materials_json,j.title,c.name company_name,j.url,
            COALESCE(c.career_source,c.ats_type) source,j.last_seen_at fetched_at,s.fit_score original_score,
            l.rank_score relevance,a.state run_state,a.final_score,a.document_hash,a.notion_state,a.updated_at application_at,
            (SELECT MAX(sent_at) FROM sent_log t WHERE t.user_id=? AND t.job_id=j.id AND t.channel='email') email_at
            FROM jobs j JOIN companies c ON c.id=j.company_id
            LEFT JOIN scores s ON s.job_id=j.id AND s.user_id=?
            LEFT JOIN local_similarity l ON l.job_id=j.id AND l.user_id=?
            LEFT JOIN application_runs a ON a.job_id=j.id AND a.user_id=?
            WHERE c.active=1 AND (NOT EXISTS(SELECT 1 FROM user_settings WHERE user_id=? AND subscriptions_configured=1)
                OR EXISTS(SELECT 1 FROM company_subscriptions WHERE user_id=? AND company_id=c.id))
            ORDER BY j.last_seen_at DESC,j.id DESC LIMIT 1001''',(user_id,)*6).fetchall()
        data['limited']=len(raw)>1000
        raw=raw[:1000]
    jobs=[job_view(row) for row in raw]
    data['counts']={'fetched':len(jobs),'scored':sum(j['original_score'] is not None or j['relevance'] is not None for j in jobs),
                    'prepared':sum(j['resume_prepared'] for j in jobs),'applied':sum(j['state']=='applied' for j in jobs),
                    'review':sum(j['needs_review'] for j in jobs),'emailed':sum(j['email_sent'] for j in jobs)}
    data['chart_max']=max(1,data['counts']['fetched'])
    filtered=[j for j in jobs if stage=='all' or stage=='scored' and (j['original_score'] is not None or j['relevance'] is not None)
              or stage=='prepared' and j['resume_prepared'] or stage=='applied' and j['state']=='applied' or stage=='review' and j['needs_review']]
    data['total']=len(filtered)
    data['pages']=max(1,(len(filtered)+19)//20)
    data['jobs']=filtered[(page-1)*20:page*20]
    for worker,name in WORKERS.items():
        pulse=conn.execute('SELECT * FROM worker_pulses WHERE user_id=? AND worker=? AND workflow_id=?',
                           (user_id,worker,workflow_id if worker=='agent' else 0)).fetchone()
        item=dict(pulse) if pulse else dict(state='unreported',updated_at=None,next_check_at=None,code=None)
        item.update(name=name,worker=worker,stale=bool(pulse and now-stamp(pulse['updated_at'])>timedelta(minutes=15)
                                                   and pulse['state'] not in ('error','complete','stopped')))
        item['detail']=message(item['code'])['title'] if item['code'] else 'No telemetry reported yet'
        data['workers'].append(item)
        if pulse and pulse['state']=='error':
            data['issues'].append(dict(worker=name,**message(pulse['code'])))
    events=conn.execute('''SELECT e.*,j.title job_title FROM worker_events e
        LEFT JOIN jobs j ON j.id=e.job_id WHERE e.user_id=? AND (e.workflow_id=? OR e.workflow_id IS NULL)
        AND (?=0 OR e.id<?) ORDER BY e.id DESC LIMIT 101''',(user_id,workflow_id or -1,before,before)).fetchall()
    for event in events[:100]:
        item=dict(event)
        item.update(message(item['code']))
        if severity=='all' or item['severity']==severity:
            data['events'].append(item)
    data['next_before']=events[99]['id'] if len(events)>100 else None
    return data
