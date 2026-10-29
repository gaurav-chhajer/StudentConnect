import copy
import json
import tempfile
import unittest
import xml.etree.ElementTree as ET
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from unittest.mock import MagicMock, patch
from zipfile import ZipFile

import requests

from backend import recruiter_ai as ai, recruiter_workflow as flow, recruiter_sources as sources
from backend import recruiter_export, personal_applications as work
from backend.notion_tracker import NotionTracker
from scripts import run_recruiter
from tests import test_personal_applications as fixtures

NOW=datetime(2026,10,29,3,0,tzinfo=timezone.utc)


def analysis():
    return {'titles':[{'title':'Software Engineer '+str(number),'category':('Strong fit','Stretch','Adjacent pivot')[number%3],
        'why':'Python project evidence','resume_line_ids':[6],'evidence':[{'line_id':6,'text':'Projects'}],
        'keywords':['Python','SQL','Git','Linux','Flask','SQLite','Testing','HTML'],
        'missing_keywords':['Git'],'gaps':[]} for number in range(20)]}


def posting(**changes):
    value={'title':'Software Engineer','company_name':'Synthetic','url':'https://jobs.lever.co/synthetic/new',
        'apply_url':'https://jobs.lever.co/synthetic/new','description':'India. Fresher or 1 year experience. Python SQL developer.',
        'location':'India','source':'Lever','posted_at':NOW.isoformat(),'date_evidence':'JobPosting.datePosted',
        'salary':{'currency':'INR','value':{'minValue':900000,'maxValue':1200000,'unitText':'YEAR'}},
        'valid_through':None,'contacts':[],'verified_at':NOW.isoformat()}
    value.update(changes)
    return value


def eligibility():
    return {'location_status':'eligible','seniority_status':'eligible','location_evidence':'India',
            'seniority_evidence':'Fresher or 1 year experience','resume_changes':['Prioritize the Python project']}


@contextmanager
def page_factory():
    yield object()


class RecruiterWorkflowTests(unittest.TestCase):
    setUp=fixtures.ApplicationWorkflowTests.setUp
    set_score=fixtures.ApplicationWorkflowTests.set_score

    def create(self):
        with patch.object(ai,'analyze',return_value=analysis()):
            return flow.create(self.conn,self.profile)

    def searched(self,postings=None):
        workflow_id=self.create()
        coverage={site:{'status':'fixture','discovered':1,'verified':1,'issues':[]} for site in sources.SITES}
        with patch.object(sources,'discover',return_value=(postings or [posting()],coverage)), \
                patch.object(ai,'classify',return_value=eligibility()), \
                patch.object(work.pipeline,'score_job_for_user',return_value=fixtures.evaluation(85)):
            flow.search(self.conn,self.profile,workflow_id,page_factory=page_factory,clock=NOW)
        return workflow_id

    def prepared(self,workflow_id):
        flow.approve(self.conn,self.profile,workflow_id,[1])
        generated={'resume_text':fixtures.RESUME,'cover_note':'I build Python and SQL projects.',
                   'change_log':[],'gaps':[],'evidence':[],'verification':{'faithful':True,'issues':[]}}
        with patch.object(ai,'review',return_value={'verdict':'Apply','reason':'Supported entry role','flags':[]}), \
                patch.object(ai,'materials',return_value=generated), \
                patch.object(work.pipeline,'score_job_for_user',return_value=fixtures.evaluation(86)):
            flow.build_materials(self.conn,self.profile,workflow_id)
        return json.loads(flow.rows(self.conn,workflow_id)[0]['materials_json'])

    def test_search_stops_before_approval_and_freezes_numbering(self):
        workflow_id=self.searched()
        row=self.conn.execute('SELECT state FROM recruiter_workflows WHERE id=?',(workflow_id,)).fetchone()
        self.assertEqual(row[0],'awaiting_approval')
        self.assertEqual(flow.rows(self.conn,workflow_id)[0]['selected'],0)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM application_runs').fetchone()[0],0)
        with self.assertRaises(ValueError):
            flow.build_materials(self.conn,self.profile,workflow_id)
        with self.assertRaises(ValueError):
            flow.search(self.conn,self.profile,workflow_id,page_factory=page_factory,clock=NOW)

    def test_recent_date_salary_location_and_seniority_filters(self):
        with patch.object(ai,'classify',return_value=eligibility()):
            self.assertEqual(flow.assess(posting(),NOW)['search_status'],'shortlisted')
            self.assertEqual(flow.assess(posting(posted_at=None),NOW)['search_status'],'unverified')
            self.assertEqual(flow.assess(posting(posted_at=(NOW-timedelta(days=8)).isoformat()),NOW)['search_status'],'excluded')
            self.assertEqual(flow.assess(posting(posted_at=(NOW+timedelta(days=1)).isoformat()),NOW)['search_status'],'excluded')
            low={'currency':'INR','value':{'value':700000,'unitText':'YEAR'}}
            self.assertEqual(flow.assess(posting(salary=low),NOW)['search_status'],'excluded')
            unknown=flow.assess(posting(salary=None),NOW)
            self.assertEqual(unknown['salary_status'],'unknown')
            self.assertIn('8 LPA minimum is not confirmed',unknown['reasons'])
        result=eligibility()
        result['location_status']='unknown'
        with patch.object(ai,'classify',return_value=result):
            self.assertEqual(flow.assess(posting(),NOW)['search_status'],'unverified')

    def test_empty_search_remains_incomplete_and_can_be_retried(self):
        workflow_id=self.create()
        with self.conn:
            self.conn.execute("UPDATE recruiter_workflows SET state='awaiting_approval' WHERE id=?",(workflow_id,))
        coverage={'Indeed':{'status':'unavailable','verified':0}}
        with patch.object(sources,'discover',return_value=([],coverage)), \
                patch.object(ai,'classify') as classify, \
                self.assertRaisesRegex(ValueError,'No postings could be verified'):
            flow.search(self.conn,self.profile,workflow_id,page_factory=page_factory,clock=NOW)
        classify.assert_not_called()
        row=self.conn.execute('SELECT state,coverage_json FROM recruiter_workflows WHERE id=?',(workflow_id,)).fetchone()
        self.assertEqual(row['state'],'searching')
        self.assertEqual(json.loads(row['coverage_json']),coverage)
        self.assertEqual(flow.rows(self.conn,workflow_id),[])

    def test_salary_currency_period_and_range_are_not_guessed(self):
        for value in (None,{}, {'currency':'USD','value':{'value':100000,'unitText':'YEAR'}},
                      {'currency':'INR','value':{'value':100000,'unitText':'MONTH'}},
                      {'currency':'INR','value':{'minValue':700000,'maxValue':900000,'unitText':'YEAR'}}):
            self.assertEqual(flow.salary_status(value),'unknown')
        self.assertEqual(flow.salary_status({'currency':'INR','value':{'value':800000,'unitText':'YEAR'}}),'confirmed')
        explicit=sources.salary_from_description('Annual salary: INR 8-12 LPA. India role.')
        self.assertEqual(flow.salary_status(explicit),'confirmed')
        self.assertIsNone(sources.salary_from_description('Salary 8-12 LPA.'))
        self.assertIsNone(sources.salary_from_description('Estimated salary INR 8-12 LPA.'))

    def test_approval_rejects_unverified_and_another_account(self):
        workflow_id=self.searched([posting(posted_at=None)])
        with self.assertRaises(ValueError):
            flow.approve(self.conn,self.profile,workflow_id,[1])
        with self.assertRaises(ValueError):
            flow.approve(self.conn,dict(self.profile,user_id=2),workflow_id,[1])

    def test_resume_change_invalidates_old_analysis(self):
        workflow_id=self.create()
        self.conn.execute("UPDATE users SET resume_text='Changed' WHERE id=1")
        self.conn.commit()
        with self.assertRaises(ValueError):
            flow.load(self.conn,self.profile,workflow_id)

    def test_materials_link_exact_document_and_enforce_selection(self):
        workflow_id=self.searched()
        value=self.prepared(workflow_id)
        run=dict(self.conn.execute('SELECT * FROM application_runs WHERE id=?',(value['run_id'],)).fetchone())
        self.assertEqual(run['state'],'ready')
        self.assertEqual(run['target_score'],90)
        self.assertEqual(run['document_hash'],value['document_hash'])
        with patch.object(flow,'utc_now',return_value=NOW):
            flow.require_submission_approval(self.conn,self.profile,run)
            self.conn.execute('UPDATE recruiter_jobs SET selected=0 WHERE workflow_id=?',(workflow_id,))
            self.conn.commit()
            with self.assertRaises(work.browser.NeedsReview):
                flow.require_submission_approval(self.conn,self.profile,run)
        self.assertEqual(self.conn.execute('SELECT resume_text FROM users').fetchone()[0],fixtures.RESUME)

    def test_approved_flow_submits_once_syncs_number_and_drafts_followup(self):
        workflow_id=self.searched()
        value=self.prepared(workflow_id)
        tracker=MagicMock()
        tracker.check.return_value=None
        tracker.record.return_value='109cb39a-c5ae-44ec-b188-bd55b3ddc646'
        def confirmed(page,url,content,answers,before):
            self.assertEqual(answers['cover note'],'I build Python and SQL projects.')
            before()
            return 'Synthetic confirmation'
        with patch.object(flow,'utc_now',return_value=NOW),patch.object(work.browser,'submit',side_effect=confirmed):
            work.send(self.conn,self.profile,value['run_id'],tracker,page_factory=page_factory)
        self.assertEqual(work.sync_notion(self.conn,self.profile,tracker),1)
        self.assertEqual(tracker.record.call_args.args[1]['workflow_number'],1)
        application_id=self.conn.execute('SELECT id FROM applications').fetchone()[0]
        flow.schedule_followup(self.conn,self.profile,application_id,'2026-10-30')
        with patch.object(ai,'followup',return_value='Following up on my application.'):
            result=flow.followups(self.conn,self.profile,None,clock=NOW)
        self.assertEqual(result['records'][0]['followup_date'],'2026-10-30')
        with self.assertRaises(work.browser.NeedsReview):
            work.send(self.conn,self.profile,value['run_id'],tracker,page_factory=page_factory)

    def test_local_followups_do_not_invent_dates(self):
        self.assertEqual(flow.followups(self.conn,self.profile,None,clock=NOW)['records'],[])
        with self.assertRaises(ValueError):
            flow.schedule_followup(self.conn,self.profile,999,'2026-10-30')

    def test_unconfirmed_pay_is_caution_and_cannot_submit(self):
        workflow_id=self.searched([posting(salary=None)])
        value=self.prepared(workflow_id)
        review=json.loads(flow.rows(self.conn,workflow_id)[0]['review_json'])
        self.assertEqual(review['verdict'],'Caution')
        run=dict(self.conn.execute('SELECT * FROM application_runs WHERE id=?',(value['run_id'],)).fetchone())
        self.assertEqual(run['state'],'needs_review')
        with patch.object(flow,'utc_now',return_value=NOW),self.assertRaises(work.browser.NeedsReview):
            flow.require_submission_approval(self.conn,self.profile,run)

    def test_ninety_plus_original_generates_cover_without_rewriting_resume(self):
        workflow_id=self.searched()
        with self.conn:
            self.conn.execute('UPDATE recruiter_jobs SET evaluation_json=? WHERE workflow_id=?',
                              (json.dumps(fixtures.evaluation(90)),workflow_id))
        flow.approve(self.conn,self.profile,workflow_id,[1])
        generated={'resume_text':fixtures.RESUME,'cover_note':'I build Python projects.',
                   'change_log':['Original retained'],'gaps':[],'evidence':[],'verification':{'faithful':True,'issues':[]}}
        with patch.object(ai,'review',return_value={'verdict':'Apply','reason':'Supported','flags':[]}), \
                patch.object(ai,'cover_materials',return_value=generated) as cover, \
                patch.object(ai,'materials') as rewrite, \
                patch.object(work.pipeline,'score_job_for_user',return_value=fixtures.evaluation(90)):
            flow.build_materials(self.conn,self.profile,workflow_id)
        cover.assert_called_once()
        rewrite.assert_not_called()
        result=json.loads(flow.rows(self.conn,workflow_id)[0]['materials_json'])
        self.assertEqual(result['resume_text'],fixtures.RESUME)

    def test_new_material_version_archives_previous_document(self):
        first=self.searched()
        original=self.prepared(first)
        previous=self.conn.execute('SELECT document FROM application_runs WHERE id=?',(original['run_id'],)).fetchone()[0]
        second=self.searched()
        replacement=self.prepared(second)
        self.assertEqual(original['run_id'],replacement['run_id'])
        archived=self.conn.execute('SELECT document,snapshot_json FROM recruiter_run_history WHERE run_id=?',
                                   (original['run_id'],)).fetchone()
        self.assertEqual(archived['document'],previous)
        self.assertEqual(json.loads(archived['snapshot_json'])['document_hash'],original['document_hash'])

    def test_saved_entry_can_be_applied_without_overwriting_its_notes(self):
        workflow_id=self.searched()
        value=self.prepared(workflow_id)
        job_id=flow.rows(self.conn,workflow_id)[0]['job_id']
        self.conn.execute("""INSERT INTO applications(user_id,job_id,title,company_name,url,status,notes,created_at,updated_at)
            VALUES(1,?,'Software Engineer','Synthetic',?,'saved','Personal note','now','now')""",
            (job_id,posting()['url']))
        self.conn.commit()
        def confirmed(page,url,content,answers,before):
            before()
            return 'Synthetic confirmation'
        with patch.object(flow,'utc_now',return_value=NOW),patch.object(work.browser,'submit',side_effect=confirmed):
            work.send(self.conn,self.profile,value['run_id'],page_factory=page_factory)
        saved=self.conn.execute('SELECT status,notes FROM applications').fetchone()
        self.assertEqual((saved['status'],saved['notes']),('applied','Personal note'))
        self.assertEqual(self.conn.execute('SELECT count(*) FROM applications').fetchone()[0],1)

    def test_unsupported_site_still_produces_materials_for_manual_handoff(self):
        link='https://www.linkedin.com/jobs/view/123456'
        workflow_id=self.searched([posting(url=link,apply_url=link,source='LinkedIn')])
        value=self.prepared(workflow_id)
        self.assertIn('unsupported application site',value['submission'])
        self.assertNotIn('run_id',value)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM application_runs').fetchone()[0],0)
        with tempfile.TemporaryDirectory(dir=work.ROOT/'data') as directory:
            root=Path(directory)
            (root/'data').mkdir()
            with patch.object(work,'ROOT',root):
                exported=flow.export_materials(self.conn,self.profile,workflow_id)
                report=json.loads(exported[0].read_text())
                self.assertIn('unsupported application site',report['submission'])

    def test_outreach_has_no_invented_contact_or_application(self):
        workflow_id=self.searched()
        self.prepared(workflow_id)
        with patch.object(sources,'find_contact',return_value=None), \
                patch.object(ai,'outreach',return_value='I build Python projects. Is this role still open?') as generate:
            flow.draft_outreach(self.conn,self.profile,workflow_id,page_factory=page_factory)
        value=json.loads(flow.rows(self.conn,workflow_id)[0]['outreach_json'])
        self.assertIsNone(value['contact'])
        self.assertFalse(value['applied'])
        self.assertFalse(generate.call_args.args[-1])

    def test_followups_use_india_week_and_actual_notion_due_dates(self):
        tracker=MagicMock()
        tracker.followups.return_value=[{'company':'Synthetic','role':'Engineer','application_date':'2026-10-22',
            'followup_date':'2026-10-29','history':'Applied','contact':None}]
        with patch.object(ai,'followup',return_value='Following up on my application.'):
            result=flow.followups(self.conn,self.profile,tracker,clock=NOW)
        self.assertEqual(result['week_start'],'2026-10-26')
        self.assertEqual(result['week_end'],'2026-11-01')
        self.assertEqual(len(result['records']),1)
        self.assertEqual(tracker.followups.call_args.args[0].isoformat(),'2026-10-26')

    def test_export_has_analysis_coverage_and_ranked_shortlist(self):
        workflow_id=self.searched()
        with tempfile.TemporaryDirectory(dir=work.ROOT/'data') as directory:
            root=Path(directory)
            (root/'data').mkdir()
            with patch.object(work,'ROOT',root):
                path=flow.export(self.conn,self.profile,workflow_id)
                with ZipFile(path) as archive:
                    for name in archive.namelist():
                        if name.endswith(('.xml','.rels')):
                            ET.fromstring(archive.read(name))
                    workbook=ET.fromstring(archive.read('xl/workbook.xml'))
                    names=[element.attrib['name'] for element in workbook.findall('.//{'+recruiter_export.MAIN+'}sheet')]
                    self.assertEqual(names,['Shortlist','Resume analysis','Source coverage','Unverified and excluded'])
                    self.assertIn(b'https://jobs.lever.co/synthetic/new',archive.read('xl/worksheets/sheet1.xml'))


class RecruiterAIAndExportTests(unittest.TestCase):
    def test_source_failure_progress_is_visible_without_private_error_text(self):
        connection=MagicMock()
        connection.execute.return_value.fetchall.return_value=[]
        page=MagicMock()
        page.is_closed.return_value=False
        page.context.browser.is_connected.return_value=True
        with patch.object(sources,'portal_links',side_effect=ValueError('synthetic-private-page-content')), \
                self.assertLogs('backend.recruiter_sources',level='INFO') as logs:
            postings,coverage=sources.discover(connection,['Software Engineer'],page)
        self.assertEqual(postings,[])
        self.assertEqual(coverage['Wellfound']['status'],'unavailable')
        output=' '.join(logs.output)
        self.assertIn('source=Wellfound',output)
        self.assertIn('Discovery finished',output)
        self.assertNotIn('synthetic-private-page-content',output)

    def test_closed_browser_stops_discovery_immediately(self):
        page=MagicMock()
        page.is_closed.return_value=True
        with patch.object(sources,'portal_links') as links, \
                self.assertRaisesRegex(ValueError,'browser/session closed'):
            sources.discover(MagicMock(),['Software Engineer'],page)
        links.assert_not_called()

    def test_browser_lifecycle_diagnostics_distinguish_crash_from_cleanup(self):
        browser,context,page=MagicMock(),MagicMock(),MagicMock()
        state=sources.monitor_browser(browser,context,page)
        page_events=dict(page.on.call_args_list[index].args for index in range(2))
        with self.assertLogs('backend.recruiter_sources',level='WARNING') as logs:
            page_events['crash'](page)
        self.assertIn('event=page_crashed expected_cleanup=False',logs.output[0])
        state['cleanup']=True
        with self.assertLogs('backend.recruiter_sources',level='INFO') as logs:
            browser.on.call_args.args[1](browser)
            context.on.call_args.args[1](context)
            page_events['close'](page)
        self.assertEqual(len(logs.output),3)
        self.assertTrue(all('INFO:' in entry and 'expected_cleanup=True' in entry for entry in logs.output))

    def test_failure_reason_is_safe_and_identifies_known_validation_error(self):
        self.assertEqual(sources.failure_reason(ValueError('A single verifiable JobPosting record is required')),
                         'missing_or_ambiguous_jobposting_data')
        self.assertEqual(sources.failure_reason(ValueError('synthetic-private-page-content')),'unclassified_ValueError')

    def test_target_closed_stops_even_when_browser_status_is_stale(self):
        target_closed=type('TargetClosedError',(Exception,),{})
        for phase in ('search','verification'):
            with self.subTest(phase=phase):
                page=MagicMock()
                page.is_closed.return_value=False
                page.context.browser.is_connected.return_value=True
                connection=MagicMock()
                connection.execute.return_value.fetchall.return_value=[]
                with patch.object(sources,'portal_links',side_effect=target_closed('private detail') if phase=='search' else None,
                                  return_value=['https://jobs.lever.co/synthetic/new']) as links, \
                        patch.object(sources,'read_posting',side_effect=target_closed('private detail')) as read, \
                        self.assertRaisesRegex(ValueError,'browser/session closed or crashed') as caught:
                    sources.discover(connection,['Software Engineer'],page)
                self.assertNotIn('private detail',str(caught.exception))
                if phase=='search':
                    links.assert_called_once()
                    read.assert_not_called()
                    connection.execute.assert_not_called()
                else:
                    read.assert_called_once()

    def test_generate_uses_bounded_extended_timeout_without_retrying(self):
        response=MagicMock()
        response.__enter__.return_value=response
        response.iter_content.return_value=[json.dumps({'candidates':[{'finishReason':'STOP',
            'content':{'parts':[{'text':'{"ok": true}'}]}}]}).encode()]
        with patch.object(ai.gemini,'configuration',return_value=('synthetic-key','test-model')), \
                patch.object(ai.requests,'post',return_value=response) as post:
            self.assertEqual(ai.generate('Connection fixture',{},ai.object_schema({'ok':{'type':'boolean'}})),{'ok':True})
        self.assertEqual(post.call_args.kwargs['timeout'],(10,180))
        post.assert_called_once()
        with patch.object(ai.gemini,'configuration',return_value=('synthetic-key','test-model')), \
                patch.object(ai.requests,'post',side_effect=requests.ReadTimeout) as post, \
                self.assertRaises(requests.ReadTimeout):
            ai.generate('Connection fixture',{},ai.object_schema({'ok':{'type':'boolean'}}))
        post.assert_called_once()

    def test_analysis_requires_twenty_evidence_supported_titles(self):
        with patch.object(ai,'generate',return_value=analysis()):
            value=ai.analyze(fixtures.RESUME)
        self.assertEqual(len(value['titles']),20)
        self.assertIn('Git',value['titles'][0]['missing_keywords'])
        invalid=analysis()
        invalid['titles'][0]['resume_line_ids']=[999]
        with patch.object(ai,'generate',return_value=invalid),self.assertRaises(ValueError):
            ai.analyze(fixtures.RESUME)

    def test_fabricated_eligibility_quote_is_rejected(self):
        value=eligibility()
        value['location_evidence']='Invented worldwide availability'
        with patch.object(ai,'generate',return_value=value),self.assertRaises(ValueError):
            ai.classify(posting())

    def test_rewrite_rejects_new_numbers_and_failed_independent_audit(self):
        base={'paragraphs':[{'text':fixtures.RESUME.splitlines()[0],'resume_line_ids':[1]},
             {'text':'Built Python reports','resume_line_ids':[7]}],
             'cover_note':'I build Python projects.','change_log':[],'gaps':[]}
        with patch.object(ai,'generate',side_effect=[copy.deepcopy(base),{'faithful':False,'issues':['Changed qualification']}]), \
                self.assertRaises(ValueError):
            ai.materials(fixtures.RESUME,posting())
        invalid=copy.deepcopy(base)
        invalid['paragraphs'][1]['text']='Improved reports by 99%'
        with patch.object(ai,'generate',return_value=invalid),self.assertRaises(ValueError):
            ai.materials(fixtures.RESUME,posting())
        with patch.object(ai,'generate',side_effect=[copy.deepcopy(base),{'faithful':True,'issues':[]} ]):
            result=ai.materials(fixtures.RESUME,posting())
        self.assertIn('Built Python reports',result['resume_text'])

    def test_outreach_word_limit_and_false_claims(self):
        for message in ('I recently applied. Can we speak?', 'word '*90+'?', 'No final question.'):
            with patch.object(ai,'generate',return_value={'message':message}),self.assertRaises(ValueError):
                ai.outreach(fixtures.RESUME,posting(),None,False)

    def test_formula_injection_is_text_and_links_are_external_relationships(self):
        content=recruiter_export.workbook([('Jobs',[['Company','Link'],['=HYPERLINK("bad")','https://jobs.lever.co/x/y']])])
        with ZipFile(BytesIO(content)) as archive:
            sheet=ET.fromstring(archive.read('xl/worksheets/sheet1.xml'))
            self.assertEqual(sheet.findall('.//{'+recruiter_export.MAIN+'}f'),[])
            cell=sheet.find('.//{'+recruiter_export.MAIN+'}c[@r="A2"]')
            self.assertEqual(cell.attrib['t'],'inlineStr')
            self.assertIn(b'TargetMode="External"',archive.read('xl/worksheets/_rels/sheet1.xml.rels'))

    def test_date_and_public_url_validation(self):
        self.assertEqual(flow.timestamp('2026-10-28').tzinfo,timezone.utc)
        with self.assertRaises(ValueError):
            flow.timestamp('2026-10-28T12:00:00')
        for url in ('https://127.0.0.1/job','http://jobs.lever.co/x/y','https://linkedin.com.evil.test/jobs'):
            with self.assertRaises(ValueError):
                sources.public_url(url)
        self.assertEqual(sources.public_url('https://in.indeed.com/viewjob?jk=abc&utm_source=test'),
                         'https://in.indeed.com/viewjob?jk=abc')

    def test_notion_followups_filter_uncertain_history_and_stop_bad_cursor(self):
        tracker=NotionTracker('synthetic','8e2b84c2-a7e8-4e6c-9194-44ee3e849f28')
        page={'properties':{'Contact status':{'select':{'name':'Not confirmed'}}}}
        with patch.object(tracker,'request',return_value={'results':[page],'has_more':False}):
            self.assertEqual(tracker.followups(NOW.date(),NOW.date()),[])
        with patch.object(tracker,'request',return_value={'results':[],'has_more':True,'next_cursor':None}), \
                self.assertRaises(ValueError):
            tracker.followups(NOW.date(),NOW.date())

    def test_cli_number_selection_is_explicit(self):
        self.assertEqual(run_recruiter.number_list('1,3,9'),[1,3,9])
        for value in ('','0','1,1','all','-2'):
            with self.assertRaises(Exception):
                run_recruiter.number_list(value)


if __name__=='__main__':
    unittest.main()
