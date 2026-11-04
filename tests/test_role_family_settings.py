import json
import unittest

from backend import role_titles
from tests.test_web_flows import WebFlowTests


class RoleFamilySettingsTests(unittest.TestCase):
    setUp = WebFlowTests.setUp
    seed_user = WebFlowTests.seed_user
    post = WebFlowTests.post

    def test_custom_families_save_and_control_title_gate(self):
        self.seed_user()
        data=dict(name='Owner',email='owner@example.test',family_mode='custom',
                  family_name_0='Backend Development',family_titles_0='Backend Engineer\nBackend Developer')
        self.assertEqual(self.post('/settings',data).status_code,302)
        user=self.conn.execute('SELECT * FROM users WHERE id=1').fetchone()
        profile=role_titles.current(self.conn,user)
        self.assertEqual(len(profile['families']),1)
        self.assertEqual(role_titles.match_index('Backend Developer',profile),0)
        self.assertIsNone(role_titles.match_index('Data Analyst',profile))
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM scores WHERE is_stale=1').fetchone()[0],2)
        page=self.client.get('/settings').get_data(as_text=True)
        self.assertIn('Matching families &amp; titles',page)
        self.assertIn('Backend Development',page)
        data['family_mode']='suggested'
        self.assertEqual(self.post('/settings',data).status_code,302)
        prefs=json.loads(self.conn.execute('SELECT preferences_json FROM users WHERE id=1').fetchone()[0])
        self.assertNotIn('role_families',prefs)

    def test_invalid_customizations_are_rejected(self):
        self.seed_user()
        data=dict(name='Owner',email='owner@example.test',family_mode='custom')
        self.assertEqual(self.post('/settings',data).status_code,400)
        data.update(family_name_0='Software',family_titles_0='Software Engineer',
                    family_name_1='Duplicate',family_titles_1='Software Developer')
        self.assertEqual(self.post('/settings',data).status_code,400)

