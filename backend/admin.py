"""Database-backed administration. All mutations are authorized and audited."""

import json
import re
import secrets
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from flask import Blueprint, current_app, flash, g, redirect, render_template, request, url_for

from backend import accounts, career_sources, matching_tasks, pipeline_skeleton as pipeline, store

PAGES = (('overview', 'Overview'), ('queue', 'Matching queue'), ('email', 'Email and delivery'),
         ('quotas', 'Quotas'), ('users', 'Users'), ('invites', 'Invitations'),
         ('companies', 'Companies'), ('jobs', 'Jobs'), ('quality', 'Score quality'),
         ('audit', 'Audit log'), ('system', 'System'))
IST = timezone(timedelta(hours=5, minutes=30))
PAGE_SIZE = 30


def audit(conn, actor_id, action, target_type, target_id, now, detail='completed'):
    """Store IDs and fixed operational detail, never email, resume or tokens."""
    conn.execute('''INSERT INTO admin_audit_log(actor_id,action,target_type,target_id,detail,created_at)
        VALUES(?,?,?,?,?,?)''', (actor_id, action, target_type, target_id, detail, now.isoformat()))


def known_unavailable(company):
    source = company['career_source']
    return bool(source and (source in career_sources.UNVERIFIED_SOURCES or
                source in career_sources.SOURCES and career_sources.SOURCES[source][1] == 'blocked'))


def user_action(conn, actor_id, user_id, action, now):
    if action not in ('disable', 'enable', 'promote', 'demote', 'signout', 'unlock'):
        raise ValueError('Choose a valid account action.')
    with conn:
        conn.execute('BEGIN IMMEDIATE')
        target = conn.execute('''SELECT a.is_admin,u.active FROM accounts a JOIN users u ON u.id=a.user_id
            WHERE a.user_id=?''', (user_id,)).fetchone()
        if target is None:
            raise ValueError('Account not found.')
        if user_id == actor_id and action in ('disable', 'demote'):
            raise ValueError('Ask another administrator to change your own access.')
        if target['is_admin'] and action in ('disable', 'demote'):
            others = conn.execute('''SELECT COUNT(*) FROM accounts a JOIN users u ON u.id=a.user_id
                WHERE a.is_admin=1 AND a.email_verified=1 AND u.active=1 AND a.user_id<>?''', (user_id,)).fetchone()[0]
            if not others:
                raise ValueError('The last active verified administrator is protected.')
        if action in ('disable', 'enable'):
            conn.execute('UPDATE users SET active=? WHERE id=?', (int(action == 'enable'), user_id))
        if action in ('promote', 'demote'):
            conn.execute('UPDATE accounts SET is_admin=? WHERE user_id=?', (int(action == 'promote'), user_id))
        if action != 'unlock':
            conn.execute('UPDATE accounts SET session_version=session_version+1 WHERE user_id=?', (user_id,))
        else:
            conn.execute('UPDATE accounts SET failed_logins=0,locked_until=0 WHERE user_id=?', (user_id,))
        audit(conn, actor_id, action, 'user', user_id, now)


def issue_verification(conn, user_id, now):
    """Persist before sending. Pending sends cannot be blindly repeated."""
    pipeline.validate_email_configuration()
    stamp = int(now.timestamp())
    with conn:
        conn.execute('BEGIN IMMEDIATE')
        target = conn.execute('''SELECT u.email,a.email_verified FROM users u JOIN accounts a ON a.user_id=u.id
            WHERE u.id=? AND u.active=1''', (user_id,)).fetchone()
        if target is None or target['email_verified']:
            raise ValueError('Choose an active unverified account.')
        previous = conn.execute('SELECT expires_at FROM account_verifications WHERE user_id=?', (user_id,)).fetchone()
        if previous and previous[0] > stamp:
            raise ValueError('A verification is pending. Reconcile delivery before requesting another.')
        token = secrets.token_urlsafe(32)
        conn.execute('''INSERT INTO account_verifications(user_id,token_hash,expires_at,requested_at) VALUES(?,?,?,?)
            ON CONFLICT(user_id) DO UPDATE SET token_hash=excluded.token_hash,expires_at=excluded.expires_at,
            requested_at=excluded.requested_at''',
            (user_id, accounts.token_digest(token), stamp + accounts.SIGNUP_LIFETIME_SECONDS, stamp))
    pipeline.deliver_account_email(target['email'], 'Jobspresso email verification',
        'Open ' + accounts.site_origin() + '/verify-account and enter your email and this single-use code. '
        'It expires in 30 minutes.\n\n' + token)


def overview(conn, now):
    cutoff = (now - timedelta(days=7)).isoformat()
    counts = dict(conn.execute('''SELECT COUNT(*) total,COALESCE(SUM(a.email_verified),0) verified,
        COALESCE(SUM(julianday(a.last_login_at)>=julianday(?)),0) active,
        COALESCE(SUM(length(trim(u.resume_text))>0),0) resumes,
        COALESCE(SUM(EXISTS(SELECT 1 FROM company_subscriptions sub WHERE sub.user_id=u.id)),0) companies,
        COALESCE(SUM(EXISTS(SELECT 1 FROM scores s WHERE s.user_id=u.id)),0) matched
        FROM accounts a JOIN users u ON u.id=a.user_id''', (cutoff,)).fetchone())
    counts['jobs'] = conn.execute('SELECT COUNT(*) FROM jobs WHERE is_active=1').fetchone()[0]
    counts['boards'] = conn.execute('SELECT COUNT(*) FROM companies WHERE active=1').fetchone()[0]
    counts['failed'] = conn.execute("SELECT COUNT(*) FROM matching_tasks WHERE status='failed'").fetchone()[0]
    midnight = datetime.combine(now.astimezone(IST).date(), datetime.min.time(), IST).isoformat()
    counts['scores_today'] = conn.execute('SELECT COUNT(*) FROM scores WHERE julianday(scored_at)>=julianday(?)', (midnight,)).fetchone()[0]
    counts['email_jobs_today'] = conn.execute("SELECT COUNT(*) FROM sent_log WHERE channel='email' AND julianday(sent_at)>=julianday(?)", (midnight,)).fetchone()[0]
    return counts


def register_routes(app, connection, context, now, database, uploads):
    routes = Blueprint('admin', __name__)

    @routes.before_request
    def authorize():
        if not g.account or not g.account['is_admin']:
            return render_template('admin_denied.html', **context()), 403

    def destination(page):
        return redirect(url_for('admin.page', page=page))

    @routes.get('/admin')
    @routes.get('/admin/area/<page>')
    def page(page='overview'):
        if page not in dict(PAGES):
            return render_template('admin_denied.html', **dict(context(), message='Admin page not found.')), 404
        conn = connection()
        try:
            number = store.integer(request.args.get('p', '1'), 1, 1000000)
            query = request.args.get('q', '').strip()
            if len(query) > 200:
                raise ValueError('Search is too long.')
        except ValueError as error:
            return str(error), 400
        stamp = now()
        data = dict(context(), admin_page=page, admin_pages=PAGES, admin_title=dict(PAGES)[page],
                    admin_number=number, admin_query=query, admin_more=False, sections=[], kpis=[],
                    source_options=tuple(career_sources.SOURCES))
        offset = (number - 1) * PAGE_SIZE

        def section(title, columns, sql, params=(), paged=False):
            rows = conn.execute(sql + (' LIMIT ? OFFSET ?' if paged else ' LIMIT 200'),
                                (*params, PAGE_SIZE+1, offset) if paged else params).fetchall()
            if paged:
                data['admin_more'] = len(rows) > PAGE_SIZE
                rows = rows[:PAGE_SIZE]
            data['sections'].append(dict(title=title, columns=columns, rows=[tuple(row) for row in rows]))

        pause = Path(database).parent / 'email_schedule.paused'
        data['mail_paused'] = pause.exists()
        data['mail_pause_at'] = datetime.fromtimestamp(pause.stat().st_mtime, timezone.utc).isoformat() if data['mail_paused'] else None
        if page == 'overview':
            data['overview'] = overview(conn, stamp)
            data['board_check_failures'] = conn.execute('''SELECT COUNT(*) FROM companies c WHERE c.active=1 AND
                (SELECT detail FROM admin_audit_log l WHERE l.target_type='company' AND l.target_id=c.id
                 AND l.action='check_board' ORDER BY l.id DESC LIMIT 1) LIKE 'unavailable%' ''').fetchone()[0]
        elif page == 'users':
            pattern = store.like_pattern(query)
            rows = conn.execute('''SELECT a.user_id,u.name,u.email,u.active,a.email_verified,a.is_admin,
                a.locked_until,a.session_version,a.last_login_at,
                (SELECT COUNT(*) FROM scores s WHERE s.user_id=u.id) scores,
                (SELECT COUNT(*) FROM company_subscriptions sub WHERE sub.user_id=u.id) companies,
                (SELECT COUNT(*) FROM resume_versions r WHERE r.user_id=u.id) resumes,
                (SELECT status FROM matching_tasks t WHERE t.user_id=u.id) task,
                COALESCE(us.cadence,'daily') cadence
                FROM accounts a JOIN users u ON u.id=a.user_id LEFT JOIN user_settings us ON us.user_id=u.id
                WHERE u.name LIKE ? ESCAPE '\\' OR u.email LIKE ? ESCAPE '\\'
                ORDER BY a.user_id LIMIT ? OFFSET ?''', (pattern, pattern, PAGE_SIZE+1, offset)).fetchall()
            data['admin_more'] = len(rows)>PAGE_SIZE
            data['admin_users'] = rows[:PAGE_SIZE]
            data['epoch'] = int(stamp.timestamp())
        elif page == 'queue':
            section('Matching tasks', ('State', 'Count'), 'SELECT status,COUNT(*) FROM matching_tasks GROUP BY status')
            section('Tasks by reason', ('Reason', 'Count'), 'SELECT reason,COUNT(*) FROM matching_tasks GROUP BY reason')
            data['failed_tasks'] = conn.execute("SELECT user_id,reason,last_error,revision,updated_at FROM matching_tasks WHERE status='failed' ORDER BY updated_at LIMIT ? OFFSET ?", (PAGE_SIZE+1,offset)).fetchall()
            data['admin_more'] = len(data['failed_tasks'])>PAGE_SIZE
            data['failed_tasks'] = data['failed_tasks'][:PAGE_SIZE]
            data['stale'] = conn.execute('SELECT COUNT(*) FROM scores WHERE is_stale=1').fetchone()[0]
        elif page == 'companies':
            rows = conn.execute('''SELECT c.*,
                (SELECT COUNT(*) FROM company_subscriptions sub WHERE sub.company_id=c.id) subscribers,
                (SELECT COUNT(*) FROM jobs j WHERE j.company_id=c.id AND j.is_active=1) openings,
                (SELECT MAX(last_seen_at) FROM jobs j WHERE j.company_id=c.id) last_seen
                FROM companies c WHERE c.name LIKE ? ESCAPE '\\' ORDER BY c.name,c.id LIMIT ? OFFSET ?''',
                (store.like_pattern(query),PAGE_SIZE+1,offset)).fetchall()
            data['admin_more'] = len(rows)>PAGE_SIZE
            data['admin_companies'] = [dict(row, unavailable=known_unavailable(row)) for row in rows[:PAGE_SIZE]]
        elif page == 'jobs':
            data['duplicates'] = request.args.get('tab')=='duplicates'
            if data['duplicates']:
                section('Same company and title across sources', ('Company','Title','Copies','Source boards','Job IDs'),
                    '''SELECT MIN(c.name),MIN(j.title),COUNT(*),GROUP_CONCAT(DISTINCT COALESCE(c.career_source,c.ats_type)),GROUP_CONCAT(j.id)
                    FROM jobs j JOIN companies c ON c.id=j.company_id GROUP BY lower(trim(c.name)),lower(trim(j.title))
                    HAVING COUNT(*)>1 ORDER BY COUNT(*) DESC,MIN(j.id)''', paged=True)
            else:
                data['admin_jobs'] = conn.execute('''SELECT j.id,j.title,j.is_active,j.admin_closed,j.first_seen_at,c.name,
                    COALESCE(c.career_source,c.ats_type) source FROM jobs j JOIN companies c ON c.id=j.company_id
                    WHERE j.title LIKE ? ESCAPE '\\' OR c.name LIKE ? ESCAPE '\\'
                    ORDER BY j.id DESC LIMIT ? OFFSET ?''', (store.like_pattern(query),store.like_pattern(query),PAGE_SIZE+1,offset)).fetchall()
                data['admin_more'] = len(data['admin_jobs'])>PAGE_SIZE
                data['admin_jobs'] = data['admin_jobs'][:PAGE_SIZE]
        elif page == 'invites':
            data['invites'] = conn.execute('''SELECT rowid id,email,is_admin,created_at,expires_at,
                CASE WHEN consumed_at IS NOT NULL THEN 'Used or revoked' WHEN expires_at<=? THEN 'Expired' ELSE 'Pending' END status
                FROM account_tokens WHERE purpose='invite' ORDER BY created_at DESC,rowid DESC LIMIT ? OFFSET ?''',
                (int(stamp.timestamp()),PAGE_SIZE+1,offset)).fetchall()
            data['admin_more']=len(data['invites'])>PAGE_SIZE
            data['invites']=data['invites'][:PAGE_SIZE]
        elif page == 'email':
            section('Email preferences', ('Cadence','Users'), "SELECT COALESCE(us.cadence,'daily'),COUNT(*) FROM accounts a LEFT JOIN user_settings us ON us.user_id=a.user_id GROUP BY COALESCE(us.cadence,'daily')")
            section('Job email history (not digest counts)', ('Day (IST)','Job records'), "SELECT date(sent_at,'+330 minutes'),COUNT(*) FROM sent_log WHERE channel='email' GROUP BY 1 ORDER BY 1 DESC")
            section('Onboarding reminders', ('Sent','Unconfirmed attempts'), 'SELECT SUM(sent_at IS NOT NULL),SUM(sent_at IS NULL) FROM onboarding_reminders')
            section('No-match reminders', ('User ID','Sent count','Last sent'), 'SELECT user_id,sent_count,last_reminder_at FROM match_reminders ORDER BY last_reminder_at DESC')
        elif page == 'quality':
            section('Score ratings by score band', ('Band','Helpful','Not accurate'), '''SELECT CASE WHEN fit_score IS NULL THEN 'Historical score unavailable'
                WHEN fit_score>=80 THEN 'Strong (80+)' WHEN fit_score>=60 THEN 'Good (60–79)' ELSE 'Stretch (<60)' END band,
                SUM(rating=1),SUM(rating=-1) FROM score_feedback GROUP BY band''')
            section('Score ratings by company', ('Company','Helpful','Not accurate'), '''SELECT c.name,SUM(f.rating=1),SUM(f.rating=-1)
                FROM score_feedback f JOIN jobs j ON j.id=f.job_id JOIN companies c ON c.id=j.company_id GROUP BY c.id ORDER BY COUNT(*) DESC''')
            section('Latest score ratings', ('Company','Job','Rating','Recorded'), '''SELECT c.name,j.title,
                CASE WHEN f.rating=1 THEN 'Helpful' ELSE 'Not accurate' END,f.created_at FROM score_feedback f
                JOIN jobs j ON j.id=f.job_id JOIN companies c ON c.id=j.company_id ORDER BY f.id DESC''')
            section('Student survey', ('Responses','Accuracy / 5','Ease / 5','Would use: yes'), "SELECT COUNT(*),ROUND(AVG(accuracy),1),ROUND(AVG(ease),1),SUM(would_use='yes') FROM feedback")
        elif page == 'quotas':
            limits = conn.execute('SELECT key_slot,model,metrics_json,updated_at FROM groq_limits ORDER BY key_slot').fetchall()
            data['groq_limits'] = [dict(slot=row['key_slot'], model=row['model'], at=row['updated_at'],
                requests=json.loads(row['metrics_json']).get('remaining_requests'),
                tokens=json.loads(row['metrics_json']).get('remaining_tokens')) for row in limits]
            section('Tavily budget', ('Day','Daily used / 33','Month','Monthly used / 1000'), 'SELECT day,daily_used,month,monthly_used FROM tavily_budget')
            section('Discovery outcomes', ('Platform','Status','Searches','Verified','Rejected','Saved'), 'SELECT platform,status,COUNT(*),SUM(verified),SUM(rejected),SUM(saved) FROM tavily_searches GROUP BY platform,status')
            section('Worker heartbeats', ('User ID','Worker','State','Code','Updated'), 'SELECT user_id,worker,state,code,updated_at FROM worker_pulses ORDER BY updated_at DESC')
        elif page == 'audit':
            section('Admin actions', ('At (UTC)','Actor ID','Action','Target type','Target ID','Result'),
                'SELECT created_at,actor_id,action,target_type,target_id,detail FROM admin_audit_log ORDER BY id DESC', paged=True)
        elif page == 'system':
            version = conn.execute('SELECT name FROM schema_migrations ORDER BY name DESC LIMIT 1').fetchone()
            data['kpis'] = [('Migration',version[0] if version else 'Unavailable'), ('Database bytes',Path(database).stat().st_size),
                ('Current failed sign-in counters',conn.execute('SELECT COALESCE(SUM(failed_logins),0) FROM accounts').fetchone()[0]),
                ('Currently locked accounts',conn.execute('SELECT COUNT(*) FROM accounts WHERE locked_until>?',(int(stamp.timestamp()),)).fetchone()[0])]
            data['system_note'] = 'Historical sign-in failures and backup verification are unavailable. Current counters are not 24-hour or 7-day history.'
        data['admin_previous_url']=url_for('admin.page',page=page,p=max(1,number-1),q=query,tab=request.args.get('tab',''))
        data['admin_next_url']=url_for('admin.page',page=page,p=number+1,q=query,tab=request.args.get('tab',''))
        return render_template('admin.html', **data)

    @routes.post('/admin/users/<int:user_id>/action')
    def account_action(user_id):
        action = request.form.get('action','')
        try:
            user_action(connection(),g.user['id'],user_id,action,now())
        except ValueError as error:
            flash(str(error))
        else:
            flash('Account action completed.')
        return destination('users')

    @routes.post('/admin/users/<int:user_id>/email')
    def account_email(user_id):
        action=request.form.get('action','')
        if action not in ('verify','reset') or request.form.get('confirm_send')!='yes':
            return 'Confirm the email action before sending.',400
        conn=connection()
        target=conn.execute('''SELECT u.email,a.recovery_requested_at FROM users u JOIN accounts a ON a.user_id=u.id
            WHERE u.id=? AND u.active=1''',(user_id,)).fetchone()
        if target is None:
            return 'Active account not found.',404
        with conn:
            audit(conn,g.user['id'],action+'_email','user',user_id,now(),'requested; reconcile delivery before retry')
        try:
            if action=='verify':
                issue_verification(conn,user_id,now())
            else:
                accounts.issue_recovery(conn,target['email'],now())
                after=conn.execute('SELECT recovery_requested_at FROM accounts WHERE user_id=?',(user_id,)).fetchone()[0]
                if after==target['recovery_requested_at']:
                    raise ValueError('No new recovery email: account is unverified or the cooldown is active.')
        except ValueError as error:
            with conn:
                audit(conn,g.user['id'],action+'_email','user',user_id,now(),'not sent; validation or cooldown')
            flash(str(error))
        except (OSError,RuntimeError) as error:
            current_app.logger.error('Admin account email unconfirmed (%s)',type(error).__name__)
            with conn:
                audit(conn,g.user['id'],action+'_email','user',user_id,now(),'delivery unconfirmed; reconcile provider')
            flash('Delivery is unconfirmed. Check Gmail Sent before trying again.')
        else:
            with conn:
                audit(conn,g.user['id'],action+'_email','user',user_id,now(),'accepted by email provider')
            flash('Email accepted by the provider. Inbox delivery is not confirmed.')
        return destination('users')

    @routes.post('/admin/queue/action')
    def queue_action():
        action=request.form.get('action','')
        if action not in ('one','failed','stale'):
            return 'Invalid queue action.',400
        try:
            uid=store.integer(request.form.get('user_id','0'),0,2147483647)
        except ValueError as error:
            return str(error),400
        conn=connection()
        with conn:
            conn.execute('BEGIN IMMEDIATE')
            if action=='stale':
                users=conn.execute('''SELECT DISTINCT s.user_id FROM scores s JOIN users u ON u.id=s.user_id
                    JOIN accounts a ON a.user_id=u.id WHERE s.is_stale=1 AND u.active=1 AND a.email_verified=1
                    AND NOT EXISTS(SELECT 1 FROM matching_tasks t WHERE t.user_id=u.id AND t.status='running')
                    ORDER BY s.user_id LIMIT 100''').fetchall()
            else:
                users=conn.execute('''SELECT t.user_id FROM matching_tasks t JOIN users u ON u.id=t.user_id
                    JOIN accounts a ON a.user_id=u.id WHERE t.status='failed' AND u.active=1 AND a.email_verified=1
                    AND (?=0 OR t.user_id=?) ORDER BY t.user_id LIMIT 100''', (uid if action=='one' else 0,uid)).fetchall()
            for user in users:
                matching_tasks.enqueue(conn,user[0],'backlog',now())
                audit(conn,g.user['id'],'requeue_'+action,'user',user[0],now())
        flash(f'Queued {len(users)} users (up to 100 per action). Running tasks are preserved; the worker performs scoring.')
        return destination('queue')

    @routes.post('/admin/jobs/<int:job_id>/action')
    def job_action(job_id):
        action=request.form.get('action','')
        if action not in ('close','reopen'):
            return 'Invalid posting action.',400
        conn=connection()
        with conn:
            conn.execute('BEGIN IMMEDIATE')
            row=conn.execute('SELECT company_id FROM jobs WHERE id=?',(job_id,)).fetchone()
            if row is None:
                return 'Posting not found.',404
            conn.execute('UPDATE jobs SET is_active=?,admin_closed=? WHERE id=?',(int(action=='reopen'),int(action=='close'),job_id))
            audit(conn,g.user['id'],action+'_job','job',job_id,now())
        flash('Posting updated. Manual closures remain closed across source refreshes.')
        return destination('jobs')

    @routes.post('/admin/jobs/<int:job_id>/deduplicate')
    def deduplicate_jobs(job_id):
        if request.form.get('confirm_close')!='yes':
            return 'Confirm closing the older copies.',400
        conn=connection()
        with conn:
            conn.execute('BEGIN IMMEDIATE')
            target=conn.execute('SELECT lower(trim(c.name)),lower(trim(j.title)) FROM jobs j JOIN companies c ON c.id=j.company_id WHERE j.id=?',(job_id,)).fetchone()
            if target is None:
                return 'Posting not found.',404
            copies=conn.execute('''SELECT j.id FROM jobs j JOIN companies c ON c.id=j.company_id
                WHERE lower(trim(c.name))=? AND lower(trim(j.title))=? AND j.is_active=1
                ORDER BY julianday(j.first_seen_at) DESC,j.id DESC LIMIT 101''',tuple(target)).fetchall()
            if len(copies)>100:
                return 'Review this group individually; it contains more than 100 open copies.',400
            for copy in copies[1:]:
                conn.execute('UPDATE jobs SET is_active=0,admin_closed=1 WHERE id=?',(copy[0],))
                audit(conn,g.user['id'],'close_duplicate','job',copy[0],now(),'kept newest open copy by first-seen date')
        flash(f'Closed {max(0,len(copies)-1)} older copies; the newest open copy was retained.')
        return redirect(url_for('admin.page',page='jobs',tab='duplicates'))

    @routes.post('/admin/companies/save')
    def company_save():
        try:
            cid=store.integer(request.form.get('company_id','0'),0,2147483647)
            name=request.form.get('name','').strip()
            ats=request.form.get('ats_type','')
            slug=request.form.get('board_slug','').strip()
            source=request.form.get('career_source','') or None
            active=int(request.form.get('active')=='on')
            discovery=int(request.form.get('discovery_only')=='on')
            if not 1<=len(name)<=200 or ats not in ('greenhouse','lever','ashby'):
                raise ValueError('Provide a company name and supported ATS type.')
            if not re.fullmatch(r'[A-Za-z0-9_-]{1,150}',slug):
                raise ValueError('Use a board identifier, not a URL.')
            if source and source not in career_sources.SOURCES:
                raise ValueError('Choose a registered source adapter.')
            if source and slug!='career-site-'+source:
                raise ValueError('Career source boards must use career-site- followed by the registered source key.')
            if active and source and (source in career_sources.UNVERIFIED_SOURCES or career_sources.SOURCES[source][1]=='blocked'):
                raise ValueError('Unavailable or unverified boards cannot be enabled.')
            if source is None and slug.startswith('career-site-'):
                raise ValueError('Choose the registered career source for this board.')
            conn=connection()
            with conn:
                conn.execute('BEGIN IMMEDIATE')
                if cid:
                    previous=conn.execute('SELECT * FROM companies WHERE id=?',(cid,)).fetchone()
                    if previous is None:
                        raise ValueError('Company not found.')
                    # Source identities own external job IDs and must not be repointed.
                    if (ats,slug,source)!=(previous['ats_type'],previous['board_slug'],previous['career_source']):
                        raise ValueError('Create a new board to change its source identity; existing jobs retain their source.')
                    conn.execute('UPDATE companies SET name=?,active=?,discovery_only=? WHERE id=?',(name,active,discovery,cid))
                else:
                    cid=conn.execute('INSERT INTO companies(name,ats_type,board_slug,career_source,active,discovery_only) VALUES(?,?,?,?,?,?)',
                        (name,ats,slug,source,active,discovery)).lastrowid
                audit(conn,g.user['id'],'company_save','company',cid,now())
        except ValueError as error:
            flash(str(error))
        except sqlite3.IntegrityError:
            flash('That board already exists or its source configuration is invalid.')
        else:
            flash('Company saved. No external fetch was performed.')
        return destination('companies')

    @routes.post('/admin/companies/<int:company_id>/check')
    def company_check(company_id):
        if request.form.get('confirm_check')!='yes':
            return 'Confirm the read-only external check.',400
        conn=connection()
        company=conn.execute('SELECT * FROM companies WHERE id=?',(company_id,)).fetchone()
        if company is None:
            return 'Company not found.',404
        started=time.monotonic()
        with conn:
            audit(conn,g.user['id'],'check_board','company',company_id,now(),'requested; read-only external check')
        try:
            source=company['career_source']
            if source and source in career_sources.SOURCES and career_sources.SOURCES[source][1]=='blocked':
                raise ValueError('Known unavailable source.')
            if source:
                jobs=career_sources.fetch(source,started+30)
            else:
                if not re.fullmatch(r'[A-Za-z0-9_-]{1,150}',company['board_slug']):
                    raise ValueError('Invalid stored board identifier.')
                jobs=pipeline.fetch_jobs_for_company(company)
            message=f'Board reachable: {len(jobs)} returned postings. No jobs were imported.'
            result='reachable; jobs='+str(len(jobs))
        except (requests.RequestException,ValueError,KeyError,TypeError,IndexError) as error:
            current_app.logger.warning('Admin board check failed (company_id=%s error=%s)',company_id,type(error).__name__)
            message='Board check failed or is unavailable. Saved jobs were preserved.'
            result='unavailable; '+type(error).__name__
        with conn:
            audit(conn,g.user['id'],'check_board','company',company_id,now(),result)
        flash(message)
        return destination('companies')

    @routes.post('/admin/invites/action')
    def invitation_action():
        action=request.form.get('action','')
        conn=connection()
        if action=='revoke':
            try:
                iid=store.integer(request.form.get('invitation_id',''),1,2147483647)
            except ValueError as error:
                return str(error),400
            with conn:
                conn.execute('BEGIN IMMEDIATE')
                count=conn.execute("UPDATE account_tokens SET consumed_at=? WHERE rowid=? AND purpose='invite' AND consumed_at IS NULL",
                    (int(now().timestamp()),iid)).rowcount
                if count:
                    audit(conn,g.user['id'],'invite_revoke','invitation',iid,now())
            flash('Invitation revoked.' if count else 'No pending invitation found.')
        elif action in ('send','resend') and request.form.get('confirm_send')=='yes':
            try:
                email=accounts.email_address(request.form.get('email',''))
                # Existing helper rejects pending invitations, protecting uncertain sends.
                with conn:
                    audit(conn,g.user['id'],'invite_send','invitation',None,now(),'requested; delivery must be reconciled')
                accounts.issue_invitation(conn,email,now())
            except ValueError as error:
                flash(str(error))
            except (OSError,RuntimeError) as error:
                current_app.logger.error('Admin invitation unconfirmed (%s)',type(error).__name__)
                flash('Invitation delivery is unconfirmed. Reconcile Gmail Sent before retrying.')
            else:
                with conn:
                    iid=conn.execute("SELECT rowid FROM account_tokens WHERE email=? AND purpose='invite' ORDER BY created_at DESC,rowid DESC LIMIT 1",(email,)).fetchone()[0]
                    audit(conn,g.user['id'],'invite_send','invitation',iid,now(),'accepted by email provider')
                flash('Invitation accepted by email provider.')
        else:
            return 'Choose an action and confirm email sending.',400
        return destination('invites')

    app.register_blueprint(routes)
