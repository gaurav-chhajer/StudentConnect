"""Allowlisted public career feeds; complete snapshots or explicit failures.

Descriptions are retrieved separately for eligible scoring work, avoiding a
description request for every worldwide vacancy during the evening fetch.
"""

import hashlib
import json
import re
import time
from html import unescape
from html.parser import HTMLParser
from urllib.parse import quote, urljoin, urlsplit

import requests

MAX_BYTES = 8 * 1024 * 1024
MAX_LISTINGS = 10000
MAX_PAGES = 1000
MAX_DESCRIPTION = 250000
SOURCES = {
    'postman': ('Postman', 'workday', 'https://postman.wd108.myworkdayjobs.com'),
    'cvent': ('Cvent', 'jibe', 'https://careers.cvent.com'),
    'zs': ('ZS', 'jibe', 'https://jobs.zs.com'),
    'icici': ('ICICI Bank', 'blocked', 'https://careers.icici.bank.in/'),
    'wipro': ('Wipro', 'sap', 'https://careers.wipro.com'),
    'idfc': ('IDFC FIRST Bank', 'phenom', 'https://careers.idfcfirst.bank.in'),
    'futuresfirst': ('Futures First', 'blocked', 'https://futuresfirst.com/job-list/'),
    'sapient': ('Publicis Sapient', 'sitemap', 'https://careers.publicissapient.com'),
    'hotstar': ('JioHotstar (JioStar)', 'workday', 'https://jiostar.wd102.myworkdayjobs.com'),
    'dpworld': ('DP World', 'oracle', 'https://ehpv.fa.em2.oraclecloud.com'),
    'bofa': ('Bank of America', 'workday', 'https://ghr.wd1.myworkdayjobs.com'),
    'jpmorgan': ('JPMorgan Chase', 'oracle', 'https://jpmc.fa.oraclecloud.com'),
    'morganstanley': ('Morgan Stanley', 'morgan', 'https://www.morganstanley.com'),
    'barclays': ('Barclays', 'talentbrew', 'https://search.jobs.barclays'),
    'nomura': ('Nomura', 'saphtml', 'https://careers.nomura.com'),
    'pwc': ('PwC', 'workday', 'https://pwc.wd3.myworkdayjobs.com'),
    'google': ('Google', 'google', 'https://www.google.com'),
    'amazon': ('Amazon (India)', 'amazon', 'https://www.amazon.jobs'),
    'apple': ('Apple (India)', 'apple', 'https://jobs.apple.com'),
    'netflix': ('Netflix', 'eightfold', 'https://explore.jobs.netflix.net'),
    'microsoft': ('Microsoft', 'pcsx', 'https://apply.careers.microsoft.com'),
    'meta': ('Meta', 'blocked', 'https://www.metacareers.com'),
}
WORKDAY = {'hotstar': ('jiostar', 'JioStar'), 'bofa': ('ghr', 'lateral-us'),
           'postman': ('postman', 'careers'), 'pwc': ('pwc', 'Global_Experienced_Careers')}
ORACLE = {'dpworld': 'CX_1', 'jpmorgan': 'CX_1001'}
# Keep the adapters available for explicit read-only verification, but do not
# enable new subscriptions until live listing and full-description checks pass.
UNVERIFIED_SOURCES = frozenset(('microsoft',))


class Page(HTMLParser):
    def __init__(self, text, scope_id=None):
        super().__init__(convert_charrefs=True)
        self.links, self.structured, self.attributes = [], [], []
        self.anchor, self.script, self.parts = None, False, []
        self.scope_id, self.scope_depth = scope_id, 0
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        self.attributes.append(attrs)
        if self.scope_id and attrs.get('id') == self.scope_id:
            self.scope_depth = 1
        elif self.scope_depth and tag not in ('br', 'hr', 'img', 'input', 'meta', 'link', 'source', 'wbr'):
            self.scope_depth += 1
        if tag == 'a' and attrs.get('href') and (self.scope_id is None or self.scope_depth):
            self.anchor = [attrs, []]
        if tag == 'script' and attrs.get('type') == 'application/ld+json':
            self.script, self.parts = True, []

    def handle_data(self, data):
        if self.anchor is not None:
            self.anchor[1].append(data)
        if self.script:
            self.parts.append(data)

    def handle_endtag(self, tag):
        if tag == 'a' and self.anchor is not None:
            self.links.append((self.anchor[0], ''.join(self.anchor[1]).strip()))
            self.anchor = None
        if tag == 'script' and self.script:
            self.structured.append(json.loads(''.join(self.parts)))
            self.script = False
        if self.scope_depth and tag not in ('br', 'hr', 'img', 'input', 'meta', 'link', 'source', 'wbr'):
            self.scope_depth -= 1

    def handle_startendtag(self, tag, attrs):
        self.attributes.append(dict(attrs))


class Feed:
    def __init__(self, key, deadline):
        if key not in SOURCES or SOURCES[key][1] == 'blocked':
            raise ValueError('Career source is not verified for automated fetching')
        self.key, self.origin, self.deadline = key, SOURCES[key][2], deadline
        self.session = requests.Session()

    def request(self, path, *, data=None, params=None):
        url = urljoin(self.origin + '/', path)
        if urlsplit(url).scheme != 'https' or urlsplit(url).netloc != urlsplit(self.origin).netloc:
            raise ValueError('Career request must stay on its allowlisted host')
        remaining = self.deadline - time.monotonic()
        if remaining < 1:
            raise ValueError('Career feed time budget exhausted; snapshot withheld')
        with self.session.request('POST' if data is not None else 'GET', url,
                                  json=data, params=params, timeout=(min(10, remaining), min(15, remaining)),
                                  allow_redirects=False, stream=True) as response:
            response.raise_for_status()
            if response.status_code != 200:
                raise ValueError('Career feed redirected or returned an unexpected status')
            chunks, size = [], 0
            for chunk in response.iter_content(65536):
                size += len(chunk)
                if size > MAX_BYTES or time.monotonic() >= self.deadline:
                    raise ValueError('Career response exceeded its size or time budget')
                chunks.append(chunk)
            return b''.join(chunks).decode('utf-8')

    def json(self, path, **kwargs):
        value = json.loads(self.request(path, **kwargs))
        if not isinstance(value, dict):
            raise ValueError('Career feed must return an object')
        return value

    def close(self):
        self.session.close()


class Description(HTMLParser):
    """Capture the verified SAP jobdescription element, preserving full HTML."""
    def __init__(self, text):
        super().__init__(convert_charrefs=False)
        self.depth, self.parts, self.wipro_label = 0, [], False
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        if self.depth:
            self.parts.append(self.get_starttag_text())
            if tag not in ('br', 'hr', 'img', 'input', 'meta', 'link'):
                self.depth += 1
        elif ('jobdescription' in dict(attrs).get('class', '').split() or
              self.wipro_label and tag == 'span' and 'rtltextaligneligible' in dict(attrs).get('class', '').split()):
            self.depth = 1
            self.wipro_label = False

    def handle_endtag(self, tag):
        if self.depth:
            self.depth -= 1
            if self.depth:
                self.parts.append('</' + tag + '>')

    def handle_data(self, data):
        if self.depth:
            self.parts.append(data)
        elif data.strip().startswith('Job Description:'):
            self.wipro_label = True

    def handle_startendtag(self, tag, attrs):
        if self.depth:
            self.parts.append(self.get_starttag_text())

    def handle_entityref(self, name):
        if self.depth:
            self.parts.append('&' + name + ';')

    def handle_charref(self, name):
        if self.depth:
            self.parts.append('&#' + name + ';')


def total(value):
    if type(value) is not int or not 0 <= value <= MAX_LISTINGS:
        raise ValueError('Career listing count is invalid or exceeds the cycle limit')
    return value


def job(key, identifier, title, location, url, version='', description=None, department=None, employment=None):
    if not isinstance(title, str) or not title.strip() or len(title) > 1000:
        raise ValueError('Career title is invalid')
    if type(identifier) not in (str, int):
        raise ValueError('Career identifier is invalid')
    identifier = str(identifier)
    if not identifier or len(identifier) > 300:
        raise ValueError('Career identifier is invalid')
    if not isinstance(url, str) or len(url) > 4000 or urlsplit(url).scheme != 'https':
        raise ValueError('Career job URL is invalid')
    if location is not None and (not isinstance(location, str) or len(location) > 4000):
        raise ValueError('Career location is invalid')
    raw = dict(career_source=key, listing_version=hashlib.sha256(
        json.dumps([identifier, title, location, url, version, employment], sort_keys=True).encode()).hexdigest())
    if employment:
        if not isinstance(employment, str) or len(employment) > 200:
            raise ValueError('Career employment type is invalid')
        raw['employmentType'] = employment
    if description:
        if not isinstance(description, str) or len(description) > MAX_DESCRIPTION:
            raise ValueError('Career description exceeds its limit')
        raw['descriptionHtml'] = description
    return dict(external_id=identifier, title=title, location=location,
                department=department, url=url, raw_json=raw)


def structured_job(text):
    for value in Page(text).structured:
        values = value if isinstance(value, list) else [value]
        for item in values:
            if isinstance(item, dict) and item.get('@type') == 'JobPosting':
                return item
    raise ValueError('Full structured job description is unavailable')


class GoogleResultCards(HTMLParser):
    """Capture result cards or the selected description, preserving nested HTML."""
    def __init__(self, text, detail=False):
        super().__init__(convert_charrefs=False)
        self.target_tag, self.target_class = ('div', 'DkhPwc') if detail else ('li', 'lLd3Je')
        self.fragments, self.parts, self.depth = [], [], 0
        self.feed(text)
        if self.depth:
            raise ValueError('Google result card is incomplete')

    def handle_starttag(self, tag, attrs):
        if not self.depth and not (tag == self.target_tag and self.target_class in dict(attrs).get('class', '').split()):
            return
        self.parts.append(self.get_starttag_text())
        if tag not in ('area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'param', 'source', 'track', 'wbr'):
            self.depth += 1

    def handle_startendtag(self, tag, attrs):
        if self.depth:
            self.parts.append(self.get_starttag_text())

    def handle_endtag(self, tag):
        if self.depth:
            self.parts.append('</' + tag + '>')
            self.depth -= 1
            if not self.depth:
                self.fragments.append(''.join(self.parts))
                self.parts = []

    def handle_data(self, data):
        if self.depth:
            self.parts.append(data)

    def handle_entityref(self, name):
        if self.depth:
            self.parts.append('&' + name + ';')

    def handle_charref(self, name):
        if self.depth:
            self.parts.append('&#' + name + ';')


def google_cards(text):
    cards = []
    for html in GoogleResultCards(text).fragments:
        links = [urlsplit(attrs['href']).path for attrs, _ in Page(html).links
                 if re.fullmatch(r'jobs/results/\d+-[^/?#]+', urlsplit(attrs['href']).path)]
        title = re.search(r'<h3 class="(?:Ki3IFe|QJPWVe)">(.*?)</h3>', html, re.S)
        if not links or title is None:
            raise ValueError('Google result identity is unavailable')
        places = re.findall(r'<span class="r0wTof\s*">(.*?)</span>', html, re.S)
        location = '; '.join(dict.fromkeys(unescape(re.sub('<[^>]*>', '', place)) for place in places)) or None
        path = links[0]
        cards.append((path.split('/')[2].split('-')[0], unescape(re.sub('<[^>]*>', '', title[1])),
                      location, 'https://www.google.com/about/careers/applications/' + path, html))
    return cards


def apple_data(text, section):
    marker = 'window.__staticRouterHydrationData = JSON.parse('
    if marker not in text:
        raise ValueError('Apple career page data is unavailable')
    encoded = json.JSONDecoder().raw_decode(text.split(marker, 1)[1])[0]
    if not isinstance(encoded, str):
        raise ValueError('Apple career page data is invalid')
    payload = json.loads(encoded)
    if not isinstance(payload, dict) or payload.get('errors'):
        raise ValueError('Apple career page reported an error')
    value = payload['loaderData'][section]
    if not isinstance(value, dict):
        raise ValueError('Apple career page section is invalid')
    return value


def apple_location(item):
    places = item['locations']
    if (not isinstance(places, list) or any(not isinstance(place, dict) for place in places) or
            any(place.get(field) is not None and not isinstance(place[field], str)
                for place in places for field in ('city', 'stateProvince', 'countryName'))):
        raise ValueError('Apple career locations are invalid')
    return '; '.join(dict.fromkeys(', '.join(filter(None, (place.get('city'), place.get('stateProvince'),
                                                           place.get('countryName')))) for place in places)) or None


def fetch(key, deadline=None):
    import logging
    from backend.job_ingestion import deduplicate, mode_for_source
    mode = mode_for_source(key)
    feed = Feed(key, deadline if deadline is not None else time.monotonic() + 300)
    try:
        rows = []
        try:
            _listings(feed, rows)
        except (requests.RequestException, ValueError, KeyError, TypeError) as error:
            if mode != 'observed' or not rows:
                raise
            logging.getLogger(__name__).warning(
                'Incomplete observations retained: source=%s rows=%s error=%s', key, len(rows), type(error).__name__)
        if len(rows) > MAX_LISTINGS or mode == 'snapshot' and len({row['external_id'] for row in rows}) != len(rows):
            raise ValueError('Career snapshot is oversized or contains duplicate jobs')
        if mode == 'observed' and len({row['external_id'] for row in rows}) != len(rows):
            logging.getLogger(__name__).warning('Repeated paginated observations: source=%s rows=%s', key, len(rows))
        rows = deduplicate(rows, mode)
        # The same senior-title rule already governs eligibility in the pipeline.
        # Apply it before transfer to keep large enterprise snapshots bounded.
        from backend.pipeline_skeleton import SENIOR_TITLE_KEYWORDS
        rows = [row for row in rows if not any(word in row['title'].lower() for word in SENIOR_TITLE_KEYWORDS)]
        if mode == 'snapshot' and len(rows) > 5000:
            raise ValueError('Eligible career snapshot exceeds the hosted job limit')
        return rows
    finally:
        feed.close()


def _listings(feed, rows=None):
    key, kind, origin = feed.key, SOURCES[feed.key][1], feed.origin
    rows = [] if rows is None else rows
    expected = None
    if kind == 'morgan':
        # The verified public result-set feed serves students and graduates.
        for opportunity in ('sg',):
            payload = feed.json('/web/career_services/webapp/service/careerservice/resultset.json',
                                params=dict(opportunity=opportunity, lang='en'))
            items = payload['resultSet']
            if not isinstance(items, list) or len(items) != total(payload['totalResults']):
                raise ValueError('Morgan Stanley snapshot is incomplete')
            for item in items:
                rows.append(job(key, opportunity + '-' + str(item['jobNumber']), item['jobTitle'], item.get('location'),
                                item['url'], description=item.get('jobDescription'), department=item.get('businessArea'),
                                employment=item.get('employmentType')))
        return rows
    if kind == 'sitemap':
        page = Page(feed.request('/job-details-sitemap'))
        paths = sorted({attrs['href'].split('?')[0] for attrs, _ in page.links if '/job-details/' in attrs['href']})
        if not paths or len(paths) > MAX_LISTINGS:
            raise ValueError('Publicis Sapient listing sitemap is unavailable')
        for path in paths:
            slug = urlsplit(path).path.rsplit('/', 1)[-1]
            # The slug is an identifier, not a replacement for the full description.
            rows.append(job(key, slug, slug.replace('-', ' '), None, urljoin(origin, path)))
        return rows
    for page_number in range(MAX_PAGES):
        if kind == 'workday':
            tenant, site = WORKDAY[key]
            payload = feed.json(f'/wday/cxs/{tenant}/{site}/jobs',
                                data=dict(appliedFacets={}, limit=20, offset=len(rows), searchText=''))
            count, items = total(payload['total']), payload['jobPostings']
            # Workday computes total only at offset zero. Subsequent pages
            # legitimately return total=0 alongside their jobPostings.
            if rows and count == 0:
                count = expected
            batch = []
            for item in items:
                path = item['externalPath']
                if not isinstance(path, str) or not path.startswith('/job/') or '_' not in path:
                    raise ValueError('Workday job path is invalid')
                location = item.get('locationsText')
                if isinstance(location, str) and re.fullmatch(r'\d+ Locations', location, re.I):
                    location = None  # Resolve the actual locations from the detail response.
                batch.append(job(key, path.rsplit('_', 1)[1], item['title'], location,
                                 origin + '/en-US/' + site + path, employment=item.get('timeType')))
        elif kind == 'google':
            text = feed.request('/about/careers/applications/jobs/results/', params=dict(page=page_number + 1))
            match = re.search(r'<span class="SWhIm">([\d,]+)</span>\s*jobs matched', text)
            if match is None:
                raise ValueError('Google career result count is unavailable')
            count = total(int(match[1].replace(',', '')))
            items = google_cards(text)
            batch = [job(key, identifier, title, location, url)
                     for identifier, title, location, url, _ in items]
        elif kind == 'apple':
            payload = apple_data(feed.request('/en-us/search', params=dict(location='india-INDC', page=page_number + 1)), 'search')
            locations = payload['filters']['locations']
            if (not isinstance(locations, list) or len(locations) != 1 or
                    not isinstance(locations[0], dict) or locations[0].get('id') != 'postLocation-INDC'):
                raise ValueError('Apple career feed did not apply its India filter')
            count, items = total(payload['totalRecords']), payload['searchResults']
            batch = []
            for item in items:
                identifier, slug = item['reqId'], item['transformedPostingTitle']
                if (not isinstance(identifier, str) or not re.fullmatch(r'[A-Za-z0-9-]+', identifier) or
                        not isinstance(slug, str) or not re.fullmatch(r'[a-z0-9-]+', slug)):
                    raise ValueError('Apple career identifier is invalid')
                batch.append(job(key, identifier, item['postingTitle'], apple_location(item),
                                 origin + '/en-us/details/' + identifier.removeprefix('PIPE-') + '/' + slug,
                                 version=[item.get('postDateInGMT'), item.get('jobSummary')]))
        elif kind == 'pcsx':
            payload = feed.json('/api/pcsx/search', params=dict(
                domain='microsoft.com', query='', location='', start=len(rows)))
            if payload.get('status') != 200 or payload.get('error') != {'message': '', 'body': ''}:
                raise ValueError('Microsoft career search reported an error')
            data = payload['data']
            count, items = total(data['count']), data['positions']
            batch = []
            for item in items:
                identifier = item['id']
                if type(identifier) is not int or identifier <= 0:
                    raise ValueError('Microsoft career identifier is invalid')
                path = '/careers/job/' + str(identifier)
                if item['positionUrl'] != path:
                    raise ValueError('Microsoft career URL does not match its identifier')
                locations = item['locations']
                if not isinstance(locations, list) or any(not isinstance(place, str) for place in locations):
                    raise ValueError('Microsoft career locations are invalid')
                batch.append(job(key, identifier, item['name'], '; '.join(locations) or None,
                                 origin + path, version=item.get('postedTs'), department=item.get('department')))
        elif kind == 'eightfold':
            payload = feed.json('/api/apply/v2/jobs', params=dict(domain='netflix.com', start=len(rows), num=10))
            count, items = total(payload['count']), payload['positions']
            batch = []
            for item in items:
                identifier = item['id']
                if type(identifier) is not int or identifier <= 0:
                    raise ValueError('Netflix career identifier is invalid')
                locations = item['locations']
                if not isinstance(locations, list) or any(not isinstance(place, str) for place in locations):
                    raise ValueError('Netflix career locations are invalid')
                batch.append(job(key, identifier, item['name'], '; '.join(locations) or None,
                                 origin + '/careers/job/' + str(identifier), version=item.get('t_update'),
                                 department=item.get('department')))
        elif kind == 'amazon':
            payload = feed.json('/en/search.json', params=dict(offset=len(rows), result_limit=100, sort='recent',
                                                              **{'normalized_country_code[]': 'IND'}))
            if payload.get('error'):
                raise ValueError('Amazon career feed reported an error')
            count, items = total(payload['hits']), payload['jobs']
            batch = []
            for item in items:
                identifier = item['id_icims']
                path = item['job_path']
                # Amazon also publishes customer-service listings with SF-prefixed IDs.
                if (not isinstance(identifier, str) or not re.fullmatch(r'(?:SF)?[0-9]+', identifier) or
                        not isinstance(path, str) or not path.startswith('/en/jobs/' + identifier + '/') or
                        item.get('country_code') != 'IND'):
                    raise ValueError('Amazon career identity or location is invalid')
                description = item['description']
                qualifications = item['basic_qualifications']
                preferred = item.get('preferred_qualifications') or ''
                if any(not isinstance(value, str) for value in (description, qualifications, preferred)) or not description.strip() or not qualifications.strip():
                    raise ValueError('Amazon full description or qualifications are unavailable')
                description += '\n<h3>Basic qualifications</h3>\n' + qualifications + '\n<h3>Preferred qualifications</h3>\n' + preferred
                batch.append(job(key, identifier, item['title'], item.get('location'), origin + path,
                                 version=description, description=description, department=item.get('job_category'),
                                 employment=item.get('job_schedule_type')))
        elif kind == 'jibe':
            payload = feed.json('/api/jobs', params=dict(page=page_number + 1, limit=100))
            count, items = total(payload['totalCount']), payload['jobs']
            batch = []
            for record in items:
                item = record['data']
                identifier = item['slug']
                if not isinstance(identifier, str) or not identifier.isascii() or not identifier.isdigit():
                    raise ValueError('Career listing identifier is invalid')
                if item.get('language') != 'en-us':
                    raise ValueError('Career listing language is unsupported')
                batch.append(job(key, identifier, item['title'], item.get('full_location'),
                                 origin + '/jobs/' + identifier + '?lang=en-us',
                                 version=[item.get('update_date'), item.get('description')],
                                 department=item.get('department') or None,
                                 employment=item.get('employment_type')))
        elif kind == 'oracle':
            site = ORACLE[key]
            payload = feed.json('/hcmRestApi/resources/latest/recruitingCEJobRequisitions', params={
                'onlyData': 'true', 'expand': 'requisitionList',
                'finder': f'findReqs;siteNumber={site},limit=100,offset={len(rows)}'})['items'][0]
            count, items = total(payload['TotalJobsCount']), payload['requisitionList']
            batch = [job(key, item['Id'], item['Title'], item.get('PrimaryLocation'),
                         f'{origin}/hcmUI/CandidateExperience/en/sites/{site}/job/{item["Id"]}',
                         version=item.get('PostedDate'), employment=item.get('JobSchedule')) for item in items]
        elif kind == 'sap':
            payload = feed.json('/services/recruiting/v1/jobs', data=dict(
                keywords='', locale='en_US', location='', pageNumber=page_number, sortBy='date'))
            count, items = total(payload['totalJobs']), payload['jobSearchResult']
            batch = []
            for record in items:
                item = record['response']
                locations = item.get('jobLocationShort') or []
                location = '; '.join(re.sub('<[^>]*>', '', unescape(value)) for value in locations)
                title = item['unifiedStandardTitle']
                batch.append(job(key, item['id'], title, location,
                                 origin + '/job/' + quote(title, safe='') + '/' + str(item['id']) + '-en_US',
                                 version=[item.get('unifiedStandardStart'), item.get('unifiedStandardEnd')]))
        elif kind == 'phenom':
            text = feed.request('/in/en/search-results', params=dict(**{'from': len(rows)}, s=1))
            match = re.search(r'phApp.ddo\s*=\s*', text)
            if match is None:
                raise ValueError('IDFC search data is unavailable')
            payload = json.JSONDecoder().raw_decode(text[match.end():])[0]['eagerLoadRefineSearch']
            count, items = total(payload['totalHits']), payload['data']['jobs']
            batch = [job(key, item['jobId'], item['title'], item.get('cityStateCountry'),
                         origin + '/in/en/job/' + item['jobSeqNo'] + '/' + quote(item['title'].replace(' ', '-'), safe='-'),
                         version=item.get('postedDate'), department=item.get('category'), employment=item.get('type')) for item in items]
        elif kind in ('saphtml', 'talentbrew'):
            path = ('/Nomura/go/Career-Opportunities-India/9050900/' + (str(len(rows)) + '/' if rows else '')
                    if kind == 'saphtml' else '/search-jobs')
            text = feed.request(path, params={'p': page_number + 1} if kind == 'talentbrew' else None)
            page = Page(text, scope_id='search-results-list' if kind == 'talentbrew' else None)
            if kind == 'talentbrew':
                counts = [a['data-total-job-results'] for a in page.attributes if 'data-total-job-results' in a]
                if not counts:
                    raise ValueError('Barclays pagination is unavailable')
                count = total(int(counts[0]))
                links = [(a['href'], title, a['data-job-id']) for a, title in page.links if 'data-job-id' in a]
            else:
                match = re.search(r'Results \d+ to \d+ of ([\d,]+)', text)
                if match is None:
                    raise ValueError('Nomura pagination is unavailable')
                count = total(int(match[1].replace(',', '')))
                links = []
                for row_text in re.findall(r'<tr\s+class="data-row"[^>]*>(.*?)</tr>', text, re.S):
                    row_links = [(a['href'], title) for a, title in Page(row_text).links
                                 if 'jobTitle-link' in a.get('class', '').split()]
                    if not row_links:
                        raise ValueError('Nomura listing row is malformed')
                    path, title = row_links[0]
                    match = re.search(r'<span\s+class="jobLocation"[^>]*>(.*?)</span>', row_text, re.S)
                    location = re.sub(r'<[^>]*>', '', unescape(match[1])).strip() if match else None
                    links.append((path, title, path.strip('/').rsplit('/', 1)[-1], location))
            items = links
            batch = [job(key, entry[2], entry[1], entry[3] if len(entry) == 4 else None,
                         urljoin(origin, entry[0])) for entry in links]
        else:
            raise ValueError('Unsupported career connector')
        if not isinstance(items, list) or expected is not None and expected != count:
            raise ValueError('Career listing count changed; snapshot withheld')
        expected = count
        if not batch and len(rows) != expected:
            raise ValueError('Career pagination ended before the advertised count')
        rows.extend(batch)
        if len(rows) == expected:
            return rows
        if len(rows) > expected:
            raise ValueError('Career pagination returned too many jobs')
    raise ValueError('Career pagination limit reached; snapshot withheld')


def prepare_job(job_row, deadline=None):
    """Retrieve a full description only for a candidate actually being scored."""
    result = dict(job_row)
    raw = json.loads(result['raw_json'] or '{}')
    if not isinstance(raw, dict):
        raise ValueError('Stored career data must be an object')
    key = raw.get('career_source')
    if key is not None and not isinstance(key, str):
        raise ValueError('Stored career source is invalid')
    if key is None or raw.get('descriptionHtml'):
        return result
    feed = Feed(key, deadline if deadline is not None else time.monotonic() + 30)
    try:
        kind = SOURCES[key][1]
        if kind == 'google':
            text = feed.request(result['url'])
            canonical = [attrs.get('href') for attrs in Page(text).attributes if attrs.get('rel') == 'canonical']
            page_title = re.search(r'<title>(.*?)</title>', text, re.S)
            descriptions = GoogleResultCards(text, detail=True).fragments
            if (canonical != [result['url']] or page_title is None or
                    not urlsplit(result['url']).path.rsplit('/', 1)[-1].startswith(result['external_id'] + '-') or
                    len(descriptions) != 1):
                raise ValueError('Google career detail identity changed')
            title = unescape(page_title[1]).removesuffix(' — Google Careers')
            location, description = result.get('location'), descriptions[0]
            if not all(section in description for section in ('Minimum qualifications:', 'About the job', 'Responsibilities')):
                raise ValueError('Google full description is unavailable')
            employment = raw.get('employmentType')
        elif kind == 'apple':
            prefix = feed.origin + '/en-us/details/' + result['external_id'].removeprefix('PIPE-') + '/'
            if not result['url'].startswith(prefix):
                raise ValueError('Apple career detail identity is invalid')
            detail = apple_data(feed.request(result['url']), 'jobDetails')['jobsData']
            if detail['reqId'] != result['external_id']:
                raise ValueError('Apple career detail identity changed')
            if not isinstance(detail.get('description'), str) or not detail['description'].strip():
                raise ValueError('Apple full description is unavailable')
            parts = [detail.get(field) or '' for field in ('jobSummary', 'description', 'minimumQualifications', 'preferredQualifications')]
            if any(not isinstance(part, str) for part in parts):
                raise ValueError('Apple description sections are invalid')
            description = '\n'.join(parts)
            title, location = detail['postingTitle'], apple_location(detail)
            # Apple's jobType identifies retail/corporate roles, not employment type.
            employment = raw.get('employmentType')
        elif kind == 'pcsx':
            expected_url = feed.origin + '/careers/job/' + result['external_id']
            if result['url'] != expected_url or not result['external_id'].isascii() or not result['external_id'].isdigit():
                raise ValueError('Microsoft career detail identity is invalid')
            detail = structured_job(feed.request(expected_url))
            if detail.get('url') != expected_url:
                raise ValueError('Microsoft career detail identity changed')
            description, title = detail['description'], detail['title']
            location = result.get('location')
            employment = detail.get('employmentType')
        elif kind == 'workday':
            tenant, site = WORKDAY[key]
            path = urlsplit(result['url']).path
            prefix = f'/en-US/{site}'
            if not path.startswith(prefix + '/job/'):
                raise ValueError('Workday detail path is invalid')
            detail = feed.json(f'/wday/cxs/{tenant}/{site}' + path[len(prefix):])['jobPostingInfo']
            description = detail['jobDescription']
            title, location = detail['title'], detail.get('location')
            additional = detail.get('additionalLocations') or []
            if not isinstance(additional, list) or any(not isinstance(value, str) for value in additional):
                raise ValueError('Workday additional locations are invalid')
            location = '; '.join(value for value in [location, *additional] if value) or None
            employment = detail.get('timeType')
        elif kind == 'jibe':
            identifier = result['external_id']
            if not isinstance(identifier, str) or not identifier.isascii() or not identifier.isdigit():
                raise ValueError('Career detail identifier is invalid')
            # Feed.json enforces the allowlisted host. Check the saved URL too
            # before trusting its identity for description updates.
            expected_url = feed.origin + '/jobs/' + identifier + '?lang=en-us'
            if result['url'] != expected_url:
                raise ValueError('Career detail URL is invalid')
            detail = feed.json('/api/jobs/' + identifier + '/en-us')
            if detail.get('slug') != identifier or detail.get('language') != 'en-us':
                raise ValueError('Career detail identity changed')
            description = detail['description']
            title, location = detail['title'], detail.get('full_location')
            employment = detail.get('employment_type')
        elif kind == 'oracle':
            payload = feed.json('/hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails', params={
                'onlyData': 'true', 'finder': f'ById;Id={result["external_id"]},siteNumber={ORACLE[key]}'})
            details = payload['items']
            if len(details) != 1 or str(details[0]['Id']) != result['external_id']:
                raise ValueError('Oracle job detail identity changed')
            detail = details[0]
            if not detail.get('ExternalDescriptionStr'):
                raise ValueError('Oracle full description is unavailable')
            description = '\n'.join(detail.get(k) or '' for k in
                                    ('ExternalDescriptionStr', 'ExternalResponsibilitiesStr', 'ExternalQualificationsStr',
                                     'CorporateDescriptionStr', 'OrganizationDescriptionStr'))
            title, location = detail['Title'], detail.get('PrimaryLocation')
            employment = detail.get('JobSchedule')
        elif kind in ('saphtml', 'sap'):
            description = ''.join(Description(feed.request(result['url'])).parts)
            title, location = result['title'], result.get('location')
            employment = raw.get('employmentType')
        else:
            detail = structured_job(feed.request(result['url']))
            description = detail['description']
            title = detail['title']
            employment = detail.get('employmentType')
            if isinstance(employment, list) and all(isinstance(value, str) for value in employment):
                employment = ', '.join(employment)
            places = detail.get('jobLocation') or []
            places = places if isinstance(places, list) else [places]
            location = '; '.join(', '.join(str(place['address'][k]) for k in
                                ('addressLocality', 'addressRegion', 'addressCountry') if place.get('address', {}).get(k))
                                for place in places) or None
        if not isinstance(description, str) or not description.strip() or len(description) > MAX_DESCRIPTION:
            raise ValueError('Full career description is missing or exceeds its limit')
        if SOURCES[key][1] != 'sitemap' and title.strip() != result['title'].strip():
            raise ValueError('Career job title changed since fetching')
        raw['descriptionHtml'] = description
        raw['resolved_title'], raw['resolved_location'] = title, location or result.get('location')
        if employment:
            if not isinstance(employment, str) or len(employment) > 200:
                raise ValueError('Career employment type is invalid')
            raw['employmentType'] = employment
        result.update(raw_json=json.dumps(raw), title=title, location=location or result.get('location'))
        return result
    finally:
        feed.close()
