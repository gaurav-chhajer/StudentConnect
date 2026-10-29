"""Monitor permissions, truthful counts, safe telemetry and rendering."""

import json
import sqlite3
import unittest
from datetime import datetime,timedelta,timezone
from unittest.mock import patch

import requests

from backend import workflow_monitor as monitor
from tests import test_web_flows


class WorkflowMonitorTests(unittest.TestCase):
    def setUp(self):
        test_web_flows.WebFlowTests.setUp(self)
        test_web_flows.WebFlowTests.seed_user(self)
        self.conn.execute('UPDATE accounts SET is_admin=1 WHERE user_id=1')
        self.conn.execute("INSERT INTO users(id,name,email,resume_text) VALUES(2,'Other','other@example.test','Other resume')")
        self.conn.execute("INSERT INTO accounts(user_id,password_hash,is_admin,created_at) VALUES(2,'fixture',1,'2026-01-01')")
        for user_id in (1,2):
            self.conn.execute('''INSERT INTO recruiter_workflows(id,user_id,resume_hash,state,analysis_json,created_at,updated_at)
                VALUES(?,?,'fixture','materials_ready','{}','2026-10-29','2026-10-29')''',(user_id,user_id))
        self.conn.execute('''INSERT INTO recruiter_jobs(workflow_id,number,posting_json,facts_json,evaluation_json,materials_json)
            VALUES(1,1,?,'{}',?,?)''',(json.dumps({'title':'Python Engineer','company_name':'Test company',
            'url':'https://jobs.lever.co/test/one','source':'Lever','verified_at':'2026-10-29T00:00:00+00:00'}),
            json.dumps({'fit_score':75}),json.dumps({'final_score':85,'resume_changed':True})))
        self.conn.commit()

    def test_monitor_requires_login_and_admin_and_filters_other_owners(self):
        response=self.app.test_client().get('/admin/workflow-monitor')
        self.assertEqual(response.status_code,302)
        self.conn.execute('UPDATE accounts SET is_admin=0 WHERE user_id=1')
        self.conn.commit()
        response=self.client.get('/admin/workflow-monitor')
        self.assertEqual(response.status_code,403)
        self.conn.execute('UPDATE accounts SET is_admin=1 WHERE user_id=1')
        self.conn.commit()
        self.assertEqual(self.client.get('/admin/workflow-monitor?workflow=2').status_code,404)
        self.assertEqual(self.client.get('/admin/workflow-monitor?workflow=-1').status_code,400)
        self.assertEqual(self.client.get('/admin/workflow-monitor?severity=private').status_code,400)

    def test_workflow_scores_resume_status_and_real_counts_render(self):
        response=self.client.get('/admin/workflow-monitor?workflow=1')
        self.assertEqual(response.status_code,200)
        self.assertIn(b'Python Engineer',response.data)
        self.assertIn(b'Revised resume',response.data)
        self.assertIn(b'+10 vs original',response.data)
        data=monitor.snapshot(self.conn,1,1)
        self.assertEqual(data['counts'],dict(fetched=1,scored=1,prepared=1,applied=0,review=0,emailed=0))
        self.assertEqual(data['jobs'][0]['state'],'Not selected')
        self.assertIn("connect-src 'self'",response.headers['Content-Security-Policy'])
        self.assertIn('no-store',response.headers['Cache-Control'])

    def test_events_are_scoped_and_unknown_exception_details_never_render(self):
        with self.conn:
            monitor.record(self.conn,1,'agent','missing_gemini',workflow_id=1,state='error')
            monitor.record(self.conn,2,'agent','missing_groq',workflow_id=2,state='error')
        page=self.client.get('/admin/workflow-monitor?workflow=1').get_data(as_text=True)
        self.assertIn('Gemini settings are missing',page)
        self.assertNotIn('Scoring key is missing',page)
        secret='synthetic-private-credential-value'
        self.assertEqual(monitor.error_code(RuntimeError(secret)),'unknown_error')
        self.assertNotIn(secret,str(monitor.message('unknown_error')))
        with self.assertRaisesRegex(ValueError,'owner mismatch'):
            monitor.record(self.conn,1,'agent','cycle_start',workflow_id=2)

    def test_stale_status_does_not_claim_worker_is_running_or_stopped(self):
        with self.conn:
            monitor.record(self.conn,1,'agent','cycle_start',workflow_id=1)
            self.conn.execute("UPDATE worker_pulses SET updated_at='2026-10-29T00:00:00+00:00'")
        data=monitor.snapshot(self.conn,1,1,now=datetime(2026,10,29,1,tzinfo=timezone.utc))
        agent=next(row for row in data['workers'] if row['worker']=='agent')
        self.assertTrue(agent['stale'])
        self.assertEqual(agent['state'],'running')
        page=self.client.get('/admin/workflow-monitor?workflow=1').get_data(as_text=True)
        self.assertIn('A schedule does not prove a process is running.',page)

    def test_local_view_shows_saved_jobs_scores_and_email_history(self):
        self.conn.execute("INSERT INTO sent_log(user_id,job_id,channel,sent_at) VALUES(1,1,'email','2026-10-29T00:00:00+00:00')")
        self.conn.commit()
        data=monitor.snapshot(self.conn,1)
        self.assertEqual(data['counts']['fetched'],2)
        self.assertEqual(data['counts']['scored'],2)
        self.assertEqual(data['counts']['emailed'],1)
        self.assertEqual(self.client.get('/admin/workflow-monitor').status_code,200)
        self.conn.execute('INSERT INTO user_settings(user_id,subscriptions_configured) VALUES(1,1)')
        self.conn.execute("INSERT INTO company_subscriptions(user_id,company_id,subscribed_at) VALUES(1,1,'2026-10-29')")
        self.conn.commit()
        self.assertEqual(monitor.snapshot(self.conn,1)['counts']['fetched'],1)

    def test_no_data_and_pre_migration_page_are_honest(self):
        self.conn.execute('DELETE FROM recruiter_jobs WHERE workflow_id=1')
        self.conn.commit()
        data=monitor.snapshot(self.conn,1,1)
        self.assertEqual(data['counts']['fetched'],0)
        self.assertEqual(data['jobs'],[])
        with patch.object(monitor,'available',return_value=False):
            response=self.client.get('/admin/workflow-monitor')
        self.assertIn(b'Monitor setup needed',response.data)
        self.assertEqual(response.status_code,200)

    def test_titles_are_escaped_and_raw_documents_are_not_exposed(self):
        self.conn.execute("UPDATE jobs SET title='<script>private()</script>',raw_json=? WHERE id=1",(json.dumps({'resume':'private resume text'}),))
        self.conn.commit()
        page=self.client.get('/admin/workflow-monitor').get_data(as_text=True)
        self.assertIn('&lt;script&gt;private()&lt;/script&gt;',page)
        self.assertNotIn('<script>private()</script>',page)
        self.assertNotIn('private resume text',page)

    def test_numeric_validation_and_provider_error_categories(self):
        for value in (float('nan'),float('inf'),-1,'private'):
            with self.subTest(value=value),self.assertRaises(ValueError):
                monitor.record(self.conn,1,'score','score_done',value=value)
        for status,expected in ((401,'provider_auth'),(403,'provider_auth'),(429,'provider_quota'),(400,'provider_request')):
            response=requests.Response()
            response.status_code=status
            self.assertEqual(monitor.error_code(requests.HTTPError(response=response)),expected)
        self.assertEqual(monitor.error_code(requests.ReadTimeout()),'provider_timeout')

    def test_log_paging_is_bounded(self):
        with self.conn:
            for index in range(105):
                monitor.record(self.conn,1,'agent','fit_done',workflow_id=1,value=index%100)
        data=monitor.snapshot(self.conn,1,1)
        self.assertEqual(len(data['events']),100)
        older=monitor.snapshot(self.conn,1,1,before=data['next_before'])
        self.assertEqual(len(older['events']),5)
        self.assertTrue(all(row['id']<data['next_before'] for row in older['events']))


if __name__=='__main__':
    unittest.main()
