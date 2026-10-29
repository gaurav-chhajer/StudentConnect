"""Optional offline browser QA: python -B -m unittest tests.monitor_browser_checks."""

import os
import unittest
from pathlib import Path
from urllib.parse import urlsplit
from unittest.mock import patch

from playwright.sync_api import sync_playwright

from backend import workflow_monitor as monitor
from backend.personal_applications import ROOT
from tests import test_workflow_monitor


class MonitorBrowserTests(unittest.TestCase):
    def test_responsive_monitor_and_refresh_preserve_job_details(self):
        fixture=test_workflow_monitor.WorkflowMonitorTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        with fixture.conn:
            monitor.record(fixture.conn,1,'fetch','fetch_done',value=16,state='waiting')
            monitor.record(fixture.conn,1,'score','score_idle',state='idle')
            monitor.record(fixture.conn,1,'email','email_idle',state='idle')
            monitor.record(fixture.conn,1,'agent','missing_gemini',workflow_id=1,state='error')
        output=ROOT/'.taste-skill'
        output.mkdir(exist_ok=True)
        with fixture.client.get('/admin/workflow-monitor?workflow=1') as response:
            (output/'monitor-preview.html').write_text(response.get_data(as_text=True).replace('/static/','/frontend/static/'),encoding='utf-8')
        with patch.dict(os.environ,{'PLAYWRIGHT_BROWSERS_PATH':str(ROOT/'data/browser-binaries')}),sync_playwright() as runtime:
            browser=runtime.chromium.launch(headless=True)
            try:
                context=browser.new_context()
                def serve(route):
                    parsed=urlsplit(route.request.url)
                    self.assertEqual(parsed.hostname,'localhost')
                    target=parsed.path+('?' + parsed.query if parsed.query else '')
                    with fixture.client.get(target) as response:
                        route.fulfill(status=response.status_code,headers=dict(response.headers),body=response.data)
                context.route('**/*',serve)
                page=context.new_page()
                for width in (1440,768,390):
                    page.set_viewport_size({'width':width,'height':1000})
                    page.goto('http://localhost/admin/workflow-monitor?workflow=1')
                    page.get_by_role('heading',name='Workflow monitor',exact=True).wait_for()
                    self.assertLessEqual(page.evaluate('document.documentElement.scrollWidth'),width)
                    self.assertTrue(page.get_by_role('heading',name='Gemini settings are missing',exact=True).is_visible())
                    page.screenshot(path=str(output/f'monitor-{width}.png'),full_page=True)
                page.locator('#wm-job-1 summary').click()
                page.get_by_role('button',name='Refresh now').click()
                page.get_by_text('Updated. Checks again in 10 seconds.',exact=True).wait_for()
                self.assertTrue(page.locator('#wm-job-1').evaluate('element=>element.open'))
                page.get_by_role('button',name='Auto-refresh on').click()
                self.assertEqual(page.locator('#wm-auto').get_attribute('aria-pressed'),'false')
                page.get_by_label('Jobs',exact=True).select_option('applied')
                page.get_by_role('button',name='Show workstream').click()
                self.assertTrue(page.get_by_text('No jobs in this view yet.',exact=False).is_visible())
            finally:
                browser.close()


if __name__=='__main__':
    unittest.main()
