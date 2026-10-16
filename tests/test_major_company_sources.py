"""Offline fixtures for the verified public enterprise career formats."""

import json
import unittest
from unittest.mock import patch

from backend import career_sources as sources


def apple_page(section, data):
    if section == 'search':
        data = dict(data, filters=dict(locations=[dict(id='postLocation-INDC')]))
    payload = dict(loaderData={section: data}, errors=None)
    return '<script>window.__staticRouterHydrationData = JSON.parse(' + json.dumps(json.dumps(payload)) + ');</script>'


def google_card(identifier='123', title='Engineer', full=False):
    return ('<li class="lLd3Je"><h3 class="Ki3IFe">' + title + '</h3>'
            '<span class="r0wTof ">Bengaluru, India</span>'
            '<a href="jobs/results/' + identifier + '-engineer">Learn more</a>'
            '<h3>Minimum qualifications:</h3><ul><li>Python &amp; SQL</li></ul>' +
            ('<h3>About the job</h3><p>Build systems</p><h3>Responsibilities</h3><ul><li>FINAL REQUIREMENT</li></ul>' if full else '') + '</li>')


def google_detail(identifier='123', title='Engineer', full=True):
    return ('<title>' + title + ' — Google Careers</title><link rel="canonical" '
            'href="https://www.google.com/about/careers/applications/jobs/results/' + identifier + '-engineer">' +
            google_card('999', title='Unrelated search result') + '<div class="DkhPwc ">' +
            '<h3>Minimum qualifications:</h3><ul><li>Python &amp; SQL</li></ul>' +
            ('<h3>About the job</h3><p>Build systems</p><h3>Responsibilities</h3><ul><li>FINAL REQUIREMENT</li></ul>' if full else '') + '</div>')


class MajorCompanySourceTests(unittest.TestCase):
    def candidate(self, key, identifier, url):
        result = sources.job(key, identifier, 'Engineer', 'India', url)
        result['raw_json'] = json.dumps(result['raw_json'])
        return result

    def test_pwc_reuses_verified_workday_protocol(self):
        payload = dict(total=1, jobPostings=[dict(title='Engineer', externalPath='/job/India/Engineer_123WD', locationsText='India')])
        with patch.object(sources.Feed, 'json', return_value=payload) as request:
            jobs = sources.fetch('pwc')
        self.assertEqual(request.call_args.args[0], '/wday/cxs/pwc/Global_Experienced_Careers/jobs')
        self.assertEqual(jobs[0]['external_id'], '123WD')

    def test_pwc_malformed_page_retains_prior_observations_with_warning(self):
        pages = [dict(total=2, jobPostings=[dict(title='Engineer', externalPath='/job/India/Engineer_123WD')]),
                 dict(total=0, jobPostings=[dict(bulletFields=['124WD'])])]
        with patch.object(sources.Feed, 'json', side_effect=pages), \
                patch.object(sources.Feed, 'close') as close, \
                self.assertLogs('backend.career_sources', level='WARNING') as logs:
            jobs = sources.fetch('pwc')
        self.assertEqual([item['external_id'] for item in jobs], ['123WD'])
        self.assertIn('Incomplete observations retained: source=pwc rows=1 error=KeyError', ' '.join(logs.output))
        close.assert_called_once()

    def test_google_paginates_and_preserves_complete_nested_description(self):
        compact = google_card().replace('Ki3IFe', 'QJPWVe').replace('123-engineer"', '123-engineer?page=1"')
        pages = ['<span class="SWhIm">2</span> jobs matched' + compact,
                 '<span class="SWhIm">2</span> jobs matched' + google_card('124')]
        with patch.object(sources.Feed, 'request', side_effect=pages) as request:
            jobs = sources.fetch('google')
        self.assertEqual(len(jobs), 2)
        self.assertEqual(request.call_args.kwargs['params'], dict(page=2))
        candidate = self.candidate('google', '123', jobs[0]['url'])
        candidate['location'] = 'Bengaluru, India'
        with patch.object(sources.Feed, 'request', return_value=google_detail()):
            prepared = sources.prepare_job(candidate)
        self.assertIn('FINAL REQUIREMENT', json.loads(prepared['raw_json'])['descriptionHtml'])
        self.assertEqual(prepared['location'], 'Bengaluru, India')
        for html in (google_detail(full=False), google_detail('124'), google_detail(title='Other')):
            with self.subTest(html=html), patch.object(sources.Feed, 'request', return_value=html):
                with self.assertRaises(ValueError):
                    sources.prepare_job(candidate)
        for html in ('<html>Unavailable</html>', '<li class="lLd3Je"><p>Truncated'):
            with patch.object(sources.Feed, 'request', return_value=html), self.assertRaises(ValueError):
                sources.fetch('google')

    def test_apple_india_pagination_and_full_description_sections(self):
        item = dict(reqId='123-456', transformedPostingTitle='engineer', postingTitle='Engineer',
                    locations=[dict(city='Bengaluru', countryName='India')])
        pages = [apple_page('search', dict(totalRecords=2, searchResults=[item])),
                 apple_page('search', dict(totalRecords=2, searchResults=[dict(item, reqId='124-456')]))]
        with patch.object(sources.Feed, 'request', side_effect=pages) as request:
            jobs = sources.fetch('apple')
        self.assertEqual(request.call_args.kwargs['params'], dict(location='india-INDC', page=2))
        candidate = self.candidate('apple', '123-456', jobs[0]['url'])
        detail = dict(item, jobSummary='Summary', description='Full job', minimumQualifications='Python',
                      preferredQualifications='FINAL REQUIREMENT', jobType='RETAIL')
        with patch.object(sources.Feed, 'request', return_value=apple_page('jobDetails', dict(jobsData=detail))):
            prepared = sources.prepare_job(candidate)
        raw = json.loads(prepared['raw_json'])
        self.assertEqual(raw['descriptionHtml'], 'Summary\nFull job\nPython\nFINAL REQUIREMENT')
        self.assertNotIn('employmentType', raw)
        for changed in (dict(detail, reqId='different'), dict(detail, postingTitle='Changed'), dict(detail, description='')):
            with patch.object(sources.Feed, 'request', return_value=apple_page('jobDetails', dict(jobsData=changed))):
                with self.assertRaises(ValueError):
                    sources.prepare_job(candidate)
        for html in ('<html>No data</html>', 'window.__staticRouterHydrationData = JSON.parse(123)'):
            with self.assertRaises(ValueError):
                sources.apple_data(html, 'search')
        with patch.object(sources.Feed, 'request', return_value=apple_page('search', dict(totalRecords=1, searchResults=[dict(item, reqId='../123')]))):
            with self.assertRaises(ValueError):
                sources.fetch('apple')

    def test_apple_pipeline_uses_canonical_url_without_following_redirects(self):
        item = dict(reqId='PIPE-123', transformedPostingTitle='engineer', postingTitle='Engineer',
                    locations=[dict(countryName='India')])
        with patch.object(sources.Feed, 'request', return_value=apple_page('search', dict(totalRecords=1, searchResults=[item]))):
            candidate = sources.fetch('apple')[0]
        self.assertEqual(candidate['url'], 'https://jobs.apple.com/en-us/details/123/engineer')
        candidate['raw_json'] = json.dumps(candidate['raw_json'])
        detail = dict(item, description='Full job', minimumQualifications='Python')
        with patch.object(sources.Feed, 'request', return_value=apple_page('jobDetails', dict(jobsData=detail))) as request:
            sources.prepare_job(candidate)
        self.assertEqual(request.call_args.args[0], candidate['url'])

    def test_apple_rejects_ignored_country_filter_and_invalid_location_values(self):
        text = apple_page('search', dict(totalRecords=0, searchResults=[])).replace('postLocation-INDC', 'postLocation-USA')
        with patch.object(sources.Feed, 'request', return_value=text), self.assertRaises(ValueError):
            sources.fetch('apple')
        for places in ('India', [None], [dict(city=123)], [dict(countryName=['India'])]):
            with self.subTest(places=places), self.assertRaises(ValueError):
                sources.apple_location(dict(locations=places))

    def test_amazon_retains_full_qualifications_and_filters_india(self):
        item = dict(id_icims='123', job_path='/en/jobs/123/engineer', country_code='IND', title='Engineer',
                    description='Full JD', basic_qualifications='Python', preferred_qualifications='FINAL REQUIREMENT',
                    location='Bengaluru, India', job_schedule_type='full-time')
        with patch.object(sources.Feed, 'json', return_value=dict(hits=1, jobs=[item])) as request:
            jobs = sources.fetch('amazon')
        self.assertEqual(request.call_args.kwargs['params']['normalized_country_code[]'], 'IND')
        raw = jobs[0]['raw_json']
        self.assertIn('Python', raw['descriptionHtml'])
        self.assertIn('FINAL REQUIREMENT', raw['descriptionHtml'])
        for changed in (dict(item, country_code='USA'), dict(item, job_path='/en/jobs/456/other'),
                        dict(item, description=''), dict(item, basic_qualifications=None)):
            with patch.object(sources.Feed, 'json', return_value=dict(hits=1, jobs=[changed])):
                with self.assertRaises(ValueError):
                    sources.fetch('amazon')
        with patch.object(sources.Feed, 'json', return_value=dict(error='unavailable', hits=0, jobs=[])):
            with self.assertRaises(ValueError):
                sources.fetch('amazon')

    def test_netflix_pagination_and_invalid_locations(self):
        item = dict(id=123, name='Engineer', locations=['Mumbai,India'], t_update=100)
        pages = [dict(count=2, positions=[item]), dict(count=2, positions=[dict(item, id=124)])]
        with patch.object(sources.Feed, 'json', side_effect=pages) as request:
            jobs = sources.fetch('netflix')
        self.assertEqual(request.call_args.kwargs['params'], dict(domain='netflix.com', start=1, num=10))
        self.assertEqual(jobs[0]['url'], 'https://explore.jobs.netflix.net/careers/job/123')
        for changed in (dict(item, id='../123'), dict(item, locations='India')):
            with patch.object(sources.Feed, 'json', return_value=dict(count=1, positions=[changed])):
                with self.assertRaises(ValueError):
                    sources.fetch('netflix')

    def test_microsoft_search_pagination_and_full_description(self):
        item = dict(id=123, positionUrl='/careers/job/123', name='Engineer',
                    locations=['Bengaluru, India'], postedTs=1234, department='Engineering')
        def payload(items):
            return dict(status=200, error=dict(message='', body=''), data=dict(count=2, positions=items))
        pages = [payload([item]), payload([dict(item, id=124, positionUrl='/careers/job/124')])]
        with patch.object(sources.Feed, 'json', side_effect=pages) as request:
            jobs = sources.fetch('microsoft')
        self.assertEqual([row['external_id'] for row in jobs], ['123', '124'])
        self.assertEqual(request.call_args.args[0], '/api/pcsx/search')
        self.assertEqual(request.call_args.kwargs['params']['start'], 1)
        candidate = self.candidate('microsoft', '123', jobs[0]['url'])
        candidate['location'] = jobs[0]['location']
        detail = dict(title='Engineer', url=jobs[0]['url'], description='<p>Full requirements tail</p>',
                      employmentType='FULL_TIME', **{'@type': 'JobPosting'})
        def html(value):
            return '<script type="application/ld+json">' + json.dumps(value) + '</script>'
        with patch.object(sources.Feed, 'request', return_value=html(detail)):
            prepared = sources.prepare_job(candidate)
        self.assertIn('Full requirements tail', json.loads(prepared['raw_json'])['descriptionHtml'])
        self.assertEqual(prepared['location'], 'Bengaluru, India')
        self.assertEqual(json.loads(prepared['raw_json'])['employmentType'], 'FULL_TIME')
        for changed in (dict(detail, url='https://example.test/wrong'), dict(detail, title='Other'),
                        dict(detail, description='')):
            with self.subTest(detail=changed), patch.object(sources.Feed, 'request', return_value=html(changed)):
                with self.assertRaises(ValueError):
                    sources.prepare_job(candidate)
        for changed in (dict(item, id=True), dict(item, positionUrl='/careers/job/999'),
                        dict(item, locations=['India', None])):
            with self.subTest(item=changed), patch.object(sources.Feed, 'json', return_value=payload([changed])):
                with self.assertRaises(ValueError):
                    sources.fetch('microsoft')
        with patch.object(sources.Feed, 'json', return_value=dict(status=429, error='limited')):
            with self.assertRaises(ValueError):
                sources.fetch('microsoft')
        with patch.object(sources.Feed, 'json', return_value=dict(status=200, error=dict(message='', body=''),
                                                                  data=dict(count=0, positions=[]))):
            self.assertEqual(sources.fetch('microsoft'), [])

    def test_unavailable_sources_make_no_requests(self):
        for key in ('meta', 'icici', 'futuresfirst'):
            with patch.object(sources.requests.Session, 'request') as request:
                with self.assertRaises(ValueError):
                    sources.fetch(key)
                request.assert_not_called()


if __name__ == '__main__':
    unittest.main()
