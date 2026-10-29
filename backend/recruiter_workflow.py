"""Persistent six-stage local workflow with an explicit post-search approval gate."""

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

from backend import application_browser as browser, gemini_resume as gemini, workflow_monitor as monitor
from backend import personal_applications as work, recruiter_ai as ai, recruiter_export, recruiter_sources
from backend.evaluation import validate_evaluation
from backend.cloud_worker import INDIA

MIN_ANNUAL_INR=800000
logger=logging.getLogger(__name__)


def utc_now():
    return datetime.now(timezone.utc)


def timestamp(value):
    if not isinstance(value,str) or len(value)>100:
        raise ValueError('A source ISO posting date is required')
    try:
        parsed=datetime.fromisoformat(value.replace('Z','+00:00'))
    except ValueError as error:
        raise ValueError('Source posting date is not a valid ISO date') from error
    if parsed.tzinfo is None and len(value)!=10:
        raise ValueError('A posting timestamp must include its timezone')
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def salary_status(salary):
    """Only explicit annual INR salary can establish the 8 LPA constraint."""
    if not isinstance(salary,dict):
        return 'unknown'
    amount=salary.get('value')
    if not isinstance(amount,dict) or salary.get('currency')!='INR' or str(amount.get('unitText','')).upper() not in ('YEAR','ANNUAL'):
        return 'unknown'
    lower=amount.get('minValue',amount.get('value'))
    upper=amount.get('maxValue',amount.get('value',lower))
    if (type(lower) not in (int,float) or type(upper) not in (int,float)
            or not 0<=lower<=upper<=1000000000):
        return 'unknown'
    if upper<MIN_ANNUAL_INR:
        return 'below_minimum'
    return 'confirmed' if lower>=MIN_ANNUAL_INR else 'unknown'


def load(conn,profile,workflow_id):
    user=work.user_for_profile(conn,profile)
    row=conn.execute('SELECT * FROM recruiter_workflows WHERE id=? AND user_id=?',
                     (workflow_id,user['id'])).fetchone()
    if row is None:
        raise ValueError('Personal workflow not found')
    if row['resume_hash']!=work.digest(user['resume_text'].encode()):
        raise ValueError('Resume changed; start a new analysis before continuing')
    return user,dict(row)


def create(conn,profile):
    user=work.user_for_profile(conn,profile)
    resume_hash=work.digest(user['resume_text'].encode())
    analysis=ai.analyze(user['resume_text'])
    if work.user_for_profile(conn,profile)['resume_text']!=user['resume_text']:
        raise ValueError('Resume changed during analysis; result discarded')
    analysis['instructions_hash']=work.digest(gemini.INSTRUCTIONS_PATH.read_bytes())
    with conn:
        date=work.now()
        result=conn.execute('''INSERT INTO recruiter_workflows(user_id,resume_hash,state,analysis_json,created_at,updated_at)
            VALUES(?,?,'analyzed',?,?,?)''',(user['id'],resume_hash,json.dumps(analysis),date,date))
    return result.lastrowid


def posting_job(posting):
    return {'title':posting['title'],'url':posting.get('apply_url') or posting['url'],
            'location':posting.get('location',''),'department':'',
            'raw_json':json.dumps({'content':posting['description']})}


def assess(posting,clock):
    facts={'salary_status':salary_status(posting.get('salary')),'reasons':[]}
    try:
        date=timestamp(posting.get('posted_at'))
        if not clock-timedelta(days=7)<=date<=clock:
            facts['reasons'].append('Posting date is outside the last seven days')
            facts['search_status']='excluded'
        else:
            facts['search_status']='shortlisted'
    except (ValueError,TypeError):
        facts.update(search_status='unverified')
        facts['reasons'].append('Original posting date could not be verified')
    expiry=posting.get('valid_through')
    if expiry:
        try:
            if timestamp(expiry)<clock:
                facts['search_status']='excluded'
                facts['reasons'].append('Posting has expired')
        except ValueError:
            facts['search_status']='unverified'
            facts['reasons'].append('Expiry date could not be verified')
    eligibility=ai.classify(posting)
    facts.update(eligibility)
    for kind in ('location','seniority'):
        status=eligibility[kind+'_status']
        if status=='ineligible':
            facts['search_status']='excluded'
            facts['reasons'].append(kind.title()+' does not meet your constraints')
        elif status=='unknown' and facts['search_status']!='excluded':
            facts['search_status']='unverified'
            facts['reasons'].append(kind.title()+' eligibility needs confirmation')
    if facts['salary_status']=='below_minimum':
        facts['search_status']='excluded'
        facts['reasons'].append('Published annual INR pay is below 8 LPA')
    elif facts['salary_status']=='unknown':
        facts['reasons'].append('8 LPA minimum is not confirmed')
    return facts


def search(conn,profile,workflow_id,page_factory=recruiter_sources.research_page,clock=None):
    user,workflow=load(conn,profile,workflow_id)
    has_rows=conn.execute('SELECT 1 FROM recruiter_jobs WHERE workflow_id=?',(workflow_id,)).fetchone()
    if workflow['state'] not in ('analyzed','searching') and not (workflow['state']=='awaiting_approval' and not has_rows):
        raise ValueError('Search is frozen after the spreadsheet approval checkpoint')
    if has_rows:
        raise ValueError('Existing search records must not be overwritten')
    analysis=json.loads(workflow['analysis_json'])
    targets=[row['title'] for row in analysis['titles'][:10]]
    with conn:
        conn.execute("UPDATE recruiter_workflows SET state='searching',updated_at=? WHERE id=?",(work.now(),workflow_id))
        monitor.record(conn,user['id'],'agent','search_start',workflow_id=workflow_id)
    logger.info('Workflow=%s opening research browser',workflow_id)
    with page_factory() as page:
        postings,coverage=recruiter_sources.discover(conn,targets,page)
    logger.info('Workflow=%s discovery returned %s verified postings; evaluating eligibility next',workflow_id,len(postings))
    if not postings:
        with conn:
            conn.execute('UPDATE recruiter_workflows SET coverage_json=?,updated_at=? WHERE id=?',
                         (json.dumps(coverage),work.now(),workflow_id))
        raise ValueError('No postings could be verified. Search is incomplete; inspect data/recruiter.log. No approval workbook was created.')
    clock=utc_now() if clock is None else clock
    if clock.tzinfo is None:
        raise ValueError('Search clock must be timezone-aware')
    records=[]
    identities=set()
    for index,posting in enumerate(postings,1):
        logger.info('Workflow=%s checking eligibility posting=%s/%s',workflow_id,index,len(postings))
        identity=recruiter_sources.public_url(posting.get('apply_url') or posting['url'])
        if identity in identities:
            continue
        identities.add(identity)
        facts=assess(posting,clock)
        evaluation={}
        if facts['search_status']=='shortlisted':
            logger.info('Workflow=%s scoring posting=%s with Groq',workflow_id,index)
            with conn:
                monitor.record(conn,user['id'],'agent','fit_start',workflow_id=workflow_id)
            evaluation=work.pipeline.score_job_for_user(user,posting_job(posting),posting['company_name'])
            validate_evaluation(evaluation,user['resume_text'],posting['description'])
        records.append((posting,facts,evaluation))
    records.sort(key=lambda row:(row[1]['search_status']!='shortlisted',-row[2].get('fit_score',-1),row[0]['company_name']))
    load(conn,profile,workflow_id)
    with conn:
        for number,(posting,facts,evaluation) in enumerate(records,1):
            conn.execute('''INSERT INTO recruiter_jobs(workflow_id,number,posting_json,facts_json,evaluation_json)
                VALUES(?,?,?,?,?)''',(workflow_id,number,json.dumps(posting),json.dumps(facts),json.dumps(evaluation)))
            monitor.record(conn,user['id'],'agent','job_verified',workflow_id=workflow_id,number=number)
            if evaluation:
                monitor.record(conn,user['id'],'agent','fit_done',workflow_id=workflow_id,number=number,value=evaluation['fit_score'])
        conn.execute("UPDATE recruiter_workflows SET state='awaiting_approval',coverage_json=?,updated_at=? WHERE id=?",
                     (json.dumps(coverage),work.now(),workflow_id))
        monitor.record(conn,user['id'],'agent','approval_wait',workflow_id=workflow_id,state='waiting')
    logger.info('Workflow=%s saved %s records; awaiting your job selection',workflow_id,len(records))
    return records


def rows(conn,workflow_id):
    return conn.execute('''SELECT r.*,a.state run_state,a.notion_state run_notion_state,a.document_hash run_document_hash
        FROM recruiter_jobs r JOIN recruiter_workflows w ON w.id=r.workflow_id
        LEFT JOIN application_runs a ON a.id=json_extract(r.materials_json,'$.run_id') AND a.user_id=w.user_id
        WHERE r.workflow_id=? ORDER BY r.number''',(workflow_id,)).fetchall()


def approve(conn,profile,workflow_id,numbers):
    _,workflow=load(conn,profile,workflow_id)
    if workflow['state'] not in ('awaiting_approval','approved','materials_ready'):
        raise ValueError('Finish and inspect the search spreadsheet before choosing jobs')
    if not isinstance(numbers,list) or not 1<=len(numbers)<=1000 or any(type(number) is not int or number<=0 for number in numbers):
        raise ValueError('Select explicit spreadsheet job numbers')
    chosen=set(numbers)
    eligible={row['number'] for row in rows(conn,workflow_id)
              if json.loads(row['facts_json'])['search_status']=='shortlisted'
              and json.loads(row['evaluation_json']).get('fit_score',0)>60}
    if not chosen.issubset(eligible):
        raise ValueError('Only verified shortlist numbers can be selected')
    with conn:
        conn.execute('UPDATE recruiter_jobs SET selected=0 WHERE workflow_id=?',(workflow_id,))
        for number in chosen:
            conn.execute('UPDATE recruiter_jobs SET selected=1 WHERE workflow_id=? AND number=?',(workflow_id,number))
        conn.execute("UPDATE recruiter_workflows SET state='approved',updated_at=? WHERE id=?",(work.now(),workflow_id))


def review_jobs(conn,profile,workflow_id):
    _,workflow=load(conn,profile,workflow_id)
    if workflow['state'] not in ('approved','materials_ready'):
        raise ValueError('Stop after Task 2: select spreadsheet numbers before further stages')
    for row in rows(conn,workflow_id):
        if row['review_json']:
            continue
        posting=json.loads(row['posting_json'])
        facts=json.loads(row['facts_json'])
        result=ai.review(posting)
        if facts['search_status']=='excluded' or facts['salary_status']=='below_minimum':
            result.update(verdict='Skip',reason='; '.join(facts['reasons']))
        elif (facts['search_status']!='shortlisted' or facts['salary_status']!='confirmed') and result['verdict']=='Apply':
            result.update(verdict='Caution',reason='; '.join(facts['reasons']) or 'Eligibility needs confirmation')
        with conn:
            conn.execute('UPDATE recruiter_jobs SET review_json=? WHERE workflow_id=? AND number=?',
                         (json.dumps(result),workflow_id,row['number']))


def bind_job(conn,posting,evaluation,user_id):
    """Import an explicitly selected supported ATS posting without changing subscriptions."""
    canonical=browser.application_url(posting.get('apply_url') or posting['url'])
    parsed=urlsplit(canonical)
    parts=parsed.path.strip('/').split('/')
    ats='greenhouse' if 'greenhouse.io' in parsed.hostname else 'ashby' if 'ashbyhq.com' in parsed.hostname else 'lever'
    slug=parts[0]
    external_id=parts[-1]
    company=conn.execute('SELECT id FROM companies WHERE ats_type=? AND board_slug=?',(ats,slug)).fetchone()
    if company:
        company_id=company[0]
    else:
        company_id=conn.execute('INSERT INTO companies(name,ats_type,board_slug) VALUES(?,?,?)',
                                (posting['company_name'],ats,slug)).lastrowid
    existing=conn.execute('SELECT id FROM jobs WHERE company_id=? AND external_id=?',(company_id,external_id)).fetchone()
    if existing:
        job_id=existing[0]
        current=conn.execute('SELECT title,url,raw_json FROM jobs WHERE id=?',(job_id,)).fetchone()
        # Existing source observations are not overwritten by the workflow importer.
        if browser.application_url(current['url'])!=canonical:
            raise ValueError('Existing posting identity differs from the selected link')
    else:
        date=work.now()
        job_id=conn.execute('''INSERT INTO jobs(company_id,external_id,title,url,location,department,raw_json,first_seen_at,last_seen_at)
            VALUES(?,?,?,?,?,'',?,?,?)''',(company_id,external_id,posting['title'],canonical,posting.get('location',''),
                json.dumps({'content':posting['description']}),date,date)).lastrowid
    conn.execute('''INSERT INTO scores(job_id,user_id,fit_score,reasoning,matched_skills,missing_requirements,scored_at,is_stale,evaluation_json)
        VALUES(?,?,?,?,?,?,?,0,?) ON CONFLICT(job_id,user_id) DO NOTHING''',
        (job_id,user_id,evaluation['fit_score'],evaluation['reasoning'],json.dumps(evaluation['matched_skills']),
         json.dumps(evaluation['missing_requirements']),work.now(),json.dumps(evaluation)))
    return job_id


def build_materials(conn,profile,workflow_id):
    user,workflow=load(conn,profile,workflow_id)
    if workflow['state'] not in ('approved','materials_ready'):
        raise ValueError('Explicit selection after Task 2 is required')
    review_jobs(conn,profile,workflow_id)
    for row in rows(conn,workflow_id):
        if not row['selected'] or row['materials_json']:
            continue
        posting=json.loads(row['posting_json'])
        review=json.loads(row['review_json'])
        original=json.loads(row['evaluation_json'])
        if review['verdict']=='Skip':
            continue
        with conn:
            monitor.record(conn,user['id'],'agent','resume_start',workflow_id=workflow_id,number=row['number'])
        generated=(ai.cover_materials(user['resume_text'],posting) if original['fit_score']>=90
                   else ai.materials(user['resume_text'],posting))
        baseline_content,baseline_text=gemini.document(user['resume_text'])
        job=posting_job(posting)
        baseline=work.pipeline.score_job_for_user(dict(user,resume_text=baseline_text),job,posting['company_name'])
        validate_evaluation(baseline,baseline_text,posting['description'])
        selected_text,document,evaluation=baseline_text,baseline_content,baseline
        if original['fit_score']<90 and generated['resume_text']!=baseline_text:
            revised_document,revised_text=gemini.document(generated['resume_text'])
            revised=work.pipeline.score_job_for_user(dict(user,resume_text=revised_text),job,posting['company_name'])
            validate_evaluation(revised,revised_text,posting['description'])
            if not revised['hard_blockers'] and not revised['unknown_eligibility'] and revised['fit_score']>baseline['fit_score']:
                selected_text,document,evaluation=revised_text,revised_document,revised
        if generated['resume_text']!=selected_text:
            generated['change_log']=['Original resume retained; the proposed rewrite did not provide a validated score improvement.']
            generated['evidence']=[{'text':line,'source':[{'line_id':index,'text':line}]}
                for index,line in enumerate(selected_text.splitlines(),1) if line.strip()]
        generated['resume_text']=selected_text
        generated['final_score']=evaluation['fit_score']
        generated['resume_changed']=selected_text!=baseline_text
        generated['submission']='manual_handoff'
        load(conn,profile,workflow_id)
        if original['hard_blockers'] or original['unknown_eligibility']:
            generated['submission']='Eligibility needs review; materials are drafts and will not be submitted'
            with conn:
                conn.execute('UPDATE recruiter_jobs SET materials_json=? WHERE workflow_id=? AND number=?',
                             (json.dumps(generated),workflow_id,row['number']))
            continue
        try:
            browser.application_url(posting.get('apply_url') or posting['url'])
        except browser.NeedsReview:
            # Materials/outreach remain available for other sites; no fake ATS identity is created.
            generated['submission']='unsupported application site; complete manually and track confirmation'
        else:
            with conn:
                job_id=bind_job(conn,posting,original,user['id'])
                conn.execute('UPDATE recruiter_jobs SET job_id=? WHERE workflow_id=? AND number=?',
                             (job_id,workflow_id,row['number']))
                # Fingerprint format remains owned by the existing application workflow.
                _,fingerprint=work.job_inputs(conn,user,job_id)
                ready=(original['fit_score']>60 and evaluation['fit_score']>=80 and not evaluation['hard_blockers']
                       and not evaluation['unknown_eligibility'] and review['verdict']=='Apply')
                state='ready' if ready else 'needs_review'
                existing=conn.execute('SELECT * FROM application_runs WHERE user_id=? AND job_id=?',(user['id'],job_id)).fetchone()
                if existing and existing['state'] in ('applied','submitting','unknown'):
                    raise ValueError('A submitted or uncertain application cannot be replaced')
                date=work.now()
                details=(fingerprint,state,original['fit_score'],evaluation['fit_score'],90 if original['fit_score']>=80 else 80,
                    selected_text,document,work.digest(document),json.dumps({'original':original,'baseline':baseline,'selected':evaluation}),
                    'Approved workflow '+str(workflow_id)+' job '+str(row['number']),date)
                if existing:
                    snapshot=dict(existing)
                    previous_document=snapshot.pop('document')
                    conn.execute('INSERT INTO recruiter_run_history(run_id,snapshot_json,document,archived_at) VALUES(?,?,?,?)',
                                 (existing['id'],json.dumps(snapshot),previous_document,date))
                    conn.execute('''UPDATE application_runs SET input_hash=?,state=?,original_score=?,final_score=?,target_score=?,
                        resume_text=?,document=?,document_hash=?,evaluation_json=?,detail=?,updated_at=? WHERE id=?''',details+(existing['id'],))
                    run_id=existing['id']
                else:
                    run_id=conn.execute('''INSERT INTO application_runs(user_id,job_id,input_hash,state,original_score,final_score,
                        target_score,resume_text,document,document_hash,evaluation_json,detail,updated_at,created_at)
                        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',(user['id'],job_id)+details+(date,)).lastrowid
                generated.update(run_id=run_id,submission=state,document_hash=work.digest(document))
        with conn:
            conn.execute('UPDATE recruiter_jobs SET materials_json=? WHERE workflow_id=? AND number=?',
                         (json.dumps(generated),workflow_id,row['number']))
            monitor.record(conn,user['id'],'agent','resume_changed' if generated['resume_changed'] else 'resume_retained',
                           workflow_id=workflow_id,number=row['number'],value=evaluation['fit_score'])
    with conn:
        conn.execute("UPDATE recruiter_workflows SET state='materials_ready',updated_at=? WHERE id=?",(work.now(),workflow_id))


def approved_posting(conn,user_id,job_id):
    row=conn.execute('''SELECT r.*,w.user_id,w.resume_hash,w.state workflow_state FROM recruiter_jobs r
        JOIN recruiter_workflows w ON w.id=r.workflow_id WHERE w.user_id=? AND r.job_id=? AND r.selected=1
        AND w.state IN ('approved','materials_ready') ORDER BY w.id DESC LIMIT 1''',(user_id,job_id)).fetchone()
    return dict(row) if row else None


def require_submission_approval(conn,profile,run):
    row=approved_posting(conn,profile['user_id'],run['job_id'])
    if row is None or not row['materials_json'] or not row['review_json']:
        raise browser.NeedsReview('Complete the search, select this job, and generate reviewed materials first')
    material=json.loads(row['materials_json'])
    facts=json.loads(row['facts_json'])
    verdict=json.loads(row['review_json'])['verdict']
    user=work.user_for_profile(conn,profile)
    if (row['resume_hash']!=work.digest(user['resume_text'].encode()) or verdict!='Apply'
            or facts.get('salary_status')!='confirmed' or facts.get('search_status')!='shortlisted'
            or material.get('document_hash')!=run['document_hash'] or material.get('run_id')!=run['id']):
        raise browser.NeedsReview('Approval, eligibility, red-flag review or prepared document changed')
    posting=json.loads(row['posting_json'])
    clock=utc_now()
    if not clock-timedelta(days=7)<=timestamp(posting.get('posted_at'))<=clock:
        raise browser.NeedsReview('Selected posting is no longer within the seven-day search window')


def draft_outreach(conn,profile,workflow_id,page_factory=recruiter_sources.research_page):
    user,workflow=load(conn,profile,workflow_id)
    if workflow['state']!='materials_ready':
        raise ValueError('Prepare approved materials before drafting outreach')
    for row in rows(conn,workflow_id):
        if not row['selected'] or not row['materials_json'] or json.loads(row['review_json'])['verdict']!='Apply':
            continue
        posting=json.loads(row['posting_json'])
        contacts=posting.get('contacts',[])
        contact=contacts[0] if contacts else None
        contact_status='Verified on source posting' if contact else 'No verified recruiter or hiring manager found'
        if not contact:
            try:
                with page_factory() as page:
                    contact=recruiter_sources.find_contact(page,posting['company_name'])
                if contact:
                    contact_status='Company recruiter verified; vacancy ownership unverified'
            except Exception as error:
                contact_status='Contact search unavailable ('+type(error).__name__+'); no contact guessed'
        if contact:
            recruiter_sources.public_url(contact['url'])
            if not contact.get('name') or not contact.get('evidence'):
                raise ValueError('A real contact requires a source name and evidence')
        applied=bool(row['job_id'] and conn.execute("SELECT 1 FROM applications WHERE user_id=? AND job_id=? AND status<>'saved'",
                                                   (user['id'],row['job_id'])).fetchone())
        result={'contact':contact,'contact_status':contact_status,
                'applied':applied,'message':ai.outreach(user['resume_text'],posting,contact,applied)}
        with conn:
            conn.execute('UPDATE recruiter_jobs SET outreach_json=? WHERE workflow_id=? AND number=?',
                         (json.dumps(result),workflow_id,row['number']))


def schedule_followup(conn,profile,application_id,date):
    user=work.user_for_profile(conn,profile)
    from datetime import date as calendar_date
    try:
        due=calendar_date.fromisoformat(date)
    except (TypeError,ValueError) as error:
        raise ValueError('Use an explicit YYYY-MM-DD follow-up date') from error
    if due.isoformat()!=date:
        raise ValueError('Use an explicit YYYY-MM-DD follow-up date')
    existing=conn.execute("SELECT id FROM applications WHERE id=? AND user_id=? AND status IN ('applied','interview')",
                          (application_id,user['id'])).fetchone()
    if not existing:
        raise ValueError('Schedule follow-ups only for your confirmed applied/interview records')
    with conn:
        conn.execute('''INSERT INTO recruiter_followups(application_id,user_id,due_date,updated_at) VALUES(?,?,?,?)
            ON CONFLICT(application_id) DO UPDATE SET due_date=excluded.due_date,updated_at=excluded.updated_at''',
            (application_id,user['id'],date,work.now()))


def followups(conn,profile,tracker,clock=None):
    user=work.user_for_profile(conn,profile)
    clock=utc_now() if clock is None else clock
    if clock.tzinfo is None:
        raise ValueError('Follow-up clock must include a timezone')
    local=clock.astimezone(INDIA).date()
    start=local-timedelta(days=local.weekday())
    end=start+timedelta(days=6)
    if tracker:
        records=tracker.followups(start,end)
    else:
        records=[]
        cursor=conn.execute('''SELECT a.*,f.due_date FROM applications a JOIN recruiter_followups f ON f.application_id=a.id
            AND f.user_id=a.user_id WHERE a.user_id=? AND a.status IN ('applied','interview') AND a.applied_at IS NOT NULL
            AND f.due_date>=? AND f.due_date<=? ORDER BY f.due_date,a.id''',(user['id'],start.isoformat(),end.isoformat()))
        while batch:=cursor.fetchmany(100):
            for row in batch:
                records.append({'application_id':row['id'],'company':row['company_name'],'role':row['title'],
                    'application_date':row['applied_at'],'followup_date':row['due_date'],'contact':None,
                    'history':'Local application status: '+row['status']+'; no contact history recorded',
                    'source':'StudentConnect; follow-up date explicitly scheduled by the owner'})
                if len(records)>10000:
                    raise ValueError('Weekly follow-up list exceeds supported size')
    for record in records:
        record['message']=ai.followup(record)
    return {'week_start':start.isoformat(),'week_end':end.isoformat(),'records':records,
            'tracker':'Notion' if tracker else 'StudentConnect (explicitly scheduled dates)'}


def export(conn,profile,workflow_id):
    _,workflow=load(conn,profile,workflow_id)
    if workflow['state'] not in ('awaiting_approval','approved','materials_ready'):
        raise ValueError('Finish the verified search before exporting the spreadsheet')
    shortlist=[['#','Company','Role','Application link','Date posted','Date evidence','Salary as listed',
        'Original match score','Justification','Resume changes needed','Verification date','Pay status','Verdict','Reason','Selected','Tailored score',
        'Submission state','Notion sync']]
    unverified=[['#','Company','Role','Source link','Status','Reason']]
    for row in rows(conn,workflow_id):
        posting=json.loads(row['posting_json'])
        facts=json.loads(row['facts_json'])
        evaluation=json.loads(row['evaluation_json'])
        review=json.loads(row['review_json'] or '{}')
        material=json.loads(row['materials_json'] or '{}')
        current_run=bool(material.get('document_hash') and row['run_document_hash']==material['document_hash'])
        if facts['search_status']=='shortlisted':
            shortlist.append([row['number'],posting['company_name'],posting['title'],posting.get('apply_url') or posting['url'],
                posting.get('posted_at'),posting.get('date_evidence'),json.dumps(posting.get('salary')) if posting.get('salary') else posting.get('salary_as_listed','Salary not verified'),
                evaluation.get('fit_score'),evaluation.get('reasoning'),'; '.join(facts.get('resume_changes',[])),posting['verified_at'],
                facts['salary_status'],review.get('verdict','Pending review after selection'),review.get('reason',''),
                'Yes' if row['selected'] else 'No',material.get('final_score'),
                row['run_state'] if current_run else material.get('submission','Not prepared'),
                row['run_notion_state'] if current_run else 'Not applicable'])
        else:
            unverified.append([row['number'],posting['company_name'],posting['title'],posting['url'],facts['search_status'],'; '.join(facts['reasons'])])
    analysis=[['Title','Group','Why it fits','Resume evidence','ATS keywords','Keywords not literally present','Gaps']]
    for title in json.loads(workflow['analysis_json'])['titles']:
        analysis.append([title['title'],title['category'],title['why'],
            '\n'.join(str(e['line_id'])+': '+e['text'] for e in title['evidence']),', '.join(title['keywords']),
            ', '.join(title['missing_keywords']),'; '.join(title['gaps'])])
    coverage=[['Source','Coverage status','Discovered','Verified','Limitations']]
    for source,detail in json.loads(workflow['coverage_json']).items():
        coverage.append([source,detail['status'],detail.get('discovered',0),detail.get('verified',0),
                         '; '.join(detail.get('issues',[]))+' '+detail.get('limits','')])
    content=recruiter_export.workbook([('Shortlist',shortlist),('Resume analysis',analysis),('Source coverage',coverage),('Unverified and excluded',unverified)])
    directory=work.ROOT/'data/recruiter-workflows'
    if directory.parent.resolve() != (work.ROOT/'data').resolve():
        raise ValueError('Workflow export must stay inside the repository')
    directory.mkdir(exist_ok=True)
    if not directory.resolve().is_relative_to((work.ROOT/'data').resolve()):
        raise ValueError('Workflow export points outside private data')
    path=directory/('workflow-'+str(workflow_id)+'-'+work.digest(content)[:16]+'.xlsx')
    if not path.exists():
        with path.open('xb') as output:
            output.write(content)
    elif path.read_bytes()!=content:
        raise ValueError('Existing workbook will not be overwritten')
    return path


def export_materials(conn,profile,workflow_id):
    _,workflow=load(conn,profile,workflow_id)
    if workflow['state']!='materials_ready':
        raise ValueError('Prepare approved materials first')
    outputs=[]
    for row in rows(conn,workflow_id):
        if not row['selected'] or not row['materials_json']:
            continue
        value=json.loads(row['materials_json'])
        if value.get('run_id'):
            resume_path=work.export_document(conn,profile['user_id'],value['run_id'],document_hash=value.get('document_hash'))
        else:
            content,_=gemini.document(value['resume_text'])
            directory=work.ROOT/'data/application-documents'
            directory.mkdir(exist_ok=True)
            if not directory.resolve().is_relative_to((work.ROOT/'data').resolve()):
                raise ValueError('Private resume export path is invalid')
            resume_path=directory/('workflow-'+str(workflow_id)+'-'+str(row['number'])+'-'+work.digest(content)[:16]+'.docx')
            if not resume_path.exists():
                with resume_path.open('xb') as output:
                    output.write(content)
        report={'number':row['number'],'resume':str(resume_path.relative_to(work.ROOT)),
                'cover_note':value['cover_note'],'change_log':value['change_log'],'gaps':value['gaps'],
                'submission':row['run_state'] if value.get('document_hash') and row['run_document_hash']==value['document_hash'] else value['submission'],
                'outreach':json.loads(row['outreach_json'] or '{}')}
        data=json.dumps(report,ensure_ascii=False,indent=2).encode()
        path=resume_path.with_name('workflow-'+str(workflow_id)+'-'+str(row['number'])+'-'+work.digest(data)[:16]+'.json')
        if not path.exists():
            with path.open('xb') as output:
                output.write(data)
        outputs.append(path)
    return outputs
