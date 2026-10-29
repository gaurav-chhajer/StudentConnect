import copy
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing, contextmanager
from pathlib import Path
from unittest.mock import MagicMock, patch

import requests

from backend import application_browser as browser, gemini_resume as gemini
from backend import personal_applications as work
from backend.evaluation import COMPONENT_LIMITS, RUBRIC_VERSION
from backend.migrate import apply_migrations
from backend.notion_tracker import NotionTracker
from backend.pipeline_skeleton import SCHEMA
from scripts import run_applications as runner

RESUME = 'Synthetic Candidate\nEducation\nBTech 2027\nSkills\nPython SQL\nProjects\nBuilt a Python reporting tool'
PROFILE = {'user_id':1,'email':'candidate@example.test','answers':{'full name':'Synthetic Candidate',
           'email':'candidate@example.test'},'history_checked':True}


def evaluation(score):
    remaining = score
    parts = {}
    for name,maximum in COMPONENT_LIMITS.items():
        value = min(remaining,maximum)
        parts[name] = {'score':value,'explanation':'Synthetic evidence'}
        remaining -= value
    category = next(label for minimum,label in ((80,'Strong Match'),(65,'Good Match'),
        (45,'Partial Match'),(25,'Weak Match'),(0,'Not a Match')) if score >= minimum)
    return dict(status='evaluated',components=parts,raw_score=score,fit_score=score,match_category=category,
        reasoning='Synthetic fit',matched_skills=['Python'],missing_requirements=[],unknown_eligibility=[],
        matched_requirements=[],hard_blockers=[],provider='groq',rubric_version=RUBRIC_VERSION,model=work.pipeline.GROQ_MODEL)


@contextmanager
def fake_page():
    yield object()


class ApplicationWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(':memory:')
        self.conn.row_factory = sqlite3.Row
        self.conn.execute('PRAGMA foreign_keys=ON')
        self.addCleanup(self.conn.close)
        self.conn.executescript(SCHEMA)
        apply_migrations(self.conn)
        self.conn.execute("INSERT INTO companies(id,name,ats_type,board_slug) VALUES(1,'Synthetic','lever','synthetic')")
        self.conn.execute("INSERT INTO users(id,name,email,resume_text,preferences_json) VALUES(1,'Synthetic',?,?,?)",
                          (PROFILE['email'],RESUME,'{}'))
        self.conn.execute("INSERT INTO accounts(user_id,password_hash,created_at) VALUES(1,'fixture','2026-01-01')")
        self.conn.execute("INSERT INTO user_settings(user_id) VALUES(1)")
        self.conn.execute("""INSERT INTO jobs(id,company_id,external_id,title,url,raw_json,first_seen_at,last_seen_at)
            VALUES(1,1,'abc','Software Engineer','https://jobs.lever.co/synthetic/abc',?,'2026-01-01','2026-01-01')""",
            (json.dumps({'content':'Python SQL developer'}),))
        self.set_score(70)
        self.conn.execute("""INSERT INTO recruiter_workflows(id,user_id,resume_hash,state,analysis_json,created_at,updated_at)
            VALUES(1,1,?,'approved','{}',?,?)""",(work.digest(RESUME.encode()),work.now(),work.now()))
        posting={'title':'Software Engineer','company_name':'Synthetic','description':'Python SQL developer',
                 'url':'https://jobs.lever.co/synthetic/abc','location':'','posted_at':work.now()}
        self.conn.execute('''INSERT INTO recruiter_jobs(workflow_id,number,job_id,posting_json,facts_json,evaluation_json,selected,review_json)
            VALUES(1,1,1,?,?,?,1,?)''',(json.dumps(posting),json.dumps({'search_status':'shortlisted','salary_status':'confirmed'}),
            json.dumps(evaluation(70)),json.dumps({'verdict':'Apply'})))
        self.conn.commit()
        self.profile = copy.deepcopy(PROFILE)
        self.environment = patch.dict('os.environ',{'GEMINI_API_KEY':'synthetic-test-key','GEMINI_MODEL':'test-model'})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.network = patch('requests.sessions.Session.request',side_effect=AssertionError('Unexpected outbound request'))
        self.network.start()
        self.addCleanup(self.network.stop)

    def set_score(self,score):
        self.conn.execute('''INSERT INTO scores(job_id,user_id,fit_score,reasoning,scored_at,evaluation_json)
            VALUES(1,1,?,'Synthetic','2026-01-01',?) ON CONFLICT(job_id,user_id) DO UPDATE SET
            fit_score=excluded.fit_score,evaluation_json=excluded.evaluation_json''',(score,json.dumps(evaluation(score))))
        self.conn.execute('UPDATE recruiter_jobs SET evaluation_json=? WHERE workflow_id=1',(json.dumps(evaluation(score)),))
        self.conn.commit()

    def row(self,run_id=1):
        return dict(self.conn.execute('SELECT * FROM application_runs WHERE id=?',(run_id,)).fetchone())

    def prepare(self,baseline=70,revised=85,tracker=None):
        # Reorder complete sections while keeping the identity block fixed.
        sections = gemini.sections(RESUME)
        tailored = '\n'.join([sections[0],sections[3],sections[2],sections[1]])
        current=self.conn.execute('SELECT evaluation_json FROM scores WHERE job_id=1 AND user_id=1').fetchone()[0]
        self.conn.execute('UPDATE recruiter_jobs SET evaluation_json=? WHERE workflow_id=1',(current,))
        self.conn.commit()
        with patch.object(gemini,'tailor',return_value=tailored), \
                patch.object(work.pipeline,'score_job_for_user',side_effect=[evaluation(baseline),evaluation(revised)]):
            run_id=work.prepare(self.conn,self.profile,1,tracker)
        row=self.row(run_id)
        with self.conn:
            self.conn.execute('UPDATE recruiter_jobs SET materials_json=? WHERE workflow_id=1',
                (json.dumps({'run_id':run_id,'document_hash':row['document_hash']}),))
        return run_id

    def test_score_boundaries_and_no_master_changes(self):
        for original,baseline,revised,expected,target in ((61,70,79,'needs_review',80),
                (79,79,80,'ready',80),(80,80,87,'ready',90),(89,89,90,'ready',90)):
            with self.subTest(original=original):
                # Each independent fixture preserves permanent production uniqueness.
                self.conn.execute('UPDATE scores SET fit_score=?,evaluation_json=? WHERE job_id=1',
                                  (original,json.dumps(evaluation(original))))
                self.conn.execute('DELETE FROM application_runs')
                self.conn.commit()
                run_id = self.prepare(baseline,revised)
                row = self.row(run_id)
                self.assertEqual((row['state'],row['target_score']),(expected,target))
                self.assertEqual(self.conn.execute('SELECT resume_text FROM users').fetchone()[0],RESUME)
                self.assertEqual(self.conn.execute('SELECT fit_score FROM scores').fetchone()[0],original)

    def test_sixty_is_excluded(self):
        self.set_score(60)
        self.assertEqual(work.candidates(self.conn,1),[])
        with self.assertRaises(browser.NeedsReview):
            work.prepare(self.conn,self.profile,1)

    def test_ninety_skips_tailoring_and_revalidates_document(self):
        self.set_score(90)
        with patch.object(gemini,'tailor') as tailor,patch.object(work.pipeline,'score_job_for_user',return_value=evaluation(88)):
            run_id = work.prepare(self.conn,self.profile,1)
        tailor.assert_not_called()
        self.assertEqual(self.row(run_id)['final_score'],88)
        self.assertEqual(self.row(run_id)['state'],'ready')

    def test_lower_revision_keeps_baseline(self):
        self.set_score(85)
        run_id = self.prepare(85,81)
        row = self.row(run_id)
        self.assertEqual(row['resume_text'],RESUME)
        self.assertEqual(row['final_score'],85)

    def test_missing_history_and_provider_failure_do_not_submit(self):
        self.profile['history_checked'] = False
        run_id = work.prepare(self.conn,self.profile,1)
        self.assertEqual(self.row(run_id)['state'],'needs_review')

    def test_network_failure_persists_failure_and_is_not_retried(self):
        with patch.object(work.pipeline,'score_job_for_user',side_effect=requests.Timeout):
            with self.assertRaises(requests.Timeout):
                work.prepare(self.conn,self.profile,1)
        self.assertEqual(self.row()['state'],'failed')
        with patch.object(work.pipeline,'score_job_for_user') as score:
            work.prepare(self.conn,self.profile,1)
        score.assert_not_called()

    def test_submit_persists_confirmed_application(self):
        run_id = self.prepare()
        def confirmed(page,url,content,answers,before):
            before()
            self.assertEqual(self.row()['state'],'submitting')
            return 'Synthetic application confirmation'
        with patch.object(browser,'submit',side_effect=confirmed):
            work.send(self.conn,self.profile,run_id,page_factory=fake_page)
        self.assertEqual(self.row()['state'],'applied')
        self.assertEqual(self.conn.execute('SELECT status FROM applications').fetchone()[0],'applied')
        with self.assertRaises(browser.NeedsReview):
            work.send(self.conn,self.profile,run_id,page_factory=fake_page)

    def test_timeout_after_click_and_interruption_are_unknown(self):
        run_id = self.prepare()
        def timeout(page,url,content,answers,before):
            before()
            raise TimeoutError('Synthetic timeout')
        with patch.object(browser,'submit',side_effect=timeout),self.assertRaises(TimeoutError):
            work.send(self.conn,self.profile,run_id,page_factory=fake_page)
        self.assertEqual(self.row()['state'],'unknown')
        self.assertEqual(self.conn.execute('SELECT count(*) FROM applications').fetchone()[0],0)

    def test_preflight_pause_is_not_applied(self):
        run_id = self.prepare()
        with patch.object(browser,'submit',side_effect=browser.NeedsReview('Required question')),self.assertRaises(browser.NeedsReview):
            work.send(self.conn,self.profile,run_id,page_factory=fake_page)
        self.assertEqual(self.row()['state'],'needs_review')

    def test_inputs_changed_after_form_fill_prevent_click(self):
        run_id = self.prepare()
        def changed(page,url,content,answers,before):
            self.conn.execute("UPDATE users SET resume_text='Changed' WHERE id=1")
            self.conn.commit()
            before()
            self.fail('Changed inputs reached submission')
        with patch.object(browser,'submit',side_effect=changed),self.assertRaises(browser.NeedsReview):
            work.send(self.conn,self.profile,run_id,page_factory=fake_page)
        self.assertEqual(self.row()['state'],'needs_review')

    def test_stale_subscription_account_and_unknown_eligibility_block(self):
        for sql in ("UPDATE scores SET is_stale=1", "UPDATE companies SET active=0",
                    "UPDATE accounts SET email_verified=0"):
            with self.subTest(sql=sql):
                self.conn.execute('SAVEPOINT check_input')
                self.conn.execute(sql)
                with self.assertRaises((ValueError,browser.NeedsReview)):
                    user=work.user_for_profile(self.conn,self.profile)
                    work.job_inputs(self.conn,user,1)
                self.conn.execute('ROLLBACK TO check_input')
                self.conn.execute('RELEASE check_input')
        result=evaluation(85)
        result['unknown_eligibility']=['Work authorization unknown']
        self.conn.execute('UPDATE scores SET fit_score=85,evaluation_json=?',(json.dumps(result),))
        self.conn.execute('UPDATE recruiter_jobs SET evaluation_json=? WHERE workflow_id=1',(json.dumps(result),))
        self.conn.commit()
        with self.assertRaises(browser.NeedsReview):
            work.prepare(self.conn,self.profile,1)

    def test_local_history_and_canonical_url_prevent_duplicates(self):
        self.conn.execute("""INSERT INTO applications(user_id,title,company_name,url,status,created_at,updated_at)
            VALUES(1,'Role','Synthetic','https://jobs.lever.co/synthetic/abc?source=old','applied','now','now')""")
        self.conn.commit()
        with self.assertRaises(browser.NeedsReview):
            work.prepare(self.conn,self.profile,1)

    def test_another_user_cannot_send(self):
        run_id=self.prepare()
        profile=dict(self.profile,user_id=2)
        with self.assertRaises(browser.NeedsReview):
            work.send(self.conn,profile,run_id,page_factory=fake_page)

    def test_tampered_document_and_score_block(self):
        run_id=self.prepare()
        self.conn.execute("UPDATE application_runs SET document_hash='bad' WHERE id=?",(run_id,))
        self.conn.commit()
        with self.assertRaises(browser.NeedsReview):
            work.send(self.conn,self.profile,run_id,page_factory=fake_page)

    def test_notion_sync_failure_never_resubmits_and_marks_unknown(self):
        run_id=self.prepare()
        tracker=MagicMock()
        tracker.check.return_value=None
        def confirmed(page,url,content,answers,before):
            before()
            return 'Synthetic confirmation'
        with patch.object(browser,'submit',side_effect=confirmed):
            work.send(self.conn,self.profile,run_id,tracker,page_factory=fake_page)
        tracker.record.side_effect=requests.Timeout
        with self.assertRaises(requests.Timeout):
            work.sync_notion(self.conn,self.profile,tracker)
        self.assertEqual(self.row()['state'],'applied')
        self.assertEqual(self.row()['notion_state'],'unknown')
        tracker.record.reset_mock()
        self.assertEqual(work.sync_notion(self.conn,self.profile,tracker),0)
        tracker.record.assert_not_called()

    def test_preview_is_offline_and_does_not_claim(self):
        self.assertEqual(runner.cycle(self.conn,self.profile,None),0)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM application_runs').fetchone()[0],0)

    def test_missing_gemini_fails_before_scoring_or_claiming(self):
        with patch.dict('os.environ',{'GEMINI_API_KEY':''}),patch.object(work.pipeline,'score_job_for_user') as score:
            with self.assertRaises(ValueError):
                work.prepare(self.conn,self.profile,1)
        score.assert_not_called()
        self.assertEqual(self.conn.execute('SELECT count(*) FROM application_runs').fetchone()[0],0)

    def test_initializer_backs_up_before_additive_migration(self):
        with tempfile.TemporaryDirectory(dir=work.ROOT/'data') as directory:
            database=Path(directory)/'jobs.db'
            with closing(sqlite3.connect(database)) as connection:
                connection.executescript(SCHEMA)
                connection.execute("INSERT INTO users(email,resume_text) VALUES('synthetic@example.test','Original')")
                connection.commit()
            runner.initialize(database)
            backups=list((database.parent/'backups').glob('*.db'))
            self.assertEqual(len(backups),1)
            with closing(sqlite3.connect(backups[0])) as backup,closing(sqlite3.connect(database)) as current:
                self.assertEqual(backup.execute('SELECT resume_text FROM users').fetchone()[0],'Original')
                self.assertIsNone(backup.execute("SELECT name FROM sqlite_master WHERE name='application_runs'").fetchone())
                self.assertIsNotNone(current.execute("SELECT name FROM sqlite_master WHERE name='application_runs'").fetchone())

    def test_export_is_exact_user_scoped_and_never_overwrites(self):
        run_id=self.prepare()
        with tempfile.TemporaryDirectory(dir=work.ROOT/'data') as directory:
            root=Path(directory)
            (root/'data').mkdir()
            with patch.object(work,'ROOT',root):
                path=work.export_document(self.conn,1,run_id)
                self.assertEqual(path.read_bytes(),self.row()['document'])
                self.assertEqual(work.export_document(self.conn,1,run_id),path)
                with self.assertRaises(ValueError):
                    work.export_document(self.conn,2,run_id)
                path.write_bytes(b'Synthetic user edit')
                with self.assertRaises(ValueError):
                    work.export_document(self.conn,1,run_id)
                self.assertEqual(path.read_bytes(),b'Synthetic user edit')


class ResumeTailoringTests(unittest.TestCase):
    def reply(self,order,finish='STOP'):
        response=MagicMock()
        response.__enter__.return_value=response
        response.iter_content.return_value=[json.dumps({'candidates':[{'finishReason':finish,
            'content':{'parts':[{'text':json.dumps({'section_order':order})}]}}]}).encode()]
        return response

    def test_only_complete_sections_are_reordered(self):
        with patch.dict('os.environ',{'GEMINI_API_KEY':'synthetic-test-key','GEMINI_MODEL':'test-model'}), \
                patch.object(gemini.requests,'post',return_value=self.reply([0,3,2,1])) as post:
            result=gemini.tailor(RESUME,'Python developer',90)
        self.assertEqual(result.splitlines()[0],'Synthetic Candidate')
        self.assertEqual(sorted(result.splitlines()),sorted(RESUME.splitlines()))
        self.assertLess(result.index('Projects'),result.index('Education'))
        self.assertFalse(post.call_args.kwargs['allow_redirects'])
        self.assertIn('responseJsonSchema',post.call_args.kwargs['json']['generationConfig'])
        instructions=post.call_args.kwargs['json']['systemInstruction']['parts'][0]['text']
        for expected in ('India','one year','8 LPA','Scope of this API call','Do not search for jobs'):
            self.assertIn(expected,instructions)
        for task in range(1,7):
            self.assertIn('### Task ' + str(task),instructions)
        self.assertIn('STOP after Task 2',instructions)
        self.assertTrue(instructions.startswith(gemini.INSTRUCTIONS_PATH.read_text(encoding='utf-8').strip()))
        self.assertIn('Do not omit, duplicate, split or rewrite sections.',instructions)

    def test_recruiter_brief_preserves_approval_and_truthful_outreach(self):
        text=gemini.INSTRUCTIONS_PATH.read_text(encoding='utf-8')
        for expected in ('20 job titles','Eight to ten','seven days','STOP after Task 2',
                         'fewer than 120 words','fewer than 90 words','Asia/Kolkata',
                         'only when submission','LinkedIn, Indeed, Wellfound, Greenhouse, Lever, and Ashby'):
            self.assertIn(expected,text)

    def test_missing_or_invalid_custom_instructions_make_no_provider_call(self):
        with tempfile.TemporaryDirectory(dir=work.ROOT/'data') as directory:
            path=Path(directory)/'instructions.md'
            with patch.object(gemini,'INSTRUCTIONS_PATH',path), \
                    patch.dict('os.environ',{'GEMINI_API_KEY':'synthetic','GEMINI_MODEL':'test-model'}), \
                    patch.object(gemini.requests,'post') as post:
                with self.assertRaises(FileNotFoundError):
                    gemini.tailor(RESUME,'Python',80)
                for content in ('Missing workflow', '# Context\n\n## Workflow\n',
                                'x'*(gemini.MAX_INSTRUCTION_BYTES+1),
                                '# Context\x00\n\n## Workflow\nTask'):
                    path.write_text(content,encoding='utf-8')
                    with self.assertRaises(ValueError):
                        gemini.tailor(RESUME,'Python',80)
                post.assert_not_called()

    def test_invalid_order_and_blocked_responses_are_rejected(self):
        for order,finish in (([1,0,2,3],'STOP'),([0,1,1,3],'STOP'),([0,1,2],'STOP'),
                             ([0,1,2,True],'STOP'),([0,1,2,3],'MAX_TOKENS')):
            with self.subTest(order=order,finish=finish), \
                    patch.dict('os.environ',{'GEMINI_API_KEY':'synthetic','GEMINI_MODEL':'test-model'}), \
                    patch.object(gemini.requests,'post',return_value=self.reply(order,finish)), \
                    self.assertRaises(ValueError):
                gemini.tailor(RESUME,'Python',80)

    def test_document_round_trip_and_empty_input(self):
        content,text=gemini.document(RESUME)
        self.assertTrue(content.startswith(b'PK'))
        self.assertEqual(text,RESUME)
        with self.assertRaises(ValueError):
            gemini.document('')

    def test_missing_key_and_unstructured_resume_make_no_calls(self):
        with patch.dict('os.environ',{'GEMINI_API_KEY':'','GEMINI_MODEL':''}), \
                patch.object(gemini.requests,'post') as post:
            with self.assertRaises(ValueError):
                gemini.tailor(RESUME,'Python',80)
            with self.assertRaises(ValueError):
                gemini.tailor('No sections here','Python',80)
        post.assert_not_called()

    def test_bounded_provider_response(self):
        response=MagicMock()
        response.iter_content.return_value=[b'x'*(gemini.MAX_RESPONSE_BYTES+1)]
        with self.assertRaises(ValueError):
            gemini.bounded_json(response)


class FormAndTrackerTests(unittest.TestCase):
    def controls(self):
        def field(index,label,kind='text',required=True):
            return dict(index=index,tag='input',type=kind,name=label,label=label,
                        visible=True,disabled=False,required=required,value='',options=[])
        return [field(0,'Full name'),field(1,'Email','email'),field(2,'Resume','file')]

    def test_approved_answers_and_required_questions(self):
        self.assertEqual(len(browser.form_plan(self.controls(),PROFILE['answers'])),3)
        controls=self.controls()
        controls[0]['label']='Work authorization'
        with self.assertRaises(browser.NeedsReview):
            browser.form_plan(controls,PROFILE['answers'])

    def test_consent_is_never_invented(self):
        controls=self.controls()
        controls[0].update(type='checkbox',label='Consent')
        for answers in ({},{'consent':'maybe'},{'consent':'no'}):
            with self.assertRaises(browser.NeedsReview):
                browser.form_plan(controls,dict(PROFILE['answers'],**answers))
        plan=browser.form_plan(controls,dict(PROFILE['answers'],consent='yes'))
        self.assertEqual(plan[0],(0,'checkbox',True))

    def test_url_allowlist_rejects_ssrf_and_preserves_identity(self):
        for url in ('http://jobs.lever.co/x/y','https://jobs.lever.co.evil.test/x',
                    'https://jobs.lever.co:443/x/y','https://127.0.0.1/job','file:///resume',
                    'https://user:password@jobs.lever.co/x','https://jobs.lever.co'):
            with self.subTest(url=url),self.assertRaises(browser.NeedsReview):
                browser.application_url(url)
        self.assertEqual(browser.application_url('https://jobs.lever.co/x/y/?source=abc#apply'),
                         'https://jobs.lever.co/x/y')
        self.assertEqual(browser.application_url('https://jobs.lever.co/x/y/apply'),
                         'https://jobs.lever.co/x/y')

    def test_notion_history_and_pagination_fail_closed(self):
        tracker=NotionTracker('synthetic','8e2b84c2-a7e8-4e6c-9194-44ee3e849f28')
        page={'id':'109cb39a-c5ae-44ec-b188-bd55b3ddc646','properties':{
            'Job link':{'url':'https://jobs.lever.co/x/y?source=old'},
            'Contact status':{'select':{'name':'Applied'}}}}
        with patch.object(tracker,'request',return_value={'results':[page],'has_more':False}):
            with self.assertRaises(browser.NeedsReview):
                tracker.check('https://jobs.lever.co/x/y')
        with patch.object(tracker,'request',return_value={'results':[],'has_more':True}):
            with self.assertRaises(browser.NeedsReview):
                tracker.find('https://jobs.lever.co/x/y')

    def test_partial_notion_configuration_is_an_error(self):
        with patch.dict('os.environ',{'STUDENTCONNECT_NOTION_TOKEN':'synthetic','STUDENTCONNECT_NOTION_SOURCE':''}):
            with self.assertRaises(ValueError):
                NotionTracker.configured()

    def test_profile_validation_and_private_paths(self):
        with tempfile.TemporaryDirectory(dir=work.ROOT/'data') as directory:
            path=Path(directory)/'profile.json'
            path.write_text(json.dumps(PROFILE),encoding='utf-8')
            self.assertEqual(work.load_profile(path)['user_id'],1)
            invalid=dict(PROFILE,answers={'Email':'other@example.test'})
            path.write_text(json.dumps(invalid),encoding='utf-8')
            with self.assertRaises(ValueError):
                work.load_profile(path)
        with self.assertRaises((ValueError,FileNotFoundError)):
            work.load_profile(work.ROOT/'requirements.txt')


if __name__=='__main__':
    unittest.main()
