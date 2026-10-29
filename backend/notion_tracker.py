"""Optional connection to the existing Gaurav Job Applications schema."""

import os
from uuid import UUID

import requests

from backend.application_browser import NeedsReview, application_url
from backend.gemini_resume import bounded_json


class NotionTracker:
    def __init__(self, token, source):
        self.token = token
        self.source = str(UUID(source))

    @classmethod
    def configured(cls):
        token = os.environ.get('STUDENTCONNECT_NOTION_TOKEN', '').strip()
        source = os.environ.get('STUDENTCONNECT_NOTION_SOURCE', '').strip()
        if bool(token) != bool(source):
            raise ValueError('Configure both Notion token and data source, or neither')
        return cls(token, source) if token else None

    def request(self, method, path, body):
        with requests.request(method, 'https://api.notion.com/v1/' + path,
                headers={'Authorization': 'Bearer ' + self.token, 'Notion-Version': '2025-09-03'},
                json=body, timeout=(10, 30), stream=True, allow_redirects=False) as response:
            result = bounded_json(response)
        return result

    def find(self, url):
        """URL query is read-only; fail closed if results are ambiguous or truncated."""
        url = application_url(url)
        result = self.request('POST', 'data_sources/' + self.source + '/query', {
            'filter': {'property': 'Job link', 'url': {'contains': url.split('://', 1)[1]}},
            'page_size': 100})
        rows = result.get('results')
        if not isinstance(rows, list) or result.get('has_more') is not False:
            raise NeedsReview('Notion duplicate check was incomplete')
        matches = []
        for row in rows:
            link = row.get('properties', {}).get('Job link', {}).get('url')
            if not link:
                raise NeedsReview('Notion returned an invalid job link')
            if application_url(link) == url:
                matches.append(row)
        if len(matches) > 1:
            raise NeedsReview('Duplicate Notion tracker records need review')
        return matches[0] if matches else None

    def check(self, url):
        page = self.find(url)
        if page:
            properties = page.get('properties', {})
            status = (properties.get('Contact status', {}).get('select') or {}).get('name')
            verdict = (properties.get('Verdict', {}).get('select') or {}).get('name')
            if status != 'Not applied' or verdict == 'Skip':
                raise NeedsReview('Existing Notion history blocks automatic resubmission')
        return page

    def record(self, run, job, existing):
        """No retries: caller records uncertainty before this mutation."""
        rich = lambda value: [{'type': 'text', 'text': {'content': value[:1900]}}]
        properties = {
            'Company': {'title': rich(job['company_name'])},
            'Role': {'rich_text': rich(job['title'])},
            'Job link': {'url': application_url(job['url'])},
            'Match score': {'number': run['final_score']},
            'Contact status': {'select': {'name': 'Applied'}},
            'Application date': {'date': {'start': run['updated_at']}},
            'Listing group': {'select': {'name': 'Application history'}}}
        if 'workflow_number' in job:
            properties['#']={'number':job['workflow_number']}
        if existing:
            page_id = str(UUID(existing['id']))
            result = self.request('PATCH', 'pages/' + page_id, {'properties': properties})
        else:
            result = self.request('POST', 'pages', {
                'parent': {'type': 'data_source_id', 'data_source_id': self.source},
                'properties': properties})
        return str(UUID(result['id']))

    def followups(self,start,end):
        """Read actual saved due dates and statuses, with bounded cursor pagination."""
        results=[]
        cursor=None
        seen=set()
        for _ in range(100):
            body={'page_size':100,'filter':{'and':[
                {'property':'Follow-up date','date':{'on_or_after':start.isoformat()}},
                {'property':'Follow-up date','date':{'on_or_before':end.isoformat()}}]}}
            if cursor:
                body['start_cursor']=cursor
            value=self.request('POST','data_sources/'+self.source+'/query',body)
            rows=value.get('results')
            if not isinstance(rows,list) or len(rows)>100:
                raise ValueError('Invalid Notion follow-up response')
            for row in rows:
                properties=row.get('properties',{})
                status=(properties.get('Contact status',{}).get('select') or {}).get('name')
                if status not in ('Applied','Initial message sent','Follow-up sent'):
                    continue
                def rich(name,kind='rich_text'):
                    entries=properties.get(name,{}).get(kind,[])
                    if not isinstance(entries,list) or len(entries)>100:
                        raise ValueError('Invalid Notion history text')
                    return ''.join(item.get('plain_text',item.get('text',{}).get('content','')) for item in entries)
                applied=(properties.get('Application date',{}).get('date') or {}).get('start')
                due=(properties.get('Follow-up date',{}).get('date') or {}).get('start')
                if not applied or not due:
                    continue
                results.append({'number':properties.get('#',{}).get('number'),'company':rich('Company','title'),
                    'role':rich('Role'),'application_date':applied,'followup_date':due,
                    'contact':rich('Contact') or None,'contact_link':properties.get('Contact link',{}).get('url'),
                    'history':'Saved Notion status: '+status,'source':row.get('url')})
            if value.get('has_more') is False:
                return results
            cursor=value.get('next_cursor')
            if not isinstance(cursor,str) or not cursor or cursor in seen:
                raise ValueError('Notion follow-up pagination did not advance')
            seen.add(cursor)
        raise ValueError('Notion follow-up scan exceeds supported size; narrow the due-date view')
