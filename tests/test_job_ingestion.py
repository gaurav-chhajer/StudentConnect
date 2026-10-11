import json
import unittest
from unittest.mock import patch

import requests

from backend import career_sources
from backend.job_ingestion import deduplicate, mode_for_source, observation_batches


class JobIngestionTests(unittest.TestCase):
    def test_source_policy_and_invalid_configuration(self):
        for source in ('wipro', 'jpmorgan', 'cvent', 'hotstar', 'nomura', 'barclays'):
            self.assertEqual(mode_for_source(source), 'observed')
        for source in (None, 'morganstanley', 'sapient'):
            self.assertEqual(mode_for_source(source), 'snapshot')
        for source in ('unknown', 'icici'):
            with self.assertRaises(ValueError):
                mode_for_source(source)

    def test_conflicting_id_is_withheld_even_if_it_reappears(self):
        first = dict(external_id='1', title='Analyst', raw_json={})
        changed = dict(first, title='Engineer')
        with self.assertLogs('backend.job_ingestion', level='WARNING'):
            self.assertEqual(deduplicate([first, changed, first], 'observed'), [])
        with self.assertRaises(ValueError):
            deduplicate([first, changed], 'snapshot')

    def test_batches_obey_count_and_exact_encoded_size(self):
        envelope = dict(company_id=1, fingerprint='x' * 64, mode='observed')
        jobs = [dict(external_id=str(index), title='Engineer ' + '\u20b9' * 20) for index in range(5)]
        batches = list(observation_batches(envelope, jobs, 2, 450))
        self.assertEqual([job for batch in batches for job in batch['jobs']], jobs)
        self.assertTrue(all(len(batch['jobs']) <= 2 and len(json.dumps(batch).encode()) <= 450 for batch in batches))
        self.assertEqual(list(observation_batches(envelope, [], 2, 450)), [dict(envelope, jobs=[])])
        with self.assertRaises(ValueError):
            list(observation_batches(envelope, jobs, 2, 10))

    def test_timeout_keeps_validated_pages_and_closes_session(self):
        page = dict(total=2, jobPostings=[dict(title='Engineer', externalPath='/job/India/A_1', locationsText='India')])
        with patch.object(career_sources.Feed, 'json', side_effect=[page, requests.Timeout]), \
                patch.object(career_sources.Feed, 'close') as close, \
                self.assertLogs('backend.career_sources', level='WARNING'):
            self.assertEqual(len(career_sources.fetch('hotstar')), 1)
        close.assert_called_once()
        with patch.object(career_sources.Feed, 'json', side_effect=requests.Timeout), self.assertRaises(requests.Timeout):
            career_sources.fetch('hotstar')
