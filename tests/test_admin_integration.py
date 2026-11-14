"""Offline integration coverage for the ZIP UI and administrative boundaries."""

import sqlite3
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from backend import admin, accounts, pipeline_skeleton as pipeline, scoring_feedback, store, tracker_analytics
from backend.migrate import apply_migrations
from tests import test_web_flows

NOW = datetime(2026, 11, 14, 8, tzinfo=timezone.utc)


class AdminIntegrationTests(unittest.TestCase):
    seed_user = test_web_flows.WebFlowTests.seed_user
    post = test_web_flows.WebFlowTests.post

    def setUp(self):
        test_web_flows.WebFlowTests.setUp(self)
        self.seed_user()
        self.conn.execute('UPDATE accounts SET is_admin=1 WHERE user_id=1')
        self.conn.commit()
        clock=patch('backend.app.utc_now',return_value=NOW)
        clock.start()
        self.addCleanup(clock.stop)

    def second_user(self, admin_role=0):
        self.conn.execute("INSERT INTO users(id,name,email,resume_text) VALUES(2,'Student','admin-prefix@example.test','Private second resume')")
        self.conn.execute("INSERT INTO accounts(user_id,password_hash,is_admin,created_at) VALUES(2,'test-only',?,'2026-10-01T00:00:00+00:00')",(admin_role,))
        self.conn.commit()

    def test_all_eleven_pages_render_real_data(self):
        for page,_ in admin.PAGES:
            with self.subTest(page=page), self.client.get('/admin/area/'+page) as response:
                self.assertEqual(response.status_code,200)
                self.assertNotIn(b'Python resume',response.data)
                self.assertNotIn(b'Kaveri Pay',response.data)
                self.assertIn(b'Audit log',response.data)

    def test_student_email_prefix_never_grants_admin(self):
        self.second_user()
        with self.client.session_transaction() as session:
            session['user_id']=2
        for page,_ in admin.PAGES:
            with self.client.get('/admin/area/'+page) as response:
                self.assertEqual(response.status_code,403)
                self.assertIn(b'Admins only',response.data)
        with self.client.get('/settings') as response:
            self.assertNotIn(b'>Admin</a>',response.data)
        response=self.post('/admin/users/1/action',dict(action='disable'))
        self.assertEqual(response.status_code,403)
        self.assertEqual(self.conn.execute('SELECT active FROM users WHERE id=1').fetchone()[0],1)

    def test_actions_require_csrf(self):
        response=self.client.post('/admin/users/1/action',data={'action':'signout'})
        self.assertEqual(response.status_code,403)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM admin_audit_log').fetchone()[0],0)

    def test_user_actions_invalidate_sessions_and_audit(self):
        self.second_user()
        for action in ('disable','enable','promote','demote','signout','unlock'):
            response=self.post('/admin/users/2/action',dict(action=action))
            self.assertEqual(response.status_code,302)
        row=self.conn.execute('SELECT session_version,is_admin FROM accounts WHERE user_id=2').fetchone()
        self.assertEqual(tuple(row),(5,0))
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM admin_audit_log').fetchone()[0],6)

    def test_last_usable_admin_protected(self):
        self.second_user(admin_role=1)
        self.conn.execute('UPDATE users SET active=0 WHERE id=2')
        self.conn.commit()
        for action in ('disable','demote'):
            with self.assertRaises(ValueError):
                admin.user_action(self.conn,2,1,action,NOW)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM admin_audit_log').fetchone()[0],0)

    def test_invalid_actions_leave_data_unchanged(self):
        self.assertEqual(self.post('/admin/jobs/1/action',dict(action='drop')).status_code,400)
        self.assertEqual(self.post('/admin/queue/action',dict(action='drop')).status_code,400)
        self.assertEqual(self.post('/admin/companies/1/check',{}).status_code,400)
        self.assertEqual(self.post('/admin/users/1/email',dict(action='reset')).status_code,400)

    def test_manual_job_closure_survives_ingestion(self):
        self.post('/admin/jobs/1/action',dict(action='close'))
        pipeline.upsert_jobs(self.conn,1,[dict(external_id='1',title='Updated role',raw_json={'content':'Python job'},url='https://example.test/apply')])
        self.assertEqual(tuple(self.conn.execute('SELECT is_active,admin_closed FROM jobs WHERE id=1').fetchone()),(0,1))
        self.post('/admin/jobs/1/action',dict(action='reopen'))
        self.assertEqual(self.conn.execute('SELECT is_active FROM jobs WHERE id=1').fetchone()[0],1)

    def test_board_check_is_read_only_and_failure_is_audited(self):
        before=self.conn.execute('SELECT COUNT(*) FROM jobs').fetchone()[0]
        with patch.object(pipeline,'fetch_jobs_for_company',return_value=[{}]):
            self.assertEqual(self.post('/admin/companies/1/check',dict(confirm_check='yes')).status_code,302)
        with patch.object(pipeline,'fetch_jobs_for_company',side_effect=ValueError('private response')):
            self.post('/admin/companies/1/check',dict(confirm_check='yes'))
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM jobs').fetchone()[0],before)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM admin_audit_log').fetchone()[0],4)
        self.assertNotIn('private response',str(self.conn.execute('SELECT detail FROM admin_audit_log').fetchall()))

    def test_only_my_companies_filter_is_user_scoped(self):
        self.conn.execute("INSERT INTO company_subscriptions VALUES(1,1,?)",(NOW.isoformat(),))
        self.conn.commit()
        rows,_=store.matches(self.conn,1,store.filters({'only_mine':'1'}),NOW)
        self.assertEqual([row['job_id'] for row in rows],[1])
        self.assertEqual(self.client.get('/jobs?only_mine=1&job=2').status_code,404)
        self.assertEqual(self.client.get('/jobs?only_mine=2').status_code,400)

    def test_queue_requeues_failed_not_running(self):
        self.second_user()
        with self.conn:
            self.conn.execute("INSERT INTO matching_tasks(user_id,reason,status,updated_at) VALUES(1,'resume','failed',?)",(NOW.isoformat(),))
            self.conn.execute("INSERT INTO matching_tasks(user_id,reason,status,updated_at) VALUES(2,'resume','running',?)",(NOW.isoformat(),))
        self.post('/admin/queue/action',dict(action='failed'))
        self.assertEqual(self.conn.execute('SELECT status FROM matching_tasks WHERE user_id=1').fetchone()[0],'pending')
        self.assertEqual(self.conn.execute('SELECT status FROM matching_tasks WHERE user_id=2').fetchone()[0],'running')

    def application(self, days, status='applied'):
        with self.conn:
            store.save_application(self.conn,1,dict(title='Role',company_name='Alpha',status=status),NOW-timedelta(days=days))
        return self.conn.execute('SELECT MAX(id) FROM applications').fetchone()[0]

    def transition(self, aid, status, days=0):
        with self.conn:
            store.save_application(self.conn,1,dict(application_id=str(aid),status=status),NOW-timedelta(days=days))

    def test_response_age_boundary_small_sample_and_saved_exclusion(self):
        self.application(14)
        self.application(3)
        self.application(30,'saved')
        aid=self.application(3)
        self.transition(aid,'rejected',1)
        o=tracker_analytics.snapshot(self.conn,1,NOW)['outcomes']
        self.assertEqual((o['eligible'],o['responses'],o['interviews'],o['median_days']),(2,1,0,2.0))
        with self.client.get('/analytics') as response:
            self.assertEqual(response.status_code,200)
            self.assertIn(b'1 of 2 replied',response.data)

    def test_interview_history_survives_later_rejection_and_notes_do_not_add_events(self):
        aid=self.application(20)
        self.transition(aid,'screen',15)
        self.transition(aid,'assessment',12)
        self.transition(aid,'interview',10)
        self.transition(aid,'rejected',2)
        self.transition(aid,'rejected',1)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM application_events WHERE application_id=?',(aid,)).fetchone()[0],5)
        o=tracker_analytics.snapshot(self.conn,1,NOW)['outcomes']
        self.assertEqual((o['responses'],o['interviews'],o['median_days']),(1,1,5.0))

    def test_no_reply_and_empty_outcomes(self):
        self.assertIsNone(tracker_analytics.snapshot(self.conn,1,NOW)['outcomes']['response_rate'])
        self.application(20,'no_reply')
        o=tracker_analytics.snapshot(self.conn,1,NOW)['outcomes']
        self.assertEqual((o['eligible'],o['responses'],o['median_days']),(1,0,None))

    def test_legacy_reply_dates_excluded_from_median(self):
        aid=self.application(20,'interview')
        with self.conn:
            self.conn.execute('UPDATE application_events SET date_known=0 WHERE application_id=?',(aid,))
        self.transition(aid,'offer')
        o=tracker_analytics.snapshot(self.conn,1,NOW)['outcomes']
        self.assertEqual(o['responses'],1)
        self.assertIsNone(o['median_days'])

    def test_ist_first_strong_once_despite_rescore_and_closure(self):
        # Sunday 18:30 UTC is Monday midnight in IST.
        first='2026-11-08T18:30:00+00:00'
        with self.conn:
            self.conn.execute('DELETE FROM strong_match_firsts')
            self.conn.execute('UPDATE scores SET fit_score=80,scored_at=? WHERE job_id=2',(first,))
            self.conn.execute("UPDATE scores SET fit_score=50,scored_at='2026-11-10T00:00:00+00:00' WHERE job_id=2")
            self.conn.execute("UPDATE scores SET fit_score=95,scored_at='2026-11-12T00:00:00+00:00' WHERE job_id=2")
            self.conn.execute('UPDATE jobs SET is_active=0 WHERE id=2')
            store.save_application(self.conn,1,dict(job_id='2',status='applied'),NOW)
        result=tracker_analytics.snapshot(self.conn,1,NOW)
        self.assertEqual((result['strong_weeks'][-1]['week'],result['strong_weeks'][-1]['count'],result['strong_weeks'][-1]['applied']),('2026-11-09',1,1))
        self.assertEqual(self.conn.execute('SELECT match_first_at FROM strong_match_firsts WHERE job_id=2').fetchone()[0],first)

    def test_admin_pagination_preserves_search(self):
        for uid in range(2,34):
            self.conn.execute('INSERT INTO users(id,name,email,resume_text) VALUES(?,?,?,?)',(uid,'Synthetic',f'synthetic{uid}@example.test',''))
            self.conn.execute('INSERT INTO accounts(user_id,password_hash,created_at) VALUES(?,?,?)',(uid,'test-only',NOW.isoformat()))
        self.conn.commit()
        response=self.client.get('/admin/area/users?q=Synthetic')
        self.assertEqual(response.status_code,200)
        self.assertIn(b'p=2',response.data)

    def test_migration_preserves_applications_and_followups_with_foreign_keys(self):
        conn=sqlite3.connect(':memory:')
        self.addCleanup(conn.close)
        conn.execute('PRAGMA foreign_keys=ON')
        conn.executescript(pipeline.SCHEMA)
        paths=list((Path(admin.__file__).parent/'migrations').glob('*.sql'))
        with patch('backend.migrate.Path.glob',return_value=[p for p in paths if p.name<'033']):
            apply_migrations(conn)
        with conn:
            conn.execute("INSERT INTO users(id,email,resume_text) VALUES(1,'synthetic@example.test','')")
            conn.execute("INSERT INTO applications(id,user_id,title,company_name,status,created_at,updated_at) VALUES(7,1,'Role','Alpha','interview',?,?)",(NOW.isoformat(),NOW.isoformat()))
            conn.execute("INSERT INTO recruiter_followups VALUES(7,1,'2026-11-15',?)",(NOW.isoformat(),))
        apply_migrations(conn)
        self.assertEqual(conn.execute('SELECT id,status FROM applications').fetchone(),(7,'interview'))
        self.assertEqual(conn.execute('SELECT application_id FROM recruiter_followups').fetchone()[0],7)
        self.assertEqual(conn.execute('PRAGMA foreign_key_check').fetchall(),[])
        self.assertEqual(conn.execute('SELECT date_known FROM application_events').fetchone()[0],0)
        apply_migrations(conn)
        self.assertEqual(conn.execute('SELECT COUNT(*) FROM application_events').fetchone()[0],1)

    def test_repeated_score_upserts_keep_first_strong(self):
        first=self.conn.execute('SELECT match_first_at FROM strong_match_firsts WHERE job_id=1').fetchone()[0]
        pipeline.store_score(self.conn,1,1,dict(fit_score=95,reasoning='Updated'))
        pipeline.store_score(self.conn,1,1,dict(fit_score=20,reasoning='Updated'))
        pipeline.store_score(self.conn,1,1,dict(fit_score=80,reasoning='Updated'))
        self.assertEqual(self.conn.execute('SELECT match_first_at FROM strong_match_firsts WHERE job_id=1').fetchone()[0],first)

    def test_deletion_requires_email_and_preserves_audit_without_personal_data(self):
        self.second_user()
        self.post('/admin/users/2/delete',dict(confirm_email='wrong@example.test'))
        self.assertIsNotNone(self.conn.execute('SELECT 1 FROM users WHERE id=2').fetchone())
        self.post('/admin/users/2/delete',dict(confirm_email='admin-prefix@example.test'))
        self.assertIsNone(self.conn.execute('SELECT 1 FROM users WHERE id=2').fetchone())
        row=self.conn.execute("SELECT actor_id,target_id,detail FROM admin_audit_log WHERE action='delete_user'").fetchone()
        self.assertEqual((row[0],row[1]),(1,2))
        self.assertNotIn('@',row[2])

    def test_company_validation_prevents_urls_and_known_unavailable_activation(self):
        original=self.conn.execute('SELECT COUNT(*) FROM companies').fetchone()[0]
        for slug,source in [('https://evil.example/path',''),('career-site-icici','icici')]:
            self.post('/admin/companies/save',dict(name='New',ats_type='lever',board_slug=slug,career_source=source,active='on'))
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM companies').fetchone()[0],original)
        self.post('/admin/companies/save',dict(name='New',ats_type='lever',board_slug='synthetic-new',active='on'))
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM companies').fetchone()[0],original+1)

    def test_verification_email_and_single_use_redemption(self):
        self.second_user()
        with self.conn:
            self.conn.execute('UPDATE accounts SET email_verified=0 WHERE user_id=2')
        with patch.object(pipeline,'validate_email_configuration'), patch.object(pipeline,'deliver_account_email') as delivery:
            self.post('/admin/users/2/email',dict(action='verify',confirm_send='yes'))
            self.assertEqual(delivery.call_count,1)
            code=delivery.call_args.args[2].split('\n\n')[-1]
            self.post('/admin/users/2/email',dict(action='verify',confirm_send='yes'))
            self.assertEqual(delivery.call_count,1)
        visitor=self.app.test_client()
        visitor.get('/verify-account').close()
        with visitor.session_transaction() as session:
            csrf=session['csrf']
        for expected in (302,400):
            with visitor.post('/verify-account',data=dict(email='admin-prefix@example.test',code=code,csrf_token=csrf)) as response:
                self.assertEqual(response.status_code,expected)
        self.assertEqual(self.conn.execute('SELECT email_verified FROM accounts WHERE user_id=2').fetchone()[0],1)

    def test_reset_email_mocked_and_failure_does_not_report_success(self):
        self.second_user()
        with patch.object(pipeline,'validate_email_configuration'), patch.object(pipeline,'deliver_account_email',side_effect=RuntimeError('private provider detail')) as delivery:
            self.post('/admin/users/2/email',dict(action='reset',confirm_send='yes'))
            self.assertEqual(delivery.call_count,1)
        row=self.conn.execute('SELECT detail FROM admin_audit_log ORDER BY id DESC LIMIT 1').fetchone()[0]
        self.assertIn('unconfirmed',row)
        self.assertNotIn('private provider',row)

    def test_invitations_pending_send_guard_revoke_and_resend(self):
        with patch.object(pipeline,'validate_email_configuration'), patch.object(pipeline,'deliver_account_email') as delivery:
            form=dict(action='send',email='new@example.test',confirm_send='yes')
            self.post('/admin/invites/action',form)
            self.post('/admin/invites/action',form)
            self.assertEqual(delivery.call_count,1)
            iid=self.conn.execute("SELECT rowid FROM account_tokens WHERE purpose='invite'").fetchone()[0]
            self.post('/admin/invites/action',dict(action='revoke',invitation_id=str(iid)))
            self.post('/admin/invites/action',dict(form,action='resend'))
            self.assertEqual(delivery.call_count,2)

    def test_score_rating_snapshots_actual_score_for_quality_band(self):
        row=self.conn.execute('SELECT id,fit_score,reasoning,matched_skills,missing_requirements,evaluation_json,deterministic_json,scored_at FROM scores WHERE job_id=1').fetchone()
        self.post('/jobs/score-feedback',dict(job_id='1',score_signature=scoring_feedback.score_signature(row),rating='-1'))
        self.assertEqual(self.conn.execute('SELECT fit_score FROM score_feedback').fetchone()[0],85)
        response=self.client.get('/admin/area/quality')
        self.assertIn(b'Strong (80+)',response.data)

    def test_duplicates_close_older_open_copy_and_audit(self):
        with self.conn:
            self.conn.execute("UPDATE companies SET name='Alpha' WHERE id=2")
            self.conn.execute("UPDATE jobs SET title='Same role' WHERE id IN (1,2)")
            self.conn.execute("UPDATE jobs SET first_seen_at='2026-11-12T00:00:00+00:00' WHERE id=2")
        self.assertEqual(self.client.get('/admin/area/jobs?tab=duplicates').status_code,200)
        self.assertEqual(self.post('/admin/jobs/1/deduplicate',dict(confirm_close='yes')).status_code,302)
        self.assertEqual([row[0] for row in self.conn.execute('SELECT id FROM jobs WHERE is_active=1')],[2])
        self.assertEqual(self.conn.execute("SELECT target_id FROM admin_audit_log WHERE action='close_duplicate'").fetchone()[0],1)

    def test_response_percentages_and_even_median(self):
        ids=[self.application(20) for _ in range(5)]
        self.transition(ids[0],'assessment',18)
        self.transition(ids[1],'offer',16)
        o=tracker_analytics.snapshot(self.conn,1,NOW)['outcomes']
        self.assertEqual((o['response_rate'],o['interview_rate'],o['median_days']),(40.0,20.0,3.0))
        response=self.client.get('/analytics')
        self.assertIn(b'40.0%',response.data)
        self.assertIn(b'20.0%',response.data)


if __name__=='__main__':
    unittest.main()
