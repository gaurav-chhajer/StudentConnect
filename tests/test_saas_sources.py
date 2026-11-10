"""Offline coverage for the SaaS catalog and bounded full-description boards."""

import io
import json
import sqlite3
import unittest
from contextlib import closing, redirect_stdout
from unittest.mock import patch

import requests

from backend import career_sources as sources, pipeline_skeleton as pipeline
from backend.job_ingestion import mode_for_source
from backend.migrate import apply_migrations
from scripts.run_local_fetch import seed_companies


class SaasSourceTests(unittest.TestCase):
    def test_worldwide_boards_fetch_full_descriptions_and_reactivate_saved_catalog_rows(self):
        for key in sources.VERIFIED_WORLDWIDE_SOURCES:
            kind = sources.SOURCES[key][1]
            if kind == 'lever':
                continue  # Lever's array response and description sections are covered separately.
            payload = self.payload('airbnb' if kind == 'greenhouse' else 'notion')
            with self.subTest(key=key), patch.object(sources.Feed, 'json', return_value=payload) as request:
                rows = sources.fetch(key)
            slug = sources.BOARD_SLUGS.get(key, key)
            expected = ('/v1/boards/' + slug + '/jobs?content=true' if kind == 'greenhouse'
                        else '/posting-api/job-board/' + slug)
            self.assertEqual(request.call_args.args[0], expected)
            self.assertEqual(mode_for_source(key), 'snapshot')
            self.assertIn('FINAL REQUIREMENT', pipeline.extract_job_description(
                dict(rows[0], raw_json=json.dumps(rows[0]['raw_json']))))
        with closing(sqlite3.connect(':memory:')) as conn:
            conn.executescript(pipeline.SCHEMA)
            apply_migrations(conn)
            with redirect_stdout(io.StringIO()):
                seed_companies(conn)
                conn.execute('UPDATE companies SET active=0')
                before = dict(conn.execute('SELECT career_source,id FROM companies WHERE career_source IS NOT NULL'))
                seed_companies(conn)
            for key in sources.VERIFIED_WORLDWIDE_SOURCES:
                self.assertEqual(conn.execute('SELECT id,active FROM companies WHERE career_source=?',
                                             (key,)).fetchone(), (before[key], 1))
            self.assertEqual(conn.execute("SELECT active FROM companies WHERE career_source='clutch'").fetchone(), (0,))

    def test_lever_preserves_all_sections_and_rejects_invalid_snapshots(self):
        payload = [dict(id='123', text='Engineer', categories=dict(location='Remote', team='Engineering'),
                        hostedUrl='https://jobs.lever.co/Instrumentl/123', description='<p>Opening</p>',
                        lists=[dict(content='<p>Requirement</p>')], additional='<p>FINAL REQUIREMENT</p>')]
        with patch.object(sources.Feed, 'request', return_value=json.dumps(payload)) as request:
            rows = sources.fetch('instrumentl')
        self.assertEqual(request.call_args.args[0], '/v0/postings/Instrumentl')
        description = pipeline.extract_job_description(dict(rows[0], raw_json=json.dumps(rows[0]['raw_json'])))
        for text in ('Opening', 'Requirement', 'FINAL REQUIREMENT'):
            self.assertIn(text, description)
        for invalid in ({}, [None], [dict(payload[0], lists=[None])],
                        [dict(payload[0], description='', lists=[], additional='')], payload * 2):
            with self.subTest(invalid=invalid), patch.object(sources.Feed, 'request', return_value=json.dumps(invalid)):
                with self.assertRaises((ValueError, TypeError)):
                    sources.fetch('instrumentl')
        with patch.object(sources.Feed, 'request', side_effect=requests.Timeout), self.assertRaises(requests.Timeout):
            sources.fetch('instrumentl')

    def payload(self, key, title='Engineer', description='<p>Python FINAL REQUIREMENT</p>'):
        if key == 'airbnb':
            return dict(meta=dict(total=1), jobs=[dict(
                id=123, title=title, location=dict(name='India'), departments=[dict(name='Engineering')],
                absolute_url='https://careers.airbnb.com/positions/123/', content=description)])
        return dict(jobs=[dict(id='123', title=title, location='India', department='Engineering',
                              jobUrl='https://jobs.ashbyhq.com/notion/123', descriptionHtml=description,
                              employmentType='FullTime', descriptionPlain='Python FINAL REQUIREMENT')])

    def test_native_boards_preserve_descriptions_metadata_and_snapshot_policy(self):
        for key, path in [('airbnb', '/v1/boards/airbnb/jobs?content=true'),
                          ('notion', '/posting-api/job-board/notion')]:
            with self.subTest(key=key), patch.object(sources.Feed, 'json', return_value=self.payload(key)) as request:
                rows = sources.fetch(key)
            self.assertEqual(request.call_args.args[0], path)
            self.assertEqual(mode_for_source(key), 'snapshot')
            self.assertEqual(rows[0]['department'], 'Engineering')
            candidate = dict(rows[0], raw_json=json.dumps(rows[0]['raw_json']))
            with patch.object(sources.Feed, 'request') as request:
                self.assertEqual(sources.prepare_job(candidate), candidate)
            request.assert_not_called()
            self.assertIn('FINAL REQUIREMENT', pipeline.extract_job_description(candidate))
            if key == 'notion':
                self.assertEqual(rows[0]['raw_json']['employmentType'], 'FullTime')
            else:
                self.assertNotIn('content', rows[0]['raw_json'])
            with patch.object(sources.Feed, 'json', return_value=self.payload(key, title='Senior Engineer')):
                self.assertEqual(sources.fetch(key), [])

    def test_native_boards_reject_invalid_incomplete_and_failed_snapshots(self):
        for key in ('airbnb', 'notion'):
            invalid = [dict(jobs=None), dict(jobs=[None]), self.payload(key, description=''),
                       self.payload(key, description='x' * (sources.MAX_DESCRIPTION + 1))]
            if key == 'airbnb':
                invalid += [dict(meta=dict(total=2), jobs=self.payload(key)['jobs'])]
            for field, value in [('location', 123), ('department', ['Engineering']),
                                 ('title', None), ('id', True)]:
                payload = self.payload(key)
                payload['jobs'][0][field] = value
                if key == 'airbnb' and field == 'department':
                    payload['jobs'][0]['departments'] = [dict(name=123)]
                invalid.append(payload)
            for payload in invalid:
                with self.subTest(key=key, payload_type=type(payload)), \
                        patch.object(sources.Feed, 'json', return_value=payload), \
                        patch.object(sources.Feed, 'close') as close:
                    with self.assertRaises((ValueError, KeyError, TypeError)):
                        sources.fetch(key)
                close.assert_called_once()
            duplicate = self.payload(key)
            duplicate['jobs'] *= 2
            if key == 'airbnb':
                duplicate['meta']['total'] = 2
            with patch.object(sources.Feed, 'json', return_value=duplicate), self.assertRaises(ValueError):
                sources.fetch(key)
            with patch.object(sources.Feed, 'json', side_effect=requests.Timeout), self.assertRaises(requests.Timeout):
                sources.fetch(key)
            with patch.object(sources.Feed, 'json', return_value=dict(meta=dict(total=0), jobs=[])):
                self.assertEqual(sources.fetch(key), [])

    def test_workday_boards_use_verified_tenants_and_preserve_full_details(self):
        for key, tenant, site in [('salesforce', 'salesforce', 'External_Career_Site'),
                                  ('adobe', 'adobe', 'external_experienced'),
                                  ('slack', 'salesforce', 'Slack')]:
            payload = dict(total=1, jobPostings=[dict(title='Engineer',
                           externalPath='/job/India/Engineer_JR1', locationsText='India')])
            with self.subTest(key=key), patch.object(sources.Feed, 'json', return_value=payload) as request:
                row = sources.fetch(key)[0]
            self.assertEqual(request.call_args.args[0], f'/wday/cxs/{tenant}/{site}/jobs')
            self.assertEqual(mode_for_source(key), 'observed')
            detail = dict(jobPostingInfo=dict(title='Engineer', location='India',
                          jobDescription='<p>Complete FINAL REQUIREMENT</p>'))
            with patch.object(sources.Feed, 'json', return_value=detail):
                prepared = sources.prepare_job(dict(row, raw_json=json.dumps(row['raw_json'])))
            self.assertIn('FINAL REQUIREMENT', pipeline.extract_job_description(prepared))

    def test_catalog_import_is_idempotent_and_zoom_cannot_fetch(self):
        with closing(sqlite3.connect(':memory:')) as conn:
            conn.execute('PRAGMA foreign_keys=ON')
            conn.executescript(pipeline.SCHEMA)
            apply_migrations(conn)
            with redirect_stdout(io.StringIO()):
                seed_companies(conn)
                before = conn.execute('SELECT * FROM companies ORDER BY id').fetchall()
                seed_companies(conn)
            self.assertEqual(before, conn.execute('SELECT * FROM companies ORDER BY id').fetchall())
            states = dict(conn.execute('SELECT name,active FROM companies'))
            for name in ('Salesforce', 'Adobe', 'Spotify', 'Slack', 'Airbnb', 'Notion', 'Figma', 'Asana', 'Dropbox'):
                self.assertEqual(states[name], 1)
            self.assertEqual(states['Zoom'], 0)
            self.assertEqual(conn.execute('PRAGMA foreign_key_check').fetchall(), [])
        with patch.object(sources.requests.Session, 'request') as request, self.assertRaises(ValueError):
            sources.fetch('zoom')
        request.assert_not_called()
