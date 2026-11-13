"""Offline verification of public feed shapes and snapshot safety."""

import json
import unittest
from unittest.mock import patch

from backend import career_sources as sources


class CareerSourceTests(unittest.TestCase):
    def google_candidate(self):
        url = 'https://www.google.com/about/careers/applications/jobs/results/90108035430523590-software-engineer/'
        candidate = sources.job('google', '90108035430523590', 'Software Engineer', 'India', url)
        candidate['raw_json'] = json.dumps(candidate['raw_json'])
        return candidate

    def test_google_captured_not_found_is_unavailable_not_identity_failure(self):
        from pathlib import Path
        page = (Path(__file__).parent / 'fixtures/google_job_not_found.html').read_text(encoding='utf-8')
        with patch.object(sources.Feed, 'request', return_value=page):
            with self.assertRaises(sources.GoogleJobUnavailable):
                sources.prepare_job(self.google_candidate())
        with patch.object(sources.Feed, 'request', return_value=page.replace('90108035430523590-software', '999-software')):
            with self.assertRaisesRegex(ValueError, 'identity changed'):
                sources.prepare_job(self.google_candidate())

    def test_google_incomplete_cached_or_downloaded_description_is_not_scoreable(self):
        from backend.pipeline_skeleton import extract_job_description
        candidate = self.google_candidate()
        raw = json.loads(candidate['raw_json'])
        raw['descriptionHtml'] = '<p>Software Engineer teaser</p>'
        candidate['raw_json'] = json.dumps(raw)
        with self.assertRaises(sources.CareerDescriptionIncomplete):
            sources.prepare_job(candidate)
        with self.assertRaises(sources.CareerDescriptionIncomplete):
            extract_job_description(candidate)
        empty = '<h3>Minimum qualifications:</h3><h3>About the job</h3><h3>Responsibilities</h3>'
        with self.assertRaises(sources.CareerDescriptionIncomplete):
            sources.validate_google_description(empty)

    def test_google_complete_selected_description_keeps_identity_and_sections(self):
        candidate = self.google_candidate()
        description = '<div class="DkhPwc"><h3>Minimum qualifications:</h3><ul><li>Python experience</li></ul><h3>About the job</h3><p>Build services</p><h3>Responsibilities</h3><p>Deliver services</p></div>'
        page = '<title>Software Engineer — Google Careers</title><link rel="canonical" href="' + candidate['url'] + '">' + description
        with patch.object(sources.Feed, 'request', return_value=page):
            result = sources.prepare_job(candidate)
        self.assertEqual(json.loads(result['raw_json'])['descriptionHtml'], description)

    def test_hsbc_uses_verified_feed_and_pagination(self):
        item = dict(id=123, name='Analyst', locations=['Pune, India'])
        pages = [dict(count=2, positions=[item]),
                 dict(count=2, positions=[dict(item, id=124)])]
        with patch.object(sources.Feed, 'json', side_effect=pages) as request:
            jobs = sources.fetch('hsbc')
        self.assertEqual(len(jobs), 2)
        self.assertEqual(request.call_args.kwargs['params'],
                         dict(domain='hsbc.com', start=1, num=10))
        self.assertEqual(jobs[0]['url'], 'https://portal.careers.hsbc.com/careers/job/123')
        with patch.object(sources.Feed, 'json', return_value=dict(
                count=1, positions=[dict(item, id='../123')])):
            with self.assertRaises(ValueError):
                sources.fetch('hsbc')

    def test_synechron_uses_official_workday_board(self):
        payload=dict(total=1,jobPostings=[dict(title='Python Developer',externalPath='/job/India/Python_JR1',locationsText='Pune, India')])
        with patch.object(sources.Feed,'json',return_value=payload) as request:
            jobs=sources.fetch('synechron')
        self.assertEqual(request.call_args.args[0],'/wday/cxs/synechron/SynechronCareers/jobs')
        self.assertEqual(jobs[0]['url'],'https://synechron.wd1.myworkdayjobs.com/en-US/SynechronCareers/job/India/Python_JR1')

    def test_postman_uses_verified_workday_tenant_and_site(self):
        payload = dict(total=1, jobPostings=[dict(title='Engineer',
                       externalPath='/job/India/Engineer_JR1', locationsText='Bangalore')])
        with patch.object(sources.Feed, 'json', return_value=payload) as request:
            jobs = sources.fetch('postman')
        self.assertEqual(request.call_args.args[0], '/wday/cxs/postman/careers/jobs')
        self.assertEqual(jobs[0]['url'],
                         'https://postman.wd108.myworkdayjobs.com/en-US/careers/job/India/Engineer_JR1')

    def test_jibe_complete_pages_and_description_change_fingerprint(self):
        item = dict(slug='123', title='Analyst', language='en-us',
                    full_location='Pune, India', description='<p>Complete JD</p>',
                    employment_type='FULL_TIME')
        for key in ('cvent', 'zs'):
            pages = [dict(totalCount=2, jobs=[dict(data=item)]),
                     dict(totalCount=2, jobs=[dict(data=dict(item, slug='124'))])]
            with self.subTest(key=key), patch.object(sources.Feed, 'json', side_effect=pages) as request:
                jobs = sources.fetch(key)
            self.assertEqual(len(jobs), 2)
            self.assertEqual(request.call_args.kwargs['params'], dict(page=2, limit=100))
            self.assertNotIn('descriptionHtml', jobs[0]['raw_json'])
            self.assertEqual(jobs[0]['raw_json']['employmentType'], 'FULL_TIME')
            changed = dict(item, description='<p>Changed requirements</p>')
            with patch.object(sources.Feed, 'json', return_value=dict(totalCount=1, jobs=[dict(data=changed)])):
                refreshed = sources.fetch(key)[0]
            self.assertNotEqual(jobs[0]['raw_json']['listing_version'], refreshed['raw_json']['listing_version'])

    def test_jibe_invalid_and_incomplete_snapshots_are_withheld(self):
        item = dict(slug='123', title='Analyst', language='en-us')
        for pages in ([dict(totalCount=2, jobs=[dict(data=item)]), dict(totalCount=2, jobs=[])],
                      [dict(totalCount=2, jobs=[dict(data=item)]), dict(totalCount=2, jobs=[dict(data=item)])],
                      [dict(totalCount=2, jobs=[dict(data=item)]), dict(totalCount=3, jobs=[])],
                      [dict(totalCount=1, jobs=[dict(data=dict(item, slug='../123'))])],
                      [dict(totalCount=1, jobs=[dict(data=dict(item, language='other'))])]):
            with self.subTest(pages=pages), patch.object(sources.Feed, 'json', side_effect=pages):
                if len(pages) > 1:
                    self.assertEqual(len(sources.fetch('cvent')), 1)
                else:
                    with self.assertRaises(ValueError):
                        sources.fetch('cvent')
        with patch.object(sources.Feed, 'json', return_value=dict(totalCount=0, jobs=[])):
            self.assertEqual(sources.fetch('zs'), [])

    def test_jibe_details_preserve_full_description_and_validate_identity(self):
        for key in ('cvent', 'zs'):
            candidate = sources.job(key, '123', 'Analyst', 'Pune, India',
                                    sources.SOURCES[key][2] + '/jobs/123?lang=en-us')
            candidate['raw_json'] = json.dumps(candidate['raw_json'])
            detail = dict(slug='123', language='en-us', title='Analyst',
                          full_location='Pune, India', description='<p>Full JD FINAL REQUIREMENT</p>')
            with self.subTest(key=key), patch.object(sources.Feed, 'json', return_value=detail) as request:
                prepared = sources.prepare_job(candidate)
            self.assertEqual(request.call_args.args[0], '/api/jobs/123/en-us')
            self.assertIn('FINAL REQUIREMENT', json.loads(prepared['raw_json'])['descriptionHtml'])
            for changed in (dict(detail, slug='124'), dict(detail, language='other'),
                            dict(detail, title='Different title'), dict(detail, description='')):
                with patch.object(sources.Feed, 'json', return_value=changed), self.assertRaises(ValueError):
                    sources.prepare_job(candidate)
            with patch.object(sources.Feed, 'json') as request, self.assertRaises(ValueError):
                sources.prepare_job(dict(candidate, url='https://example.test/jobs/123'))
            request.assert_not_called()

    def test_workday_complete_pages_and_stable_metadata(self):
        pages = [dict(total=2, jobPostings=[dict(title='Analyst', externalPath='/job/India/A_1', locationsText='India')]),
                 dict(total=0, jobPostings=[dict(title='Engineer', externalPath='/job/India/B_2', locationsText='India')])]
        with patch.object(sources.Feed, 'json', side_effect=pages) as request:
            jobs = sources.fetch('hotstar')
        self.assertEqual(len(jobs), 2)
        self.assertEqual(request.call_args_list[1].kwargs['data']['offset'], 1)
        self.assertNotIn('descriptionHtml', jobs[0]['raw_json'])
        self.assertEqual(jobs[0]['raw_json']['career_source'], 'hotstar')

    def test_complete_empty_and_incomplete_duplicate_changed_count(self):
        item = dict(title='Analyst', externalPath='/job/India/A_1', locationsText='India')
        for pages in ([dict(total=2, jobPostings=[item]), dict(total=2, jobPostings=[])],
                      [dict(total=2, jobPostings=[item]), dict(total=2, jobPostings=[item])],
                      [dict(total=2, jobPostings=[item]), dict(total=3, jobPostings=[item])]):
            with self.subTest(pages=pages), patch.object(sources.Feed, 'json', side_effect=pages):
                self.assertEqual(len(sources.fetch('hotstar')), 1)
        with patch.object(sources.Feed, 'json', return_value=dict(total=0, jobPostings=[])):
            self.assertEqual(sources.fetch('hotstar'), [])

    def test_senior_filter_matches_existing_pipeline_rule(self):
        items = [dict(title=title, externalPath='/job/India/A_' + str(index), locationsText='India')
                 for index, title in enumerate(['Analyst', 'Senior Analyst', 'Engineering Manager'])]
        with patch.object(sources.Feed, 'json', return_value=dict(total=3, jobPostings=items)):
            self.assertEqual([job['title'] for job in sources.fetch('bofa')], ['Analyst'])

    def test_oracle_uses_advertised_count_and_known_site(self):
        payload = dict(items=[dict(TotalJobsCount=1, requisitionList=[dict(Id='123', Title='Analyst', PrimaryLocation='India')])])
        with patch.object(sources.Feed, 'json', return_value=payload) as request:
            jobs = sources.fetch('dpworld')
        self.assertIn('siteNumber=CX_1', request.call_args.kwargs['params']['finder'])
        self.assertTrue(jobs[0]['url'].endswith('/CX_1/job/123'))

    def test_idfc_embedded_data_requires_complete_pagination(self):
        item = dict(jobId='P-1', title='Analyst', jobSeqNo='IFBAINP1ENIN', cityStateCountry='India')
        text = 'phApp.ddo = ' + json.dumps(dict(eagerLoadRefineSearch=dict(totalHits=1, data=dict(jobs=[item])))) + ';'
        with patch.object(sources.Feed, 'request', return_value=text):
            self.assertEqual(sources.fetch('idfc')[0]['external_id'], 'P-1')
        with patch.object(sources.Feed, 'request', return_value='<html>Error</html>'):
            with self.assertRaises(ValueError):
                sources.fetch('idfc')

    def test_no_guessed_sources_or_unverified_sources(self):
        for key in ('unknown', 'icici', 'futuresfirst'):
            with self.assertRaises(ValueError):
                sources.fetch(key)

    def test_idfc_detail_uses_embedded_full_description_and_checks_identity(self):
        candidate = sources.job('idfc', 'P-1', 'DevOps Engineer', 'India',
                                'https://careers.idfcfirst.bank.in/in/en/job/IFBAINP1ENIN/DevOps-Engineer')
        candidate['raw_json'] = json.dumps(candidate['raw_json'])
        detail = dict(jobId='P-1', jobSeqNo='IFBAINP1ENIN', title='DevOps Engineer',
                      cityStateCountry='Hyderabad, India', type='Full-Time',
                      description='<p>Complete description FINAL REQUIREMENT</p>',
                      descriptionTeaser='Short teaser')

        def page(job, **changes):
            payload = dict(status=200, hits=1, totalHits=1, data=dict(job=job))
            payload.update(changes)
            return ('<script type="application/ld+json"></script>phApp.ddo = ' +
                    json.dumps(dict(jobDetail=payload)) + ';')

        with patch.object(sources.Feed, 'request', return_value=page(detail)), \
                patch.object(sources.Feed, 'close') as close:
            prepared = sources.prepare_job(candidate)
        close.assert_called_once()
        self.assertEqual(prepared['location'], 'Hyderabad, India')
        self.assertIn('FINAL REQUIREMENT', json.loads(prepared['raw_json'])['descriptionHtml'])
        self.assertEqual(json.loads(prepared['raw_json'])['employmentType'], 'Full-Time')
        invalid_pages = [page(dict(detail, **change)) for change in (
            dict(jobId='P-2'), dict(jobSeqNo='OTHER'), dict(title='Other title'),
            dict(description=''), dict(description=None), dict(description=[]),
            dict(cityStateCountry=[]), dict(type=[]))]
        invalid_pages.extend((page(detail, status=404), page(detail, hits=0), page(detail, hits=True),
                              page(detail, totalHits=2), page(None),
                              '<html>No embedded data</html>', 'phApp.ddo = invalid'))
        for text in invalid_pages:
            with self.subTest(text=text), patch.object(sources.Feed, 'request', return_value=text), \
                    patch.object(sources.Feed, 'close') as close, self.assertRaises(ValueError):
                sources.prepare_job(candidate)
            close.assert_called_once()

    def test_description_host_allowlist_deadline_and_cleanup(self):
        feed = sources.Feed('hotstar', 0)
        self.addCleanup(feed.close)
        with patch.object(feed.session, 'request') as request:
            for path in ('https://example.test/job', '//127.0.0.1/job', '/jobs'):
                with self.assertRaises(ValueError):
                    feed.request(path)
            request.assert_not_called()

    def test_candidate_description_is_full_and_identity_checked(self):
        job = sources.job('hotstar', '1', 'Analyst', 'India',
                          'https://jiostar.wd102.myworkdayjobs.com/en-US/JioStar/job/India/A_1')
        job['raw_json'] = json.dumps(job['raw_json'])
        with patch.object(sources.Feed, 'json', return_value=dict(jobPostingInfo=dict(
                title='Analyst', location='Mumbai', jobDescription='<p>Full description tail</p>'))):
            prepared = sources.prepare_job(job)
        self.assertEqual(prepared['location'], 'Mumbai')
        self.assertIn('Full description tail', json.loads(prepared['raw_json'])['descriptionHtml'])
        with patch.object(sources.Feed, 'json', return_value=dict(jobPostingInfo=dict(
                title='Changed title', jobDescription='<p>Full description</p>'))):
            with self.assertRaises(ValueError):
                sources.prepare_job(job)

    def test_workday_identifier_is_stable_when_title_slug_changes(self):
        identifiers = []
        for path in ('/job/India/Analyst_JR1', '/job/India/Analyst-New_JR1'):
            payload = dict(total=1, jobPostings=[dict(title='Analyst', externalPath=path, locationsText='2 Locations')])
            with patch.object(sources.Feed, 'json', return_value=payload):
                result = sources.fetch('hotstar')[0]
            identifiers.append(result['external_id'])
            self.assertIsNone(result['location'])
        self.assertEqual(identifiers, ['JR1', 'JR1'])

    def test_pwc_denied_detail_requires_independent_empty_search_before_retiring(self):
        import requests
        candidate = sources.job('pwc', '123WD-3', 'Analyst', 'India',
                                sources.SOURCES['pwc'][2] + '/en-US/Global_Experienced_Careers/job/India/A_123WD-3')
        candidate['raw_json'] = json.dumps(candidate['raw_json'])
        response = requests.Response()
        response.status_code = 403
        denied = requests.HTTPError(response=response)
        with patch.object(sources.Feed, 'json', side_effect=[denied, dict(total=0, jobPostings=[])]) as request:
            with self.assertRaises(sources.CareerJobUnavailable):
                sources.prepare_job(candidate)
        self.assertEqual(request.call_args.kwargs['data']['searchText'], '123WD')
        for search in (dict(total=1, jobPostings=[{}]), dict(total=False, jobPostings=[]),
                       dict(total=0), dict(total=0, jobPostings=[{}])):
            with self.subTest(search=search), patch.object(sources.Feed, 'json', side_effect=[denied, search]):
                with self.assertRaises(requests.HTTPError):
                    sources.prepare_job(candidate)
        for failure in (requests.Timeout(), requests.ConnectionError()):
            with patch.object(sources.Feed, 'json', side_effect=[denied, failure]):
                with self.assertRaises(type(failure)):
                    sources.prepare_job(candidate)
        response.status_code = 429
        with patch.object(sources.Feed, 'json', side_effect=denied) as request:
            with self.assertRaises(requests.HTTPError):
                sources.prepare_job(candidate)
        request.assert_called_once()

    def test_structured_job_requires_description_not_listing_summary(self):
        html = '<script type="application/ld+json">' + json.dumps(dict(
            **{'@type': 'JobPosting'}, title='Analyst', description='<p>Requirements</p>')) + '</script>'
        self.assertEqual(sources.structured_job(html)['title'], 'Analyst')
        with self.assertRaises(ValueError):
            sources.structured_job('<html>No structured job</html>')

    def test_sap_description_preserves_tail_and_self_closing_tags(self):
        for html in ('<span class="jobdescription"><p>Python<br/>SQL &amp; tail</p></span>',
                     '<span class="joblayouttoken-label">Job Description: </span><span class="rtltextaligneligible"><p>Python<br/>SQL &amp; tail</p></span>'):
            description = ''.join(sources.Description(html).parts)
            self.assertIn('SQL &amp; tail', description)
            self.assertIn('<br/>', description)

    def test_sap_and_nomura_complete_listings(self):
        payload = dict(totalJobs=1, jobSearchResult=[dict(response=dict(
            id='1', unifiedStandardTitle='Analyst', jobLocationShort=['Mumbai<br/>']))])
        with patch.object(sources.Feed, 'json', return_value=payload):
            self.assertEqual(sources.fetch('wipro')[0]['location'], 'Mumbai')
        html = 'Results 1 to 1 of 1<tr class="data-row"><td><a class="jobTitle-link" href="/Nomura/job/Analyst/1/">Analyst</a><span class="jobLocation">Mumbai, IN</span></td></tr>'
        with patch.object(sources.Feed, 'request', return_value=html):
            self.assertEqual(sources.fetch('nomura')[0]['location'], 'Mumbai, IN')

    def test_oracle_requires_full_description_and_identity(self):
        candidate = sources.job('dpworld', '1', 'Analyst', 'India',
                                 'https://ehpv.fa.em2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/job/1')
        candidate['raw_json'] = json.dumps(candidate['raw_json'])
        payload = dict(items=[dict(Id='1', Title='Analyst', ExternalDescriptionStr='<p>Full JD</p>',
                                  ExternalQualificationsStr='<p>Python</p>')])
        with patch.object(sources.Feed, 'json', return_value=payload) as request:
            result = sources.prepare_job(candidate)
        self.assertIn('recruitingCEJobRequisitionDetails', request.call_args.args[0])
        self.assertIn('Python', json.loads(result['raw_json'])['descriptionHtml'])
        payload['items'][0].pop('ExternalDescriptionStr')
        with patch.object(sources.Feed, 'json', return_value=payload), self.assertRaises(ValueError):
            sources.prepare_job(candidate)

    def test_barclays_excludes_recommended_jobs_outside_results(self):
        html = '<div data-total-job-results="1"></div><div id="search-results-list"><a href="/job/analyst/1" data-job-id="1"><strong>Analyst</strong></a></div><a href="/job/recommended/2" data-job-id="2">Other Analyst</a>'
        with patch.object(sources.Feed, 'request', return_value=html):
            self.assertEqual([job['external_id'] for job in sources.fetch('barclays')], ['1'])

    def test_invalid_stored_data_and_missing_career_metadata_fail_without_network(self):
        from backend import pipeline_skeleton
        for raw in ('[]', 'null', '{"career_source": []}'):
            with self.assertRaises(ValueError):
                sources.prepare_job(dict(raw_json=raw))
        with patch.object(pipeline_skeleton.requests, 'get') as request:
            with self.assertRaises(ValueError):
                pipeline_skeleton.fetch_jobs_for_company(dict(ats_type='greenhouse', board_slug='career-site-wipro'))
            request.assert_not_called()
