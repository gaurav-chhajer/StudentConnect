"""Local personal application workflow, separate from matching and master resumes."""

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from backend import application_browser as browser, gemini_resume, matching_tasks
from backend import pipeline_skeleton as pipeline
from backend.evaluation import RUBRIC_VERSION, validate_evaluation

ROOT = Path(__file__).resolve().parents[1]
PAGE_SIZE = 100


def now():
    return datetime.now(timezone.utc).isoformat()


def digest(value):
    return hashlib.sha256(value).hexdigest()


def load_profile(path):
    path = Path(path).resolve()
    if not path.is_relative_to(ROOT / 'data') or path.stat().st_size > 65536:
        raise ValueError('Application profile must be a bounded private file inside data/')
    value = json.loads(path.read_text(encoding='utf-8-sig'))
    if not isinstance(value, dict) or set(value) != {'user_id', 'email', 'answers', 'history_checked'}:
        raise ValueError('Profile requires user_id, email, answers, history_checked')
    if type(value['user_id']) is not int or value['user_id'] <= 0:
        raise ValueError('Choose an explicit personal user_id')
    if (not isinstance(value['email'], str) or len(value['email']) > 254
            or not re.fullmatch(r'[^\s<>@]+@[^\s<>@]+\.[^\s<>@]+', value['email'])):
        raise ValueError('Profile email must identify the chosen account')
    if type(value['history_checked']) is not bool:
        raise ValueError('history_checked must be true or false')
    answers = value['answers']
    if not isinstance(answers, dict) or len(answers) > 100:
        raise ValueError('Provide at most 100 approved application answers')
    cleaned = {}
    for label, answer in answers.items():
        if (not isinstance(label, str) or not 1 <= len(label.strip()) <= 300
                or not isinstance(answer, str) or not 1 <= len(answer.strip()) <= 4000
                or '\x00' in answer):
            raise ValueError('Approved answer labels and text must be nonempty and bounded')
        key = browser.normalized(label)
        if key in cleaned:
            raise ValueError('Approved answer labels must be distinct')
        cleaned[key] = answer.strip()
    for label in ('email', 'email address'):
        if label in cleaned and cleaned[label].casefold() != value['email'].casefold():
            raise ValueError('Application email must match the chosen account')
        cleaned[label] = value['email']
    value['answers'] = cleaned
    return value


def user_for_profile(conn, profile):
    row = conn.execute('''SELECT u.* FROM users u JOIN accounts a ON a.user_id=u.id
        WHERE u.id=? AND u.active=1 AND a.email_verified=1''', (profile['user_id'],)).fetchone()
    if row is None or row['email'].casefold() != profile['email'].casefold() or not row['resume_text'].strip():
        raise ValueError('Profile must match an active verified account with a saved resume')
    return dict(row)


def job_inputs(conn, user, job_id):
    job = conn.execute('''SELECT j.*,c.name company_name,c.active company_active,
        s.fit_score,s.is_stale,s.evaluation_json FROM jobs j JOIN companies c ON c.id=j.company_id
        JOIN scores s ON s.job_id=j.id AND s.user_id=? WHERE j.id=?''', (user['id'], job_id)).fetchone()
    if job is None:
        raise browser.NeedsReview('No saved job evaluation')
    job = dict(job)
    from backend.recruiter_workflow import approved_posting
    approval=approved_posting(conn,user['id'],job_id)
    stored_raw=job['raw_json']
    if approval:
        if approval['resume_hash']!=digest(user['resume_text'].encode()):
            raise browser.NeedsReview('Resume changed since shortlist approval')
        posting=json.loads(approval['posting_json'])
        evaluation=json.loads(approval['evaluation_json'])
        job.update(title=posting['title'],location=posting.get('location',''),department='',
            url=browser.application_url(posting.get('apply_url') or posting['url']),
            raw_json=json.dumps({'content':posting['description']}),
            fit_score=evaluation['fit_score'],evaluation_json=approval['evaluation_json'])
    if (not job['is_active'] or not job['company_active'] or job['is_stale']
            or job['fit_score'] <= 60 or not approval and job['company_id'] not in matching_tasks.companies_for_user(conn, user['id'])):
        raise browser.NeedsReview('Posting, subscription or score is no longer eligible')
    if not approval and not pipeline.filter_jobs_for_user([job], user):
        raise browser.NeedsReview('Posting no longer matches saved preferences')
    description = pipeline.extract_job_description(job)
    evaluation = json.loads(job['evaluation_json'] or '{}')
    validate_evaluation(evaluation, user['resume_text'], description)
    if evaluation['fit_score'] != job['fit_score']:
        raise browser.NeedsReview('Stored score and evaluation disagree')
    if evaluation.get('provider') != 'groq' or evaluation.get('rubric_version') != RUBRIC_VERSION:
        raise browser.NeedsReview('A current Groq rubric evaluation is required')
    if evaluation.get('model') != pipeline.GROQ_MODEL:
        raise browser.NeedsReview('Rescoring model differs from the original evaluation')
    if evaluation['hard_blockers'] or evaluation['unknown_eligibility']:
        raise browser.NeedsReview('Eligibility requires your review')
    browser.application_url(job['url'])
    identity = dict(resume=user['resume_text'], preferences=user['preferences_json'],
        job={key: job[key] for key in ('id','company_id','title','url','raw_json','department','location')},
        evaluation=evaluation,stored_job_raw=stored_raw)
    fingerprint = digest(json.dumps(identity, sort_keys=True).encode())
    return job, fingerprint


def application_history(conn, user_id, job):
    canonical = browser.application_url(job['url'])
    saved=[]
    # Scan a bounded cursor rather than loading unknown historical data at once.
    rows = conn.execute('SELECT id,job_id,url,status,applied_at FROM applications WHERE user_id=?', (user_id,))
    while batch := rows.fetchmany(PAGE_SIZE):
        for row in batch:
            matched=row['job_id']==job['id']
            if not matched and row['url']:
                from urllib.parse import urlsplit
                matched=urlsplit(row['url']).netloc in browser.ATS_HOSTS and browser.application_url(row['url'])==canonical
            if matched:
                if row['status']!='saved' or row['applied_at']:
                    return True,None
                saved.append(row['id'])
                if len(saved)>1:
                    raise browser.NeedsReview('Multiple saved records for this posting need reconciliation')
    return False,saved[0] if saved else None


def already_tracked(conn, user_id, job):
    canonical = browser.application_url(job['url'])
    blocked,_=application_history(conn,user_id,job)
    if blocked:
        return True
    other = conn.execute('''SELECT j.url FROM application_runs r JOIN jobs j ON j.id=r.job_id
        WHERE r.user_id=? AND r.job_id<>?''', (user_id,job['id']))
    while batch := other.fetchmany(PAGE_SIZE):
        if any(browser.application_url(row['url']) == canonical for row in batch):
            return True
    return False


def candidates(conn, user_id, after=0):
    return conn.execute('''SELECT j.id FROM scores s JOIN jobs j ON j.id=s.job_id
        JOIN companies c ON c.id=j.company_id WHERE s.user_id=? AND s.fit_score>60
        AND s.is_stale=0 AND j.is_active=1 AND c.active=1 AND j.id>?
        AND NOT EXISTS(SELECT 1 FROM application_runs r WHERE r.user_id=s.user_id AND r.job_id=j.id)
        AND EXISTS(SELECT 1 FROM recruiter_jobs r JOIN recruiter_workflows w ON w.id=r.workflow_id
            WHERE r.job_id=j.id AND w.user_id=s.user_id AND r.selected=1 AND w.state IN ('approved','materials_ready'))
        ORDER BY j.id LIMIT ?''', (user_id, after, PAGE_SIZE)).fetchall()


def set_state(conn, run_id, state, detail):
    with conn:
        conn.execute('UPDATE application_runs SET state=?,detail=?,updated_at=? WHERE id=?',
                     (state, detail, now(), run_id))


def prepare(conn, profile, job_id, tracker=None):
    user = user_for_profile(conn, profile)
    from backend.recruiter_workflow import approved_posting
    if not approved_posting(conn,user['id'],job_id):
        raise browser.NeedsReview('Stop after Task 2: select this job in a recruiter workflow first')
    # A permanent row makes re-runs conservative even after crashes.
    with conn:
        conn.execute('BEGIN IMMEDIATE')
        existing = conn.execute('SELECT id FROM application_runs WHERE user_id=? AND job_id=?',
                                (user['id'], job_id)).fetchone()
        if existing:
            return existing['id']
        job, fingerprint = job_inputs(conn, user, job_id)
        if already_tracked(conn, user['id'], job):
            raise browser.NeedsReview('Existing local application history blocks resubmission')
        if job['fit_score'] < 90:
            gemini_resume.configuration()  # Fail before a paid scoring request or claiming work.
        timestamp = now()
        run_id = conn.execute('''INSERT INTO application_runs(user_id,job_id,input_hash,state,
            original_score,target_score,created_at,updated_at) VALUES(?,?,?,'preparing',?,?,?,?)''',
            (user['id'],job_id,fingerprint,job['fit_score'],90 if job['fit_score'] >= 80 else 80,
             timestamp,timestamp)).lastrowid
    try:
        if tracker:
            tracker.check(job['url'])
        elif not profile['history_checked']:
            raise browser.NeedsReview('Configure Notion or reconcile existing application history first')
        baseline_content, baseline_text = gemini_resume.document(user['resume_text'])
        baseline = pipeline.score_job_for_user(dict(user, resume_text=baseline_text), job, job['company_name'])
        validate_evaluation(baseline, baseline_text, pipeline.extract_job_description(job))
        selected_text, content, evaluation = baseline_text, baseline_content, baseline
        # Already 90+ needs no tailoring. Lower scores get exactly one attempt.
        if job['fit_score'] < 90:
            tailored = gemini_resume.tailor(user['resume_text'], pipeline.extract_job_description(job),
                                          90 if job['fit_score'] >= 80 else 80)
            tailored_content, tailored_text = gemini_resume.document(tailored)
            if tailored_text != baseline_text:
                revised = pipeline.score_job_for_user(dict(user, resume_text=tailored_text), job, job['company_name'])
                validate_evaluation(revised, tailored_text, pipeline.extract_job_description(job))
                if (not revised['hard_blockers'] and not revised['unknown_eligibility']
                        and revised['fit_score'] > baseline['fit_score']):
                    selected_text, content, evaluation = tailored_text, tailored_content, revised
        if evaluation['hard_blockers'] or evaluation['unknown_eligibility']:
            raise browser.NeedsReview('Document evaluation has unresolved eligibility')
        state = 'ready' if evaluation['fit_score'] >= 80 else 'needs_review'
        with conn:
            conn.execute('''UPDATE application_runs SET state=?,final_score=?,resume_text=?,document=?,
                document_hash=?,evaluation_json=?,detail=?,updated_at=? WHERE id=?''',
                (state,evaluation['fit_score'],selected_text,content,digest(content),json.dumps({
                    'original':json.loads(job['evaluation_json']),
                    'baseline':baseline,'selected':evaluation,
                    'tailoring_strategy':'section-order-v1',
                    'tailoring_model':gemini_resume.configuration()[1] if job['fit_score'] < 90 else None}),
                 'Ready for browser submission' if state == 'ready' else 'Validated document remains below 80',now(),run_id))
    except browser.NeedsReview as error:
        set_state(conn,run_id,'needs_review',str(error))
    except Exception:
        set_state(conn,run_id,'failed','Preparation failed; inspect provider configuration or inputs before retrying')
        raise
    return run_id


def export_document(conn, user_id, run_id, *, document_hash=None):
    row = conn.execute('SELECT document,document_hash FROM application_runs WHERE id=? AND user_id=?',
                       (run_id,user_id)).fetchone()
    if row is not None and document_hash is not None and row['document_hash']!=document_hash:
        row=conn.execute('''SELECT h.document,json_extract(h.snapshot_json,'$.document_hash') document_hash
            FROM recruiter_run_history h JOIN application_runs r ON r.id=h.run_id
            WHERE r.id=? AND r.user_id=? AND json_extract(h.snapshot_json,'$.document_hash')=? ORDER BY h.id DESC LIMIT 1''',
            (run_id,user_id,document_hash)).fetchone()
    if row is None or not row['document'] or digest(row['document']) != row['document_hash']:
        raise ValueError('No intact prepared document for this personal run')
    directory = ROOT / 'data/application-documents'
    if not directory.parent.resolve().is_relative_to(ROOT):
        raise ValueError('Resume exports must stay inside the repository')
    directory.mkdir(exist_ok=True)
    if not directory.resolve().is_relative_to(ROOT / 'data'):
        raise ValueError('Resume export directory points outside private data/')
    path = directory / (str(run_id) + '-' + row['document_hash'] + '.docx')
    if path.exists():
        if path.stat().st_size > gemini_resume.resumes.MAX_UPLOAD_BYTES or digest(path.read_bytes()) != row['document_hash']:
            raise ValueError('Existing exported document differs; it will not be overwritten')
    else:
        with path.open('xb') as output:
            output.write(row['document'])
    return path


def send(conn, profile, run_id, tracker=None, page_factory=browser.browser_page):
    run = conn.execute('SELECT * FROM application_runs WHERE id=? AND user_id=?',
                       (run_id, profile['user_id'])).fetchone()
    if run is None or run['state'] != 'ready':
        raise browser.NeedsReview('Only a ready personal application can be submitted')
    run = dict(run)

    def recheck():
        from backend.recruiter_workflow import require_submission_approval
        require_submission_approval(conn,profile,run)
        user = user_for_profile(conn, profile)
        job, fingerprint = job_inputs(conn,user,run['job_id'])
        if fingerprint != run['input_hash'] or already_tracked(conn,user['id'],job):
            raise browser.NeedsReview('Inputs or application history changed')
        if (type(run['final_score']) is not int or run['final_score'] < 80 or
                not run['document'] or digest(run['document']) != run['document_hash']):
            raise browser.NeedsReview('Prepared document or score is invalid')
        extracted = gemini_resume.resumes.extract_docx(run['document'])
        if extracted != run['resume_text']:
            raise browser.NeedsReview('Prepared document changed')
        selected = json.loads(run['evaluation_json'])['selected']
        validate_evaluation(selected, extracted, pipeline.extract_job_description(job))
        if selected['fit_score'] != run['final_score'] or selected['unknown_eligibility'] or selected['hard_blockers']:
            raise browser.NeedsReview('Final evaluation is no longer safe to submit')
        if tracker:
            tracker.check(job['url'])
        elif not profile['history_checked']:
            raise browser.NeedsReview('Application history has not been reconciled')
        return job

    def before_submit():
        with conn:
            conn.execute('BEGIN IMMEDIATE')
            recheck()
            changed = conn.execute("UPDATE application_runs SET state='submitting',updated_at=? WHERE id=? AND state='ready'",
                                   (now(),run_id)).rowcount
            if changed != 1:
                raise browser.NeedsReview('Application was claimed by another worker')

    try:
        job = recheck()
        from backend.recruiter_workflow import approved_posting
        approval=approved_posting(conn,profile['user_id'],run['job_id'])
        materials=json.loads(approval['materials_json'])
        answers=dict(profile['answers'])
        if materials.get('cover_note'):
            answers.update({'cover letter':materials['cover_note'],'cover note':materials['cover_note']})
        with page_factory() as page:
            confirmation = browser.submit(page,job['url'],run['document'],answers,before_submit)
            timestamp = now()
            # Persist confirmation before browser cleanup, which itself can fail.
            with conn:
                state = conn.execute('SELECT state FROM application_runs WHERE id=?',(run_id,)).fetchone()[0]
                if state != 'submitting':
                    raise RuntimeError('Browser returned without recording submission intent')
                blocked,saved_id=application_history(conn,profile['user_id'],job)
                if saved_id:
                    conn.execute("""UPDATE applications SET job_id=?,status='applied',applied_at=?,updated_at=?
                        WHERE id=? AND user_id=?""",(job['id'],timestamp,timestamp,saved_id,profile['user_id']))
                elif not blocked:
                    conn.execute('''INSERT INTO applications(user_id,job_id,title,company_name,url,status,
                        notes,applied_at,created_at,updated_at) VALUES(?,?,?,?,?,'applied',?,?,?,?)''',
                        (profile['user_id'],job['id'],job['title'],job['company_name'],job['url'],
                         f"Browser confirmation recorded. Resume score {run['final_score']}; application run {run_id}.",
                         timestamp,timestamp,timestamp))
                conn.execute("""UPDATE application_runs SET state='applied',confirmation=?,detail=?,
                    notion_state=?,updated_at=? WHERE id=?""",
                    (confirmation,'Submission confirmed', 'pending' if tracker else 'local', timestamp,run_id))
    except BaseException as error:
        state = conn.execute('SELECT state FROM application_runs WHERE id=?',(run_id,)).fetchone()[0]
        if state == 'submitting':
            set_state(conn,run_id,'unknown','Submission may have occurred. Check employer confirmation; never retry automatically.')
        elif state == 'ready':
            if isinstance(error,browser.NeedsReview):
                set_state(conn,run_id,'needs_review',str(error))
            else:
                set_state(conn,run_id,'failed','Browser failed before submission; inspect locally')
        raise


def sync_notion(conn, profile, tracker):
    if tracker is None:
        raise ValueError('Configure the persistent Notion connection first')
    rows = conn.execute("""SELECT r.*,j.title,j.url,c.name company_name FROM application_runs r
        JOIN jobs j ON j.id=r.job_id JOIN companies c ON c.id=j.company_id
        WHERE r.user_id=? AND r.state='applied' AND r.notion_state='pending' ORDER BY r.id LIMIT ?""",
        (profile['user_id'],PAGE_SIZE)).fetchall()
    for row in rows:
        existing = tracker.check(row['url'])
        # Commit uncertainty first; a failed Notion write never repeats a submission.
        with conn:
            conn.execute("UPDATE application_runs SET notion_state='unknown' WHERE id=?",(row['id'],))
        job=dict(row)
        approved=conn.execute('''SELECT number FROM recruiter_jobs WHERE job_id=? AND selected=1
            AND json_extract(materials_json,'$.run_id')=? ORDER BY workflow_id DESC LIMIT 1''',(row['job_id'],row['id'])).fetchone()
        if approved:
            job['workflow_number']=approved['number']
        page_id = tracker.record(row,job,existing)
        with conn:
            conn.execute("UPDATE application_runs SET notion_state='synced',notion_page_id=? WHERE id=?",
                         (page_id,row['id']))
    return len(rows)
