"""Sync identity, additive preservation and authoritative profile boundaries."""

import json
import sqlite3
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

from scripts import sync_hosted
from tests.test_cloud_worker import CloudEndpointTests


class ObservationEndpointTests(CloudEndpointTests):
    def test_catalog_adds_verified_hsbc_once_without_changing_settings(self):
        for _ in range(2):
            self.assertEqual(self.call('sync-catalog', {'sources': ['hsbc']}).status_code, 200)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM companies WHERE career_source='hsbc'").fetchone()[0], 1)
        self.assertEqual(self.call('sync-catalog', {'sources': ['unknown']}).status_code, 400)
        self.assertEqual(self.call('sync-catalog', {'sources': ['meta']}).status_code, 400)

    def test_partial_observations_preserve_other_hosted_jobs(self):
        company = self.call('companies').json['companies'][0]
        before = self.conn.execute('SELECT count(*) FROM jobs WHERE is_active=1').fetchone()[0]
        job = dict(external_id='sync-new', title='Software Engineer', location='Remote',
                   department=None, url='https://example.test/job', raw_json={'content': 'Python SQL'})
        payload = dict(company_id=company['id'], fingerprint=company['fingerprint'],
                       mode='observed', jobs=[job])
        self.assertEqual(self.call('observations', payload).status_code, 200)
        self.assertEqual(self.call('observations', payload).status_code, 200)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM jobs WHERE is_active=1').fetchone()[0], before + 1)
        payload['fingerprint'] = '0' * 64
        self.assertEqual(self.call('observations', payload).status_code, 409)
        payload['fingerprint'] = company['fingerprint']
        payload['jobs'][0]['url'] = 'javascript:bad'
        self.assertEqual(self.call('observations', payload).status_code, 400)


class SyncTests(unittest.TestCase):
    def test_unavailable_description_does_not_block_following_hosted_score(self):
        import requests
        scorer, client = MagicMock(), MagicMock()
        def item(number):
            return dict(user={'id': 1}, job={'id': number}, company_name='Example', fingerprint='a'*64)
        client.call.side_effect = [dict(next=1, job_after=2, items=[item(1),item(2)]),
                                   dict(saved=True), dict(next=None, job_after=0, items=[])]
        with patch.object(sync_hosted.run_similarity.career_sources, 'prepare_job',
                          side_effect=[requests.HTTPError('private'), {'id':2}]), \
                self.assertLogs(sync_hosted.run_similarity.logger):
            self.assertEqual(sync_hosted.run_similarity.score_pending(client,time.monotonic()+300,scorer),1)
        scorer.score.assert_called_once()

    def test_maps_company_identity_and_never_transmits_local_ids_or_accounts(self):
        conn = sqlite3.connect(':memory:')
        self.addCleanup(conn.close)
        conn.row_factory = sqlite3.Row
        conn.executescript('''CREATE TABLE companies(id,ats_type,board_slug,career_source,active);
            CREATE TABLE jobs(id,company_id,external_id,title,location,department,url,raw_json,is_active);
            INSERT INTO companies VALUES(7,'lever','example',NULL,1);''')
        conn.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?)',
                     (19,7,'stable-id','Engineer','Remote',None,'https://example.test/job',json.dumps({'content':'Python'}),1))
        client = MagicMock()
        client.call.side_effect = [dict(companies=[dict(id=123, ats_type='lever',board_slug='example',
            career_source=None,fingerprint='a'*64)]), dict(saved=1)]
        with patch.object(sync_hosted.local_embeddings, 'LocalScorer'), \
                patch.object(sync_hosted.run_similarity, 'score_pending', return_value=0) as score:
            self.assertTrue(sync_hosted.synchronize(conn,client,time.monotonic()+300,threading.Event()))
        route, payload = client.call.call_args.args
        self.assertEqual(route,'observations')
        self.assertEqual(payload['company_id'],123)
        self.assertEqual(payload['jobs'][0]['external_id'],'stable-id')
        self.assertNotIn('id',payload['jobs'][0])
        self.assertNotIn('user',payload)
        score.assert_called_once()
