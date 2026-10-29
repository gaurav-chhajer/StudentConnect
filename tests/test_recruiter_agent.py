"""Offline orchestration tests; real provider and browser calls are forbidden."""

import json
import unittest
from unittest.mock import MagicMock, patch

from backend import personal_applications as work, recruiter_ai as ai, recruiter_workflow as flow
from scripts import run_recruiter_agent as agent
from tests import test_personal_applications as fixtures
from tests.test_recruiter_workflow import analysis, page_factory


class RecruiterAgentTests(unittest.TestCase):
    setUp=fixtures.ApplicationWorkflowTests.setUp
    set_score=fixtures.ApplicationWorkflowTests.set_score
    row=fixtures.ApplicationWorkflowTests.row
    prepare=fixtures.ApplicationWorkflowTests.prepare

    def materials(self):
        return {'resume_text':fixtures.RESUME,'cover_note':'I build Python and SQL projects.',
                'change_log':[],'gaps':[],'evidence':[],'verification':{'faithful':True,'issues':[]}}

    def test_preview_is_offline_and_does_not_change_state(self):
        with patch.object(flow,'build_materials') as build,patch.object(work,'send') as send:
            result=agent.cycle(self.conn,self.profile,1)
        self.assertEqual(result,{'status':'preview','stage':'approved','selected':1})
        build.assert_not_called()
        send.assert_not_called()

    def test_waiting_for_approval_never_prepares_or_submits(self):
        self.conn.execute("UPDATE recruiter_workflows SET state='awaiting_approval'")
        self.conn.execute('UPDATE recruiter_jobs SET selected=0')
        self.conn.commit()
        with patch.object(flow,'build_materials') as build,patch.object(work,'send') as send:
            self.assertEqual(agent.cycle(self.conn,self.profile,1,live=True)['status'],'awaiting_approval')
        build.assert_not_called()
        send.assert_not_called()

    def test_search_advances_once_and_exports_without_submitting(self):
        self.conn.execute("UPDATE recruiter_workflows SET state='analyzed',analysis_json=?",(json.dumps(analysis()),))
        self.conn.execute('UPDATE recruiter_jobs SET selected=0')
        self.conn.commit()
        with patch.object(work.pipeline,'GROQ_API_KEY','synthetic'), \
                patch.object(flow,'search') as search,patch.object(flow,'export') as export, \
                patch.object(work,'send') as send:
            self.assertEqual(agent.cycle(self.conn,self.profile,1,live=True)['status'],'awaiting_approval')
        search.assert_called_once_with(self.conn,self.profile,1)
        export.assert_called_once()
        send.assert_not_called()

    def test_selected_lever_flow_prepares_submits_tracks_and_restart_skips(self):
        tracker=MagicMock()
        tracker.check.return_value=None
        tracker.record.return_value='109cb39a-c5ae-44ec-b188-bd55b3ddc646'
        def submit(page,url,document,answers,before):
            self.assertTrue(url.startswith('https://jobs.lever.co/'))
            before()
            return 'Synthetic confirmation'
        real_send=work.send
        def send(conn,profile,run_id,tracker):
            return real_send(conn,profile,run_id,tracker,page_factory=page_factory)
        with patch.object(work.pipeline,'GROQ_API_KEY','synthetic'), \
                patch.object(ai,'materials',return_value=self.materials()) as generate, \
                patch.object(work.pipeline,'score_job_for_user',return_value=fixtures.evaluation(85)), \
                patch.object(flow,'export'),patch.object(flow,'export_materials'), \
                patch.object(work.browser,'submit',side_effect=submit) as browser, \
                patch.object(work,'send',side_effect=send) as sends:
            result=agent.cycle(self.conn,self.profile,1,tracker,live=True)
            self.assertEqual(result,{'status':'complete','applied':1,'blocked':0})
            self.assertEqual(self.row()['notion_state'],'synced')
            self.assertEqual(agent.cycle(self.conn,self.profile,1,tracker,live=True),result)
        generate.assert_called_once()
        browser.assert_called_once()
        sends.assert_called_once()
        tracker.record.assert_called_once()
        self.assertEqual(self.conn.execute('SELECT count(*) FROM applications').fetchone()[0],1)

    def test_low_score_requires_review_and_is_never_submitted(self):
        with patch.object(work.pipeline,'GROQ_API_KEY','synthetic'), \
                patch.object(ai,'materials',return_value=self.materials()), \
                patch.object(work.pipeline,'score_job_for_user',return_value=fixtures.evaluation(79)), \
                patch.object(flow,'export'),patch.object(flow,'export_materials'),patch.object(work,'send') as send:
            result=agent.cycle(self.conn,self.profile,1,live=True)
        self.assertEqual(result['status'],'needs_review')
        self.assertEqual(self.row()['state'],'needs_review')
        send.assert_not_called()

    def test_non_lever_selection_is_rejected_before_preparation(self):
        row=self.conn.execute('SELECT posting_json FROM recruiter_jobs').fetchone()
        posting=json.loads(row[0])
        posting['url']='https://jobs.ashbyhq.com/synthetic/new'
        self.conn.execute('UPDATE recruiter_jobs SET posting_json=?',(json.dumps(posting),))
        self.conn.commit()
        with patch.object(flow,'build_materials') as build,self.assertRaisesRegex(ValueError,'Lever only'):
            agent.cycle(self.conn,self.profile,1,live=True)
        build.assert_not_called()

    def test_uncertain_submission_is_durable_and_never_repeated(self):
        self.prepare()
        self.conn.execute("UPDATE recruiter_workflows SET state='materials_ready'")
        self.conn.commit()
        def uncertain(page,url,document,answers,before):
            before()
            raise TimeoutError('synthetic private browser detail')
        real_send=work.send
        with patch.object(work.browser,'submit',side_effect=uncertain) as browser, \
                patch.object(work,'send',side_effect=lambda conn,profile,run_id,tracker:
                             real_send(conn,profile,run_id,tracker,page_factory=page_factory)):
            with self.assertRaises(TimeoutError):
                agent.cycle(self.conn,self.profile,1,live=True)
            self.assertEqual(self.row()['state'],'unknown')
            with self.assertRaisesRegex(ValueError,'reconciliation'):
                agent.cycle(self.conn,self.profile,1,live=True)
        browser.assert_called_once()

    def test_missing_selection_and_stale_resume_fail_closed(self):
        self.conn.execute('UPDATE recruiter_jobs SET selected=0')
        self.conn.commit()
        with self.assertRaisesRegex(ValueError,'explicitly selected'):
            agent.cycle(self.conn,self.profile,1,live=True)
        self.conn.execute("UPDATE users SET resume_text='Changed'")
        self.conn.commit()
        with self.assertRaisesRegex(ValueError,'Resume changed'):
            agent.cycle(self.conn,self.profile,1,live=True)

    def test_failed_or_submitting_runs_are_not_retried(self):
        self.prepare()
        for state in ('failed','submitting'):
            with self.subTest(state=state):
                self.conn.execute('UPDATE application_runs SET state=?',(state,))
                self.conn.commit()
                with patch.object(work,'send') as send,self.assertRaisesRegex(ValueError,'reconciliation'):
                    agent.cycle(self.conn,self.profile,1,live=True)
                send.assert_not_called()

    def test_scheduler_waits_remaining_interval_and_finishes(self):
        stop=MagicMock()
        stop.is_set.return_value=False
        tick=MagicMock(side_effect=[{'status':'awaiting_approval'},{'status':'complete'}])
        result=agent.serve(tick,stop,clock=MagicMock(side_effect=[0,10,300]))
        self.assertEqual(result['status'],'complete')
        stop.wait.assert_called_once_with(290)
        self.assertEqual(tick.call_count,2)

    def test_scheduler_does_not_retry_errors_or_repeat_once(self):
        stop=MagicMock()
        stop.is_set.return_value=False
        tick=MagicMock(side_effect=TimeoutError('fixture'))
        with self.assertRaises(TimeoutError):
            agent.serve(tick,stop)
        tick.assert_called_once()
        stop.wait.assert_not_called()
        tick=MagicMock(return_value={'status':'awaiting_approval'})
        agent.serve(tick,stop,once=True)
        tick.assert_called_once()
        stop.wait.assert_not_called()


if __name__=='__main__':
    unittest.main()
