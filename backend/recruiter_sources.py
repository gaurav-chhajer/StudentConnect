"""Read-only browser discovery and source verification; never bypass site gates."""

import json
import logging
import re
from contextlib import contextmanager
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

from backend import pipeline_skeleton as pipeline

ROOTS=('linkedin.com','licdn.com','indeed.com','indeed.co.in','indeed.net','wellfound.com',
       'greenhouse.io','lever.co','ashbyhq.com')
SITES=('LinkedIn','Indeed','Wellfound','Greenhouse','Lever','Ashby')
MAX_DISCOVERED=1000
logger=logging.getLogger(__name__)


def failure_reason(error):
    reasons={'Posting or search page is unavailable':'http_page_unavailable',
        'Site requires login or verification; search was not completed':'login_or_verification_required',
        'Wellfound keyword search is unavailable without further user interaction':'wellfound_search_box_not_found',
        'No accessible posting links found; search coverage is unverified':'no_accessible_job_links',
        'A single verifiable JobPosting record is required':'missing_or_ambiguous_jobposting_data',
        'Posting contains invalid structured data':'invalid_jobposting_json',
        'Research URL is outside the supported public job sites':'unsupported_destination',
        'Posting lacks a title, employer, or usable description':'missing_posting_fields'}
    return reasons.get(str(error),'unclassified_'+type(error).__name__)


def ensure_browser_open(page,error=None):
    # Playwright can report a closed target before its cached status updates.
    target_closed=error is not None and type(error).__name__=='TargetClosedError'
    if target_closed or page.is_closed() or not page.context.browser.is_connected():
        raise ValueError('Research browser/session closed or crashed. Search stopped; no jobs were approved. Keep its window open; if it was already open, the browser failure needs investigation.') from error


def monitor_browser(browser,context,page):
    """Record lifecycle events without page content, URLs, or browser stderr."""
    state={'cleanup':False}
    def record(event):
        level=logging.INFO if state['cleanup'] else logging.WARNING
        logger.log(level,'Research browser event=%s expected_cleanup=%s',event,state['cleanup'])
    browser.on('disconnected',lambda *_:record('browser_disconnected'))
    context.on('close',lambda *_:record('context_closed'))
    page.on('close',lambda *_:record('page_closed'))
    page.on('crash',lambda *_:record('page_crashed'))
    return state


def public_url(value):
    if not isinstance(value,str) or len(value)>2000 or any(ord(c)<33 for c in value):
        raise ValueError('Invalid research URL')
    parsed=urlsplit(value)
    host=parsed.hostname or ''
    if (parsed.scheme!='https' or parsed.username or parsed.password or parsed.port not in (None,443)
            or not any(host==root or host.endswith('.'+root) for root in ROOTS)):
        raise ValueError('Research URL is outside the supported public job sites')
    query=''
    if host.endswith('indeed.com') or host.endswith('indeed.co.in'):
        job_id=parse_qs(parsed.query).get('jk')
        if job_id:
            query=urlencode({'jk':job_id[0]})
    path=parsed.path.rstrip('/')
    if host.endswith('lever.co'):
        path=path.removesuffix('/apply')
    elif host.endswith('ashbyhq.com'):
        path=path.removesuffix('/application')
    return urlunsplit(('https',parsed.netloc,path,query,''))


@contextmanager
def research_page():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as runtime:
        browser=runtime.chromium.launch(headless=False)
        try:
            context=browser.new_context(service_workers='block',accept_downloads=False)
            context.set_default_timeout(10000)
            context.set_default_navigation_timeout(30000)
            def restrict(route):
                try:
                    public_url(route.request.url)
                except ValueError:
                    route.abort()
                    return
                if route.request.method not in ('GET','HEAD'):
                    route.abort()
                else:
                    route.continue_()
            context.route('**/*',restrict)
            lifecycle=None
            try:
                page=context.new_page()
                lifecycle=monitor_browser(browser,context,page)
                logger.info('Research browser started engine=chromium version=%s',browser.version)
                yield page
            finally:
                if lifecycle is not None:
                    lifecycle['cleanup']=True
                context.close()
        finally:
            browser.close()


def checked_navigation(page,url):
    public_url(url)
    response=page.goto(url,wait_until='domcontentloaded')
    if response is None or response.status>=400:
        raise ValueError('Posting or search page is unavailable')
    public_url(page.url)
    if page.locator('iframe[src*="captcha"],input[type="password"],input[autocomplete="one-time-code"]').count():
        raise ValueError('Site requires login or verification; search was not completed')


def portal_links(page,site,title):
    if site=='LinkedIn':
        url='https://www.linkedin.com/jobs/search/?'+urlencode({'keywords':title,'location':'India'})
        pattern=r'https://(?:www\.)?linkedin\.com/jobs/view/'
    elif site=='Indeed':
        url='https://in.indeed.com/jobs?'+urlencode({'q':title,'l':'India'})
        pattern=r'https://[^/]*indeed\.(?:com|co\.in)/(?:viewjob|rc/clk|pagead/clk)'
    else:
        url='https://wellfound.com/jobs'
        pattern=r'https://wellfound\.com/(?:jobs/\d|company/[^/]+/jobs/)'
    checked_navigation(page,url)
    if site=='Wellfound':
        search=page.get_by_role('textbox',name=re.compile('search|keyword|job title',re.I))
        if search.count()!=1:
            raise ValueError('Wellfound keyword search is unavailable without further user interaction')
        search.fill(title)
        search.press('Enter')
        page.wait_for_load_state('domcontentloaded')
    links=page.locator('a[href]').evaluate_all('(elements)=>elements.slice(0,3000).map(e=>e.href.slice(0,2001))')
    found=[]
    for link in links:
        if re.match(pattern,link):
            canonical=public_url(link)
            if canonical not in found:
                found.append(canonical)
    if not found:
        raise ValueError('No accessible posting links found; search coverage is unverified')
    return found[:100]


def schema_jobs(value,depth=0):
    if depth>5:
        return []
    if isinstance(value,list):
        return [job for item in value[:100] for job in schema_jobs(item,depth+1)]
    if isinstance(value,dict):
        types=value.get('@type',[])
        if types=='JobPosting' or isinstance(types,list) and 'JobPosting' in types:
            return [value]
        return schema_jobs(value.get('@graph',[]),depth+1)
    return []


def salary_from_description(description):
    """Normalize only unambiguous, employer-written annual INR lakh amounts."""
    pattern=r'(?:INR|₹|Rs\.?)\s*([0-9]+(?:\.[0-9]+)?)(?:\s*[-–]\s*([0-9]+(?:\.[0-9]+)?))?\s*(?:LPA|lakhs?\s+per\s+annum)\b'
    matches=list(re.finditer(pattern,description,re.I))
    if len(matches)!=1:
        return None
    match=matches[0]
    context=description[max(0,match.start()-100):match.end()+100]
    if not re.search(r'salary|compensation|fixed pay|CTC',context,re.I) or re.search(r'estimat|variable|OTE|incentive',context,re.I):
        return None
    lower=float(match.group(1))*100000
    upper=float(match.group(2) or match.group(1))*100000
    if not 0<=lower<=upper<=1000000000:
        return None
    return {'currency':'INR','value':{'minValue':lower,'maxValue':upper,'unitText':'YEAR'},'source_quote':match.group(0)}


def read_posting(page,url,seed=None):
    checked_navigation(page,url)
    url=public_url(page.url)
    blocks=page.locator('script[type="application/ld+json"]').evaluate_all(
        'elements=>elements.slice(0,51).map(e=>(e.textContent||"").slice(0,1000001))')
    if len(blocks)>50 or sum(len(block) for block in blocks)>1000000:
        raise ValueError('Structured posting data exceeds safety limits')
    found=[]
    for block in blocks:
        try:
            parsed=json.loads(block)
        except json.JSONDecodeError as error:
            raise ValueError('Posting contains invalid structured data') from error
        found.extend(schema_jobs(parsed))
    if len(found)!=1:
        raise ValueError('A single verifiable JobPosting record is required')
    data=found[0]
    title=data.get('title')
    organization=data.get('hiringOrganization')
    company=organization.get('name') if isinstance(organization,dict) else None
    description=pipeline._clean_description(data.get('description'))
    if not isinstance(title,str) or not title.strip() or not isinstance(company,str) or not company.strip() or not description:
        raise ValueError('Posting lacks a title, employer, or usable description')
    if len(description)>250000:
        raise ValueError('Job description exceeds safety limit')
    if any(ord(character)<32 and character not in '\n\r\t' for value in (title,company,description) for character in value):
        raise ValueError('Posting text contains unsupported control characters')
    locations=data.get('jobLocation',[])
    if isinstance(locations,dict):
        locations=[locations]
    if not isinstance(locations,list) or len(locations)>50:
        raise ValueError('Invalid posting locations')
    names=[]
    for location in locations:
        if not isinstance(location,dict):
            raise ValueError('Invalid location entry')
        address=location.get('address',{})
        if isinstance(address,str):
            names.append(address)
        elif isinstance(address,dict):
            for key in ('addressLocality','addressRegion','addressCountry'):
                value=address.get(key)
                if isinstance(value,str):
                    names.append(value)
    location=', '.join(dict.fromkeys(names))[:1000]
    eligibility=data.get('applicantLocationRequirements')
    evidence=json.dumps({'jobLocation':locations,'jobLocationType':data.get('jobLocationType'),
                         'applicantLocationRequirements':eligibility,'experienceRequirements':data.get('experienceRequirements')},
                        ensure_ascii=False)
    # Structured eligibility is supplied verbatim as source evidence, never assumed.
    description=description+'\nPosting metadata: '+evidence
    apply_url=url
    candidate=data.get('url')
    if candidate:
        candidate=public_url(candidate)
        if urlsplit(candidate).netloc!=urlsplit(url).netloc:
            # A canonical employer link must itself be visited, not merely trusted.
            employer=read_posting(page,candidate,seed={'redirect_depth':1}) if not seed or not seed.get('redirect_depth') else None
            if employer:
                return employer
    contacts=page.locator('a[href*="linkedin.com/in/"]').evaluate_all('''elements=>elements.slice(0,30).map(e=>({
        name:(e.textContent||'').trim().slice(0,150),url:e.href.slice(0,2001),
        evidence:(e.parentElement.textContent||'').trim().slice(0,1500)}))''')
    contacts=[contact for contact in contacts if contact['name'] and re.search(r'recruiter|hiring manager|talent acquisition',contact['evidence'],re.I)]
    visible=page.locator('body').evaluate('(e)=>(e.innerText||"").slice(0,30000)')
    salary_lines=[line.strip()[:600] for line in visible.splitlines()
                  if re.search(r'salary|compensation|\bCTC\b|\bLPA\b|\bINR\b|₹|\$[0-9]',line,re.I)][:20]
    host=urlsplit(url).hostname
    site=next((name for name,root in (('LinkedIn','linkedin.com'),('Indeed','indeed.com'),
        ('Wellfound','wellfound.com'),('Greenhouse','greenhouse.io'),('Lever','lever.co'),('Ashby','ashbyhq.com'))
        if host==root or host.endswith('.'+root)),'Indeed' if host.endswith('indeed.co.in') else 'Unknown')
    return {'title':title.strip()[:200],'company_name':company.strip()[:200],
        'url':url,'apply_url':apply_url,'description':description,'location':location,'source':site,
        'posted_at':data.get('datePosted'),'date_evidence':'JobPosting.datePosted',
        'salary':data.get('baseSalary') or salary_from_description(description),
        'salary_as_listed':'\n'.join(salary_lines) or 'Salary not verified in accessible source text',
        'valid_through':data.get('validThrough'),
        'organization_url':organization.get('sameAs'),'contacts':contacts,
        'verified_at':datetime.now(timezone.utc).isoformat()}


def discover(conn,titles,page):
    """Bound discovery per source, report every inaccessible source explicitly."""
    links={}
    coverage={site:{'status':'not_searched','discovered':0,'verified':0,'issues':[]} for site in SITES}
    for site in ('LinkedIn','Indeed','Wellfound'):
        ensure_browser_open(page)
        completed=0
        for title_index,title in enumerate(titles[:10],1):
            logger.info('Searching source=%s title=%s/%s',site,title_index,len(titles[:10]))
            try:
                found=portal_links(page,site,title)
                for link in found:
                    if len(links)<MAX_DISCOVERED:
                        links.setdefault(link,site)
                coverage[site]['discovered']+=len(found)
                completed+=1
                logger.info('Source=%s title=%s found=%s posting links',site,title_index,len(found))
            except Exception as error:
                ensure_browser_open(page,error)
                coverage[site]['issues'].append('search:'+failure_reason(error))
                logger.warning('Source=%s search stopped (%s) reason=%s; continuing with other sources',site,type(error).__name__,failure_reason(error))
                break
        coverage[site]['status']='searched' if completed==len(titles[:10]) else 'partial' if completed else 'unavailable'
    boards=conn.execute("SELECT * FROM companies WHERE active=1 AND career_source IS NULL ORDER BY id LIMIT 1000").fetchall()
    for ats,site in (('greenhouse','Greenhouse'),('lever','Lever'),('ashby','Ashby')):
        searched=0
        for company in boards:
            if company['ats_type']!=ats:
                continue
            try:
                logger.info('Fetching source=%s company_id=%s',site,company['id'])
                jobs=pipeline.fetch_jobs_for_company(company)
                for job in jobs:
                    title=job.get('title','').casefold()
                    if any(target.casefold() in title or title in target.casefold() for target in titles[:10]):
                        if len(links)<MAX_DISCOVERED:
                            links.setdefault(public_url(job['url']),site)
                        coverage[site]['discovered']+=1
                searched+=1
            except Exception as error:
                coverage[site]['issues'].append('Configured board unavailable ('+type(error).__name__+')')
                logger.warning('Source=%s company_id=%s fetch failed (%s)',site,company['id'],type(error).__name__)
        coverage[site]['status']='configured_boards_only' if searched else 'unavailable'
        coverage[site]['boards_searched']=searched
        if not searched:
            coverage[site]['issues'].append('No accessible configured board; no claim of global ATS coverage')
    postings=[]
    verified=set()
    for index,(url,site) in enumerate(links.items(),1):
        ensure_browser_open(page)
        logger.info('Verifying posting=%s/%s source=%s',index,len(links),site)
        try:
            posting=read_posting(page,url)
            if posting['url'] not in verified:
                verified.add(posting['url'])
                postings.append(posting)
                coverage[site]['verified']+=1
        except Exception as error:
            ensure_browser_open(page,error)
            coverage[site]['issues'].append('posting:'+failure_reason(error))
            logger.warning('Posting=%s source=%s verification failed (%s) reason=%s',index,site,type(error).__name__,failure_reason(error))
    for value in coverage.values():
        value['issues']=list(dict.fromkeys(value['issues']))
        value['limits']='One accessible search result page per title; at most 1000 discovered links; configured ATS boards only'
    logger.info('Discovery finished: verified=%s discovered=%s',len(postings),len(links))
    return postings,coverage


def find_contact(page,company):
    """Look for a real company recruiter; never infer ownership of this vacancy."""
    query=urlencode({'keywords':company+' recruiter talent acquisition'})
    checked_navigation(page,'https://www.linkedin.com/search/results/people/?'+query)
    links=page.locator('a[href*="linkedin.com/in/"]').evaluate_all(
        'elements=>elements.slice(0,20).map(e=>({url:e.href.slice(0,2001),text:(e.parentElement.textContent||"").slice(0,2000)}))')
    for link in links:
        context=link['text']
        if company.casefold() not in context.casefold() or not re.search(r'recruiter|talent acquisition',context,re.I):
            continue
        url=public_url(link['url'])
        checked_navigation(page,url)
        headings=page.get_by_role('heading',level=1)
        if headings.count()!=1:
            continue
        name=headings.inner_text().strip()
        body=page.locator('body').evaluate('(e)=>(e.innerText||"").slice(0,100000)')
        if (not 1<=len(name)<=150 or company.casefold() not in body.casefold()
                or not re.search(r'recruiter|talent acquisition',body,re.I)):
            continue
        return {'name':name,'url':url,'source_url':url,
                'evidence':context,'relationship':'Company recruiter; ownership of this specific vacancy is unverified'}
    return None
