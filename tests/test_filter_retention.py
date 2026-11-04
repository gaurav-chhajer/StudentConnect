"""Applied filters survive navigation within an authenticated browser session."""

import unittest
from unittest.mock import patch

from flask import render_template
from tests import test_web_flows


class FilterRetentionTests(unittest.TestCase):
    def setUp(self):
        test_web_flows.WebFlowTests.setUp(self)
        test_web_flows.WebFlowTests.seed_user(self)
        self.conn.execute('UPDATE accounts SET is_admin=1 WHERE user_id=1')
        self.conn.commit()

    def view(self, path, client=None):
        with patch('backend.app.render_template', wraps=render_template) as render:
            response = (client or self.client).get(path)
        self.assertEqual(response.status_code, 200)
        return render.call_args.kwargs

    def test_each_page_restores_its_own_applied_selection(self):
        self.view('/?min_score=80&sort=recent')
        self.view('/jobs?q=Engineer&location=Remote&company=1&min_score=70&job_type=full-time&days=7')
        self.view('/analytics?weeks=4')
        self.view('/admin/workflow-monitor?stage=scored&severity=warning&sort=fit')
        dashboard = self.view('/')
        self.assertEqual((dashboard['min_score'], dashboard['sort']), (80, 'recent'))
        jobs = self.view('/jobs')
        self.assertEqual((jobs['search'], jobs['location'], jobs['company'], jobs['min_score'],
                          jobs['job_type'], jobs['days']), ('Engineer', 'Remote', 1, 70, 'full-time', 7))
        self.assertEqual(self.view('/analytics')['weeks'], 4)
        monitor = self.view('/admin/workflow-monitor')['monitor']
        self.assertEqual((monitor['stage'], monitor['severity'], monitor['sort']), ('scored', 'warning', 'fit'))

    def test_explicit_filters_replace_saved_filters_and_defaults_reset_them(self):
        self.view('/jobs?q=Engineer&min_score=90')
        changed = self.view('/jobs?location=Remote')
        self.assertEqual((changed['search'], changed['min_score'], changed['location']), ('', 0, 'Remote'))
        self.view('/jobs?q=&company=0&min_score=0&location=&job_type=&days=0&sort=best')
        reset = self.view('/jobs')
        self.assertEqual((reset['search'], reset['min_score'], reset['location']), ('', 0, ''))

    def test_invalid_requests_do_not_replace_valid_selections(self):
        for valid, invalid, bare in (
                ('/jobs?min_score=80', '/jobs?min_score=101', '/jobs'),
                ('/analytics?weeks=4', '/analytics?weeks=0', '/analytics'),
                ('/admin/workflow-monitor?sort=fit', '/admin/workflow-monitor?sort=invalid', '/admin/workflow-monitor')):
            with self.subTest(page=bare):
                self.view(valid)
                with self.client.session_transaction() as session:
                    before = dict(session['page_filters'])
                self.assertEqual(self.client.get(invalid).status_code, 400)
                with self.client.session_transaction() as session:
                    self.assertEqual(session['page_filters'], before)
                self.view(bare)

    def test_navigation_cursors_are_not_remembered(self):
        self.view('/jobs?min_score=80&page=2')
        self.assertEqual(self.view('/jobs')['page'], 1)
        self.view('/admin/workflow-monitor?stage=scored&page=2&before=999')
        monitor = self.view('/admin/workflow-monitor')['monitor']
        self.assertEqual((monitor['page'], monitor['before']), (1, 0))
        self.assertEqual(monitor['stage'], 'scored')

    def test_search_whitespace_is_normalized_before_session_storage(self):
        self.view('/jobs', self.client)
        response = self.client.get('/jobs', query_string={'q': ' ' * 5000 + 'Engineer', 'location': ' Remote '})
        self.assertEqual(response.status_code, 200)
        with self.client.session_transaction() as session:
            self.assertEqual(session['page_filters']['jobs'], {'q': 'Engineer', 'location': 'Remote'})
        self.assertEqual(self.view('/jobs')['search'], 'Engineer')

    def test_selections_are_isolated_between_browser_sessions(self):
        self.view('/jobs?min_score=80')
        other = self.app.test_client()
        with other.session_transaction() as session:
            session['user_id'], session['session_version'] = 1, 0
        self.assertEqual(self.view('/jobs', other)['min_score'], 0)
        self.assertEqual(self.view('/jobs')['min_score'], 80)
