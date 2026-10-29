"""Optional real-browser suite: python -B -m unittest tests.browser_checks.

Every request is fulfilled locally; the default suite needs no browser install.
"""

import os
import json
import unittest
from contextlib import contextmanager
from unittest.mock import patch

from playwright.sync_api import sync_playwright

from backend import application_browser as browser, gemini_resume
from backend import recruiter_sources as sources
from backend.personal_applications import ROOT

URL = 'https://jobs.lever.co/synthetic/offline-fixture'
FORM = '''<!doctype html><html><body><form class="application-form" action="">
<label>Full name<input name="name" required></label>
<label>Email<input type="email" name="email" required></label>
<label>Resume<input type="file" name="resume" required></label>
<button type="submit">Submit application</button></form>
<script>document.querySelector('form').addEventListener('submit',event=>{
event.preventDefault();document.body.innerHTML='<h1>Application submitted!</h1>';
});</script></body></html>'''


class OfflineBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.environment = patch.dict(os.environ,{'PLAYWRIGHT_BROWSERS_PATH':str(ROOT/'data/browser-binaries')})
        cls.environment.start()
        cls.addClassCleanup(cls.environment.stop)
        cls.runtime = sync_playwright().start()
        cls.addClassCleanup(cls.runtime.stop)
        cls.browser = cls.runtime.chromium.launch(headless=True)
        cls.addClassCleanup(cls.browser.close)
        cls.content,_ = gemini_resume.document('Synthetic Candidate\nSkills\nPython')

    def setUp(self):
        self.context = self.browser.new_context(service_workers='block')
        self.addCleanup(self.context.close)
        self.page = self.context.new_page()
        self.answers={'full name':'Synthetic Candidate','email':'candidate@example.test'}

    def serve(self,html=FORM):
        # This interception includes navigations and form POSTs.
        self.page.route('**/*',lambda route: route.fulfill(status=200,content_type='text/html',body=html))

    def test_lever_form_upload_fill_submit_and_confirmation(self):
        self.serve()
        intent=[]
        def before():
            self.assertEqual(self.page.locator('input[name="email"]').input_value(),'candidate@example.test')
            self.assertEqual(self.page.locator('input[type="file"]').evaluate('(e)=>e.files[0].name'),'Resume.docx')
            intent.append('submitting')
        result=browser.submit(self.page,URL,self.content,self.answers,before)
        self.assertEqual(intent,['submitting'])
        self.assertIn('confirmation',result)

    def test_agent_prepares_submits_real_form_and_restart_does_not_repeat(self):
        from backend import personal_applications as work, recruiter_ai as ai, recruiter_workflow as flow
        from scripts import run_recruiter_agent as agent
        from tests import test_personal_applications as fixtures

        fixture=fixtures.ApplicationWorkflowTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.serve()
        @contextmanager
        def page_factory():
            yield self.page
        generated={'resume_text':fixtures.RESUME,'cover_note':'I build Python projects.',
                   'change_log':[],'gaps':[],'evidence':[],'verification':{'faithful':True,'issues':[]}}
        real_send=work.send
        def send(conn,profile,run_id,tracker):
            return real_send(conn,profile,run_id,tracker,page_factory=page_factory)
        with patch.object(work.pipeline,'GROQ_API_KEY','synthetic'), \
                patch.object(ai,'materials',return_value=generated) as generate, \
                patch.object(work.pipeline,'score_job_for_user',return_value=fixtures.evaluation(85)), \
                patch.object(flow,'export'),patch.object(flow,'export_materials'), \
                patch.object(work,'send',side_effect=send) as sends:
            first=agent.cycle(fixture.conn,fixture.profile,1,live=True)
            self.assertEqual(first,{'status':'complete','applied':1,'blocked':0})
            self.assertEqual(fixture.row()['state'],'applied')
            self.assertEqual(agent.cycle(fixture.conn,fixture.profile,1,live=True),first)
        generate.assert_called_once()
        sends.assert_called_once()
        self.assertEqual(fixture.conn.execute('SELECT count(*) FROM applications').fetchone()[0],1)

    def test_greenhouse_form_uses_its_adapter(self):
        self.serve(FORM.replace('class="application-form"','id="application_form"'))
        result=browser.submit(self.page,'https://job-boards.greenhouse.io/synthetic/jobs/123',
                              self.content,self.answers,lambda:None)
        self.assertIn('confirmation',result)

    def test_ashby_form_uses_its_adapter(self):
        self.serve(FORM.replace('class="application-form"','class="synthetic-ashby-form"'))
        result=browser.submit(self.page,'https://jobs.ashbyhq.com/synthetic/abc',self.content,self.answers,lambda:None)
        self.assertIn('confirmation',result)

    def test_jobposting_structured_data_verifies_link_date_and_salary(self):
        posting={'@context':'https://schema.org','@type':'JobPosting','title':'Software Engineer',
            'hiringOrganization':{'name':'Synthetic'},'description':'India fresher role. Python SQL.',
            'jobLocation':{'address':{'addressCountry':'India'}},'datePosted':'2026-10-28',
            'baseSalary':{'currency':'INR','value':{'value':900000,'unitText':'YEAR'}}}
        self.serve('<html><head><script type="application/ld+json">'+json.dumps(posting)+'</script></head><body>Role</body></html>')
        result=sources.read_posting(self.page,URL)
        self.assertEqual(result['posted_at'],'2026-10-28')
        self.assertEqual(result['company_name'],'Synthetic')
        self.assertEqual(result['salary']['value']['value'],900000)
        self.assertEqual(result['url'],URL)

    def test_search_extracts_real_links_and_preserves_posting_identity(self):
        self.serve('<html><body><a href="https://www.linkedin.com/jobs/view/123?tracking=test">Engineer</a></body></html>')
        links=sources.portal_links(self.page,'LinkedIn','Software Engineer')
        self.assertEqual(links,['https://www.linkedin.com/jobs/view/123'])

    def test_search_login_gate_is_reported_without_bypass(self):
        self.serve('<html><body><input type="password"></body></html>')
        with self.assertRaises(ValueError):
            sources.portal_links(self.page,'LinkedIn','Software Engineer')

    def test_real_contact_requires_company_and_recruiter_evidence(self):
        self.serve('<html><body><h1>Synthetic Recruiter</h1><div>Synthetic talent acquisition '
                   '<a href="https://www.linkedin.com/in/synthetic-recruiter">Synthetic Recruiter</a></div></body></html>')
        contact=sources.find_contact(self.page,'Synthetic')
        self.assertEqual(contact['name'],'Synthetic Recruiter')
        self.assertIn('unverified',contact['relationship'])

    def test_unknown_required_answer_never_clicks(self):
        self.serve(FORM.replace('<button','<label>Work authorization<input required name="authorization"></label><button'))
        intent=[]
        with self.assertRaises(browser.NeedsReview):
            browser.submit(self.page,URL,self.content,self.answers,lambda:intent.append('clicked'))
        self.assertEqual(intent,[])
        self.assertEqual(self.page.locator('input[name="name"]').input_value(),'')

    def test_captcha_pauses(self):
        self.serve(FORM.replace('<form','<iframe src="https://challenge.example.test/recaptcha"></iframe><form'))
        with self.assertRaises(browser.NeedsReview):
            browser.submit(self.page,URL,self.content,self.answers,lambda:self.fail('CAPTCHA bypassed'))

    def test_changed_form_after_upload_pauses(self):
        handler='''<script>document.querySelector('input[type="file"]').addEventListener('change',()=>{
        const input=document.createElement('input');input.required=true;input.name='New question';
        document.querySelector('form').append(input);});</script>'''
        self.serve(FORM.replace('</body>',handler+'</body>'))
        with self.assertRaises(browser.NeedsReview):
            browser.submit(self.page,URL,self.content,self.answers,lambda:self.fail('Changed form submitted'))

    def test_page_cannot_silently_replace_an_approved_answer(self):
        handler='''<script>document.querySelector('input[type="file"]').addEventListener('change',()=>{
        document.querySelector('input[name="email"]').value='changed@example.test';});</script>'''
        self.serve(FORM.replace('</body>',handler+'</body>'))
        with self.assertRaises(browser.NeedsReview):
            browser.submit(self.page,URL,self.content,self.answers,lambda:self.fail('Changed answer submitted'))

    def test_unexpected_form_action_and_existing_confirmation_pause(self):
        for html in (FORM.replace('action=""','action="https://evil.example.test/collect"'),
                     FORM.replace('<form','<h1>Application submitted!</h1><form')):
            with self.subTest(html=html[:60]):
                self.serve(html)
                with self.assertRaises(browser.NeedsReview):
                    browser.submit(self.page,URL,self.content,self.answers,lambda:self.fail('Unsafe form submitted'))
                self.page.unroute('**/*')


if __name__=='__main__':
    unittest.main()
