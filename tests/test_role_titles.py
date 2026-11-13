"""Offline coverage of distinct role families, aliases and title-first matching."""

import copy
import json
import threading
import unittest
from unittest.mock import MagicMock, patch

import requests

from backend import cloud_worker, pipeline_skeleton as pipeline, role_titles
from scripts import run_cloud_worker as runner
from tests import test_cloud_worker, test_evaluation


def profile():
    return test_cloud_worker.title_profile(['Python Developer', 'Backend Developer', 'Software Engineer',
                                          'API Developer', 'Data Analyst'])


class TitleTests(unittest.TestCase):
    def test_five_to_seven_distinct_families_with_real_line_references(self):
        for count in (5, 6, 7):
            value = profile()
            value['families'].extend(dict(family=title, aliases=[title], reason='Project evidence', resume_line_ids=[3])
                                   for title in ['Test Engineer', 'Automation Engineer'][:count - 5])
            self.assertEqual(role_titles.validate(value, 'Python project\n\nTesting project'), value)

    def test_invalid_and_fabricated_evidence_is_rejected(self):
        invalid = [None, {}, dict(profile(), status='unknown'), dict(profile(), families=[]),
                   dict(profile(), families=profile()['families'][:4]),
                   dict(profile(), families=profile()['families'] * 2),
                   dict(profile(), families=profile()['families'][:4] + [profile()['families'][0]])]
        for change in (dict(resume_line_ids=[True]), dict(resume_line_ids=[2]),
                       dict(resume_line_ids=[99]), dict(resume_line_ids=[1, 1]),
                       dict(resume_line_ids=[]), dict(family=''), dict(family='Intern'),
                       dict(family='x' * 121), dict(family='Python\nDeveloper'), dict(reason=None),
                       dict(aliases=[]), dict(aliases=None), dict(aliases=['Intern']),
                       dict(aliases=['x' * 121]), dict(aliases=['Python\nDeveloper']),
                       dict(aliases=[None]), dict(aliases=['Python Developer'] * 21),
                       dict(aliases=['Python Developer', 'python developer'])):
            value = profile()
            value['families'][0].update(change)
            invalid.append(value)
        for value in invalid:
            with self.subTest(value_type=type(value).__name__), self.assertRaises(ValueError):
                role_titles.validate(value, 'Python project\n\nTesting project')

    def test_insufficient_evidence_does_not_pad_or_allow_jobs(self):
        value = dict(status='insufficient_evidence', reason='Not enough demonstrated activities', families=[])
        self.assertEqual(role_titles.validate(value, 'Brief resume'), value)
        self.assertEqual(role_titles.select([dict(id=1, title='Software Engineer')], value), [])
        with self.assertRaises(ValueError):
            role_titles.validate(dict(value, families=profile()['families']), 'Brief resume')

    def test_aliases_entry_titles_and_conservative_numeric_specialization_checks(self):
        value = profile()
        for title in ('Junior Python Developer', 'Python Engineer Intern', 'SWE',
                      'Graduate Software Developer', 'Entry-level Back-end Developer',
                      'Software Engineer - Backend', 'Backend Software Developer'):
            with self.subTest(title=title):
                self.assertIsNotNone(role_titles.match_index(title, value))
        for title in ('Security Software Engineer 5', 'Software Engineer 5', 'Software Engineer II',
                      'Security Software Engineer', 'Security Backend Software Engineer 5',
                      'Senior Security Engineer', 'Data Scientist', ''):
            with self.subTest(title=title):
                self.assertIsNone(role_titles.match_index(title, value))

    def test_equivalent_titles_share_one_family_and_do_not_consume_slots(self):
        value = profile()
        value['families'][2].update(family='Software Engineering',
            aliases=['Software Engineer', 'Software Developer', 'SDE', 'SWE'])
        role_titles.validate(value, 'Python project')
        self.assertEqual(len(value['families']), 5)
        for title in ('Software Engineer', 'Software Developer', 'SDE', 'SWE',
                      'Software Development Engineer', 'Application Programmer', 'Application Developer'):
            with self.subTest(title=title):
                self.assertEqual(role_titles.match_index(title, value), 2)

    def test_family_core_accepts_trailing_team_technology_and_location(self):
        value = profile()
        for title, expected in (('Software Engineer - Payments', 2),
                                ('Software Developer Platform', 2), ('SWE - London', 2),
                                ('Backend Developer Python', 1),
                                ('Entry-level Backend Developer - Python', 1),
                                ('Software Engineer - Backend (Python)', 1),
                                ('Application Programmer - Payments', 2)):
            with self.subTest(title=title):
                self.assertEqual(role_titles.match_index(title, value), expected)
        jobs = [dict(id=7, title='Software Engineer Payments'),
                dict(id=3, title='Backend Developer Python'),
                dict(id=1, title='Software Developer - Platform')]
        self.assertEqual([job['id'] for job in role_titles.select(jobs, value)], [3, 1, 7])

    def test_family_core_does_not_discard_seniority_levels_or_an_unrelated_prefix(self):
        value = profile()
        for title in ('Software Engineer - Senior', 'Software Engineer Lead',
                      'Software Engineer - Manager', 'Software Engineer II - Payments',
                      'Software Engineer 5 - Payments', 'Security Software Engineer - Payments',
                      'Software Engineer IC5 - Payments', 'Software Engineer L3 - Payments',
                      'Data Scientist - Python'):
            with self.subTest(title=title):
                self.assertIsNone(role_titles.match_index(title, value))
                self.assertEqual(role_titles.select([dict(id=1, title=title)], value), [])
        value['families'][2]['aliases'].append('Software Engineer II')
        self.assertEqual(role_titles.match_index('Software Engineer II - Payments', value), 2)

    def test_associate_prefix_matches_core_family_and_decorated_saved_alias(self):
        value = profile()
        for title, expected in (('Associate Python Developer', 0),
                                ('Associate Python Engineer - Platform', 0),
                                ('Graduate Associate Backend Developer Python', 1)):
            with self.subTest(title=title):
                self.assertEqual(role_titles.match_index(title, value), expected)
                self.assertEqual(len(role_titles.select([dict(id=1, title=title)], value)), 1)
        value['families'][0]['aliases'] = ['Associate Python Developer']
        self.assertEqual(role_titles.match_index('Python Engineer - Platform', value), 0)
        self.assertEqual(role_titles.match_index('Associate Python Engineer - Platform', value), 0)

    def test_associate_prefix_does_not_bypass_levels_or_seniority(self):
        value = profile()
        for title in ('Senior Associate Python Developer', 'Associate Python Developer II',
                      'Associate Python Developer - Manager', 'Associate Python Developer IC5',
                      'Associate Security Software Engineer', 'Associate Data Scientist'):
            with self.subTest(title=title):
                self.assertIsNone(role_titles.match_index(title, value))
                self.assertEqual(role_titles.select([dict(id=1, title=title)], value), [])

    def test_broad_preferences_accept_associate_without_expanding_explicit_associate_preference(self):
        self.assertTrue(pipeline._matches_role(
            dict(title='Associate Python Engineer - Platform', department=None), ['python developer']))
        self.assertFalse(pipeline._matches_role(
            dict(title='Python Developer', department=None), ['associate python developer']))
        user = dict(preferences_json=json.dumps(dict(roles=['Associate Python Developer'])))
        value = profile()
        value['families'][0]['aliases'].append('Associate Python Developer')
        self.assertEqual(json.loads(role_titles.filtering_user(user, value)['preferences_json'])['roles'],
                         ['Associate Python Developer'])

    def test_technology_prefixes_and_versions_keep_the_role_family(self):
        value = profile()
        for title, expected in (('Python Backend Developer', 1), ('Python 3.12 Backend Developer', 1),
                                ('Python3 Software Engineer', 2), ('Software Engineer - Python 3', 2),
                                ('Python Developer - Python 3.12', 0), ('SQL Data Analyst', 4)):
            with self.subTest(title=title):
                self.assertEqual(role_titles.match_index(title, value), expected)
        for title in ('Python 3 Backend Developer II', 'Software Engineer - Python 3 - IC5',
                      'Security Software Engineer - Python 3'):
            with self.subTest(title=title):
                self.assertIsNone(role_titles.match_index(title, value))

    def test_common_engineer_abbreviations(self):
        value = profile()
        value['families'][3]['aliases'] = ['DevOps Engineer']
        for title, expected in (('Software Eng. - Payments', 2), ('Backend Engr Python', 1),
                                ('DevOps Engr', 3)):
            with self.subTest(title=title):
                self.assertEqual(role_titles.match_index(title, value), expected)
        self.assertIsNone(role_titles.match_index('Software Engr II', value))

    def test_explicit_qualifiers_accept_synonyms_but_remain_required(self):
        for title, preferred in (('Associate Python Engineer - Platform', 'associate python developer'),
                                 ('Python Engineer Intern', 'python developer internship'),
                                 ('Jr Python Engineer', 'junior python developer')):
            with self.subTest(title=title):
                self.assertTrue(pipeline._matches_role(dict(title=title, department=None), [preferred]))
        for title, preferred in (('Python Engineer', 'associate python developer'),
                                 ('Python Engineer', 'python developer internship'),
                                 ('Python Engineer II', 'python engineer 2'),
                                 ('Python Engineer II 5', 'python engineer ii')):
            with self.subTest(title=title):
                self.assertFalse(pipeline._matches_role(dict(title=title, department=None), [preferred]))

    def test_combined_distinct_roles_are_held_even_when_one_matches(self):
        value = profile()
        for title in ('Software Engineer / Data Analyst', 'Software Engineer & Data Analyst',
                      'Software Engineer and Data Analyst', 'Software Engineer / Backend Developer'):
            with self.subTest(title=title):
                self.assertIsNone(role_titles.match_index(title, value))
                self.assertEqual(role_titles.select([dict(id=1, title=title)], value), [])
        self.assertEqual(role_titles.match_index('Software Engineer / Software Developer', value), 2)
        self.assertEqual(role_titles.match_index('Software Engineer - Python/Java', value), 2)

    def test_seniority_checks_use_words_and_keep_real_senior_roles_excluded(self):
        user = dict(preferences_json='{}')
        for title in ('Software Engineer - Staffing', 'Managerial Analyst', 'Leadership Analyst'):
            job = dict(title=title, department=None, location=None, raw_json='{}')
            with self.subTest(title=title):
                self.assertEqual(pipeline.filter_jobs_for_user([job], user), [job])
        for title in ('Staff Engineer', 'Software Engineer Lead', 'Sr. Software Engineer',
                      'Engineering Manager', 'Vice-President Engineering', 'Senior Associate Developer'):
            job = dict(title=title, department=None, location=None, raw_json='{}')
            with self.subTest(title=title):
                self.assertEqual(pipeline.filter_jobs_for_user([job], user), [])

    def test_employer_levels_require_the_specific_alias(self):
        value = profile()
        for title in ('SDE-1', 'Software Engineer II', 'Software Engineer IC2'):
            with self.subTest(title=title):
                self.assertIsNone(role_titles.match_index(title, value))
        value['families'][2]['aliases'].extend(['Software Engineer 1', 'Software Engineer II'])
        self.assertEqual(role_titles.match_index('SDE-1', value), 2)
        self.assertEqual(role_titles.match_index('Software Engr II - Python 3', value), 2)
        self.assertIsNone(role_titles.match_index('Software Engineer IC2', value))

    def test_role_preferences_recognize_equivalent_family_core_without_relaxing_explicit_levels(self):
        for title, roles in (('SWE - Payments', ['software engineering']),
                             ('Backend Developer Python', ['backend engineer']),
                             ('Application Programmer - Platform', ['software engineer'])):
            job = dict(title=title, department=None)
            with self.subTest(title=title):
                self.assertTrue(pipeline._matches_role(job, roles))
        for title, roles in (('Data Analyst - Payments', ['software engineer']),
                             ('Software Engineer - Payments', ['software engineer intern']),
                             ('Software Engineer 5 - Payments', ['software engineer ii'])):
            with self.subTest(title=title):
                self.assertFalse(pipeline._matches_role(dict(title=title, department=None), roles))
        user = dict(preferences_json=json.dumps(dict(roles=['Data Analysis'])))
        before = copy.deepcopy(user)
        expanded = role_titles.filtering_user(user, test_cloud_worker.title_profile())
        roles = pipeline._parse_preferences(expanded)['roles']
        self.assertTrue(pipeline._matches_role(dict(title='Data Analyst - Payments', department=None), roles))
        self.assertFalse(pipeline._matches_role(dict(title='Software Engineer - Payments', department=None), roles))
        self.assertEqual(user, before)

    def test_synonymous_families_or_overlapping_aliases_are_rejected(self):
        for change in (dict(family='Software Development'), dict(aliases=['Application Programmer'])):
            value = profile()
            value['families'][0].update(change)
            with self.subTest(change=change), self.assertRaises(ValueError):
                role_titles.validate(value, 'Python project')

    def test_groq_can_expand_aliases_without_changing_family_count(self):
        value = profile()
        value['families'][2]['aliases'].append('Application Systems Builder')
        role_titles.validate(value, 'Python project')
        self.assertEqual(len(value['families']), 5)
        self.assertEqual(role_titles.match_index('Application Systems Builder', value), 2)
        self.assertIsNone(role_titles.match_index('Security Application Systems Builder 5', value))

    def test_explicit_preferences_expand_within_selected_family_without_mutation(self):
        user = dict(id=1, resume_text='Python project', preferences_json=json.dumps(dict(
            roles=['Software Engineer'], locations=['Remote'], employment_types=['internship'], min_stipend=20000)))
        before = copy.deepcopy(user)
        expanded = json.loads(role_titles.filtering_user(user, profile())['preferences_json'])
        self.assertIn('Software Developer', expanded['roles'])
        self.assertIn('Application Programmer', expanded['roles'])
        self.assertNotIn('Data Analyst', expanded['roles'])
        self.assertEqual({key: expanded[key] for key in ('locations', 'employment_types', 'min_stipend')},
                         dict(locations=['Remote'], employment_types=['internship'], min_stipend=20000))
        self.assertEqual(user, before)
        for role in ('Software Engineer Intern', 'Software Engineer 5', 'Python'):
            preferred = dict(user, preferences_json=json.dumps(dict(roles=[role])))
            self.assertEqual(json.loads(role_titles.filtering_user(preferred, profile())['preferences_json'])['roles'], [role])

    def test_invalid_saved_role_preferences_are_rejected(self):
        for preferences in ([], dict(roles=None), dict(roles='Software Engineer'), dict(roles=[None]),
                            dict(roles=['x' * 101]), dict(roles=['Software Engineer'] * 21)):
            with self.subTest(preferences_type=type(preferences).__name__), self.assertRaises(ValueError):
                role_titles.filtering_user(dict(preferences_json=json.dumps(preferences)), profile())

    def test_generate_uses_full_resume_closes_response_and_validates_evidence(self):
        resume = 'Python API project\n' + 'SQL work\n' * 30 + 'Complete resume tail'
        with patch.object(role_titles.requests, 'post') as post:
            post.return_value.iter_content.return_value = [json.dumps(dict(
                choices=[dict(message=dict(content=json.dumps(profile())))])).encode()]
            self.assertEqual(role_titles.generate(dict(resume_text=resume), api_key='synthetic-key'), profile())
        request = post.call_args.kwargs
        self.assertEqual(request['timeout'], 30)
        self.assertEqual(request['headers']['Authorization'], 'Bearer synthetic-key')
        self.assertIn('Complete resume tail', request['json']['messages'][1]['content'])
        self.assertEqual(request['json']['model'], pipeline.GROQ_MODEL)
        post.return_value.close.assert_called_once()

    def test_generate_redacts_resume_identifiers_before_external_request(self):
        resume = 'Asha Kumar\nasha@example.test\n+91 98765 43210\nPython project'
        with patch.object(role_titles.requests, 'post') as post:
            post.return_value.iter_content.return_value = [json.dumps(dict(
                choices=[dict(message=dict(content=json.dumps(profile())))])).encode()]
            role_titles.generate(dict(name='Asha Kumar', resume_text=resume), api_key='synthetic-key')
        prompt = post.call_args.kwargs['json']['messages'][1]['content']
        for private_value in ('Asha Kumar', 'asha@example.test', '98765 43210'):
            self.assertNotIn(private_value, prompt)
        self.assertIn('Python project', prompt)

    def test_invalid_outputs_http_failures_and_timeouts_propagate_without_retry(self):
        for content in ('not JSON', '{}', json.dumps(dict(profile(), families=[]))):
            with self.subTest(content=content), patch.object(role_titles.requests, 'post') as post:
                post.return_value.iter_content.return_value = [json.dumps(dict(
                    choices=[dict(message=dict(content=content))])).encode()]
                with self.assertRaises(ValueError):
                    role_titles.generate(dict(resume_text='Python project'), api_key='synthetic')
                post.return_value.close.assert_called_once()
        with patch.object(role_titles.requests, 'post') as post:
            post.return_value.raise_for_status.side_effect = requests.HTTPError()
            with self.assertRaises(requests.HTTPError):
                role_titles.generate(dict(resume_text='Python project'), api_key='synthetic')
            post.return_value.close.assert_called_once()
        with patch.object(role_titles.requests, 'post', side_effect=requests.Timeout()) as post:
            with self.assertRaises(requests.Timeout):
                role_titles.generate(dict(resume_text='Python project'), api_key='synthetic')
            post.assert_called_once()

    def test_bounded_response_and_malformed_provider_envelopes(self):
        for payload in (b'x' * (role_titles.MAX_RESPONSE_BYTES + 1), b'[]', b'{"choices":[]}',
                        b'{"choices":[{}]}', b'{"choices":[{"message":{"content":null}}]}'):
            with self.subTest(size=len(payload)), patch.object(role_titles.requests, 'post') as post:
                post.return_value.iter_content.return_value = [payload]
                with self.assertRaises(ValueError):
                    role_titles.generate(dict(resume_text='Python project'), api_key='synthetic')
                post.return_value.close.assert_called_once()

    def test_missing_inputs_and_context_budget_fail_before_network(self):
        with patch.object(role_titles.requests, 'post') as post:
            for resume, key in (('', 'synthetic'), ('x' * 100001, 'synthetic'), ('Python', '')):
                with self.assertRaises((ValueError, RuntimeError)):
                    role_titles.generate(dict(resume_text=resume), api_key=key)
            with patch.dict(pipeline.os.environ, GROQ_CONTEXT_TOKENS='5000'):
                with self.assertRaises(ValueError):
                    role_titles.generate(dict(resume_text='Python'), api_key='synthetic')
            post.assert_not_called()


class ProfileEndpointTests(unittest.TestCase):
    setUp = test_cloud_worker.CloudEndpointTests.setUp
    call = test_cloud_worker.CloudEndpointTests.call

    def test_decorated_family_title_passes_preferences_queue_and_score_save(self):
        self.conn.execute('UPDATE scores SET is_stale=0')
        self.conn.execute('UPDATE scores SET is_stale=1 WHERE job_id=1')
        self.conn.execute("UPDATE jobs SET title='Software Developer - Payments' WHERE id=1")
        self.conn.execute('UPDATE users SET preferences_json=? WHERE id=1',
                          (json.dumps(dict(roles=['Software Engineering'], locations=['Remote'])),))
        self.conn.commit()
        work = self.call('work').json['items']
        self.assertEqual([item['job']['id'] for item in work], [1])
        client = MagicMock()
        def hosted(route, data=None):
            response = self.call(route, data)
            self.assertEqual(response.status_code, 200)
            return response.json
        client.call.side_effect = hosted
        pool = MagicMock(reporting_failed=False)
        pool.score.return_value = test_evaluation.sample_result()
        with patch.object(runner, 'GroqKeyPool', return_value=pool):
            self.assertEqual(runner.score_pending(client, runner.time.monotonic() + 3000,
                                                 threading.Event()), 0)
        pool.score.assert_called_once()
        self.assertEqual(pool.score.call_args.args[1]['title'], 'Software Developer - Payments')
        self.assertEqual(self.call('work').json['items'], [])

    def require_profile(self):
        # A changed resume invalidates the saved profile without deleting prior data.
        self.conn.execute("UPDATE users SET resume_text='Python API project' WHERE id=1")
        self.conn.commit()
        return self.call('work').json['role_profile_required']

    def save_data(self, required, value=None):
        return dict(user_id=required['user']['id'], fingerprint=required['fingerprint'],
                    version=required['version'], model=required['model'], profile=value or profile())

    def test_cached_profiles_skip_generation_and_do_not_read_complete_jds(self):
        with patch.object(pipeline, 'extract_job_description', side_effect=AssertionError('Read JD before title gate')):
            self.assertEqual(len(self.call('work').json['items']), 2)
        self.client = self.app.test_client()
        self.assertNotIn('role_profile_required', self.call('work').json)
        required = self.require_profile()
        self.assertEqual(required['user']['resume_text'], 'Python API project')
        self.assertIn('name', required['user'])
        self.assertEqual(self.call('work').json['items'], [])

    def test_title_gate_rejects_specialization_and_numeric_level_before_five_job_limit(self):
        self.conn.execute("UPDATE jobs SET title='Security Software Engineer 5' WHERE id=1")
        self.conn.execute("UPDATE jobs SET title='Graduate Software Engineer' WHERE id=2")
        role_titles.save(self.conn, dict(id=1, resume_text='Python resume'), profile())
        self.conn.commit()
        with patch.object(pipeline, 'extract_job_description', side_effect=AssertionError('Read JD')):
            self.assertEqual([item['job']['id'] for item in self.call('work').json['items']], [2])
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM jobs').fetchone()[0], 2)

    def test_explicit_engineer_preference_accepts_developer_and_keeps_other_filters(self):
        settings = json.dumps(dict(roles=['Software Engineer'], locations=['Remote']))
        self.conn.execute('UPDATE users SET preferences_json=? WHERE id=1', (settings,))
        self.conn.execute("UPDATE jobs SET title='Software Developer' WHERE id=1")
        self.conn.execute("UPDATE jobs SET title='Backend Developer' WHERE id=2")
        self.conn.commit()
        item = self.call('work').json['items'][0]
        self.assertEqual(item['job']['id'], 1)
        self.assertEqual(item['user']['preferences_json'], settings)
        self.assertEqual(len(self.call('work').json['items']), 1)
        data = dict(user_id=1, job_id=1, fingerprint=item['fingerprint'], result=test_evaluation.sample_result())
        self.assertEqual(self.call('score', data).json, dict(saved=True))
        self.assertEqual(self.conn.execute('SELECT preferences_json FROM users WHERE id=1').fetchone()[0], settings)

    def test_uncached_profile_is_requested_before_literal_role_filter_can_drop_synonym(self):
        self.conn.execute("UPDATE users SET resume_text='Updated project',preferences_json=? WHERE id=1",
                          (json.dumps(dict(roles=['Software Engineer'], locations=['Remote'])),))
        self.conn.execute("UPDATE jobs SET title='Software Developer'")
        self.conn.commit()
        required = self.call('work').json['role_profile_required']
        self.assertEqual(self.call('role-titles', self.save_data(required)).json, dict(saved=True))
        self.assertEqual(len(self.call('work').json['items']), 2)

    def test_legacy_title_profile_is_invalidated_without_editing_applied_migration(self):
        self.conn.execute('UPDATE resume_role_titles SET version=?,profile_json=? WHERE user_id=1',
                          ('resume-titles-v1', json.dumps(dict(status='ready', reason='Legacy', titles=[]))))
        self.conn.commit()
        self.assertIn('role_profile_required', self.call('work').json)
        self.assertEqual(self.conn.execute('SELECT version FROM resume_role_titles WHERE user_id=1').fetchone()[0],
                         'resume-titles-v1')

    def test_profile_auth_validation_and_changed_inputs(self):
        required = self.require_profile()
        data = self.save_data(required)
        self.assertEqual(self.client.post('/internal/worker/role-titles', json=data).status_code, 401)
        self.assertEqual(self.call('role-titles', dict(data, profile={})).status_code, 400)
        self.assertEqual(self.call('role-titles', dict(data, user_id=True)).status_code, 400)
        self.assertEqual(self.call('role-titles', dict(data, version='old')).status_code, 409)
        self.assertEqual(self.call('role-titles', dict(data, model='other')).status_code, 409)
        self.conn.execute("UPDATE users SET resume_text='Updated again' WHERE id=1")
        self.conn.commit()
        self.assertEqual(self.call('role-titles', data).status_code, 409)

    def test_insufficient_evidence_is_saved_explicitly_without_fallback(self):
        required = self.require_profile()
        value = dict(status='insufficient_evidence', reason='Too little evidence', families=[])
        self.assertEqual(self.call('role-titles', self.save_data(required, value)).json, dict(saved=True))
        self.assertEqual(self.call('work').json, dict(next=1, items=[]))

    def test_profile_configuration_and_preferences_are_rechecked(self):
        required = self.require_profile()
        data = self.save_data(required)
        self.assertEqual(self.call('role-titles', data).json, dict(saved=True))
        with patch.object(role_titles, 'VERSION', 'next-version'):
            self.assertIn('role_profile_required', self.call('work').json)
        with patch.object(pipeline, 'GROQ_MODEL', 'other-model'):
            self.assertIn('role_profile_required', self.call('work').json)
        self.conn.execute("UPDATE users SET preferences_json=? WHERE id=1", (json.dumps(dict(roles=['Security'])),))
        self.conn.commit()
        self.assertEqual(self.call('work').json, dict(next=1, items=[]))

    def test_changed_title_is_rejected_when_saving_groq_score(self):
        item = self.call('work').json['items'][0]
        self.conn.execute("UPDATE jobs SET title='Security Software Engineer 5' WHERE id=?", (item['job']['id'],))
        self.conn.commit()
        job = dict(self.conn.execute('SELECT * FROM jobs WHERE id=?', (item['job']['id'],)).fetchone())
        data = dict(user_id=1, job_id=job['id'], fingerprint=cloud_worker.fingerprint(
            cloud_worker.work_identity(item['user'], job)), result=test_evaluation.sample_result())
        self.assertEqual(self.call('score', data).status_code, 409)

    def test_resolved_title_is_checked_before_full_jd_evaluation(self):
        item = self.call('work').json['items'][0]
        job_id = item['job']['id']
        self.conn.execute("UPDATE companies SET career_source='hotstar' WHERE id=?", (item['job']['company_id'],))
        self.conn.execute('UPDATE jobs SET raw_json=? WHERE id=?', (json.dumps(dict(career_source='hotstar')), job_id))
        self.conn.commit()
        item = next(item for item in self.call('work').json['items'] if item['job']['id'] == job_id)
        response = self.call('description', dict(user_id=1, job_id=job_id, fingerprint=item['fingerprint'],
            title='Security Software Engineer 5', location='Remote', description='<p>Python required</p>', title_gate=True))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json['eligible'])

    def test_resolved_alias_preserves_explicit_role_preference(self):
        self.conn.execute('UPDATE users SET preferences_json=? WHERE id=1',
                          (json.dumps(dict(roles=['Software Engineer'])),))
        self.conn.execute("UPDATE companies SET career_source='hotstar' WHERE id=1")
        self.conn.execute('UPDATE jobs SET raw_json=? WHERE id=1', (json.dumps(dict(career_source='hotstar')),))
        self.conn.commit()
        item = next(item for item in self.call('work').json['items'] if item['job']['id'] == 1)
        response = self.call('description', dict(user_id=1, job_id=1, fingerprint=item['fingerprint'],
            title='Application Programmer', location='Remote', description='<p>Python required</p>', title_gate=True))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json['eligible'])
        data = dict(user_id=1, job_id=1, fingerprint=response.json['fingerprint'], result=test_evaluation.sample_result())
        self.assertEqual(self.call('score', data).json, dict(saved=True))

    def test_end_to_end_worker_generates_once_then_scores_only_matching_title(self):
        self.require_profile()
        self.conn.execute("UPDATE jobs SET title='Software Engineer' WHERE id=1")
        self.conn.execute("UPDATE jobs SET title='Security Software Engineer 5' WHERE id=2")
        self.conn.commit()
        client = MagicMock()

        def hosted(route, data=None):
            response = self.call(route, data)
            self.assertEqual(response.status_code, 200, response.json)
            return response.json

        client.call.side_effect = hosted
        with patch.object(pipeline, 'GROQ_API_KEY', 'synthetic'), \
                patch.dict(pipeline.os.environ, GROQ_API_KEY_2='', GROQ_API_KEY_3='',
                           GROQ_API_KEY_4='', GROQ_API_KEY_5='', GROQ_API_KEY_6='', GROQ_API_KEY_7=''), \
                patch.object(role_titles, 'generate', return_value=profile()) as generate, \
                patch.object(pipeline, 'score_job_for_user', return_value=test_evaluation.sample_result()) as score, \
                patch.object(runner.time, 'monotonic', return_value=0):
            self.assertEqual(runner.score_pending(client, 3000, threading.Event()), 0)
            self.assertEqual(runner.score_pending(client, 3000, threading.Event()), 0)
        generate.assert_called_once()
        score.assert_called_once()
        self.assertEqual(score.call_args.args[1]['id'], 1)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM scores WHERE is_stale=0').fetchone()[0], 1)


class ProfileWorkerTests(unittest.TestCase):
    def required(self):
        return dict(next=1, items=[], role_profile_required=dict(user=dict(id=1, resume_text='Python project'),
                    fingerprint='0' * 64, version=role_titles.VERSION, model=pipeline.GROQ_MODEL))

    def test_failed_or_uncertain_profile_write_never_starts_jd_scoring(self):
        for failure in (requests.Timeout(), runner.HostedInputsChanged(), runner.HostedOperationError(503)):
            client, pool = MagicMock(), MagicMock(reporting_failed=False)
            client.call.side_effect = [self.required(), failure]
            pool.suggest_titles.return_value = profile()
            with patch.object(runner, 'GroqKeyPool', return_value=pool), \
                    patch.object(runner.time, 'monotonic', return_value=0):
                with self.assertRaises(type(failure)):
                    runner.score_pending(client, 3000, threading.Event())
            pool.score.assert_not_called()
            self.assertEqual(client.call.call_count, 2)

    def test_title_requests_share_key_rotation_and_budget_with_evaluations(self):
        client = MagicMock()
        client.call.return_value = dict(saved=True, revision=0)
        response = requests.Response()
        response.status_code = 429
        with patch.object(pipeline, 'GROQ_API_KEY', 'synthetic-1'), \
                patch.dict(pipeline.os.environ, GROQ_API_KEY_2='synthetic-2', GROQ_API_KEY_3='',
                           GROQ_API_KEY_4='', GROQ_API_KEY_5='', GROQ_API_KEY_6='', GROQ_API_KEY_7=''), \
                patch.object(runner.GroqKeyPool, 'publish'), \
                patch.object(runner.time, 'monotonic', return_value=0), \
                patch.object(role_titles, 'generate', side_effect=[requests.HTTPError(response=response), profile()]) as generate, \
                patch.object(pipeline, 'score_job_for_user', return_value={}) as score:
            pool = runner.GroqKeyPool(client, 3000)
            self.assertEqual(pool.suggest_titles(dict(resume_text='Python')), profile())
            pool.score(dict(resume_text='Python'), {}, 'Synthetic')
        self.assertEqual([call.kwargs['api_key'] for call in generate.call_args_list], [None, 'synthetic-2'])
        self.assertEqual({k:v for k,v in score.call_args.kwargs.items() if k!='response_observer'}, dict(api_key='synthetic-2'))

        client, pool = MagicMock(), MagicMock(reporting_failed=False)
        client.call.side_effect = [self.required(), dict(saved=True), dict(next=1, items=[dict(
            user=dict(id=1, resume_text='Python project'), job=dict(id=1), company_name='Synthetic', fingerprint='0' * 64)])]
        pool.suggest_titles.return_value = profile()
        with patch.object(runner, 'GroqKeyPool', return_value=pool), \
                patch.object(runner, 'MAX_SCORES', 1), patch.object(runner.time, 'monotonic', return_value=0):
            self.assertEqual(runner.score_pending(client, 3000, threading.Event()), 0)
        pool.suggest_titles.assert_called_once()
        pool.score.assert_not_called()

    def test_invalid_or_repeated_profile_requests_fail_without_fallback(self):
        for change in (dict(version='old'), dict(model='other'), dict(fingerprint='invalid'),
                       dict(user=dict(id=True, resume_text='Python'))):
            required = self.required()
            required['role_profile_required'].update(change)
            client, pool = MagicMock(), MagicMock(reporting_failed=False)
            client.call.return_value = required
            with patch.object(runner, 'GroqKeyPool', return_value=pool), \
                    patch.object(runner.time, 'monotonic', return_value=0):
                with self.assertRaises(ValueError):
                    runner.score_pending(client, 3000, threading.Event())
            pool.suggest_titles.assert_not_called()
        client, pool = MagicMock(), MagicMock(reporting_failed=False)
        client.call.side_effect = [self.required(), dict(saved=True), self.required()]
        pool.suggest_titles.return_value = profile()
        with patch.object(runner, 'GroqKeyPool', return_value=pool), \
                patch.object(runner.time, 'monotonic', return_value=0):
            with self.assertRaises(RuntimeError):
                runner.score_pending(client, 3000, threading.Event())
        pool.suggest_titles.assert_called_once()
        pool.score.assert_not_called()
