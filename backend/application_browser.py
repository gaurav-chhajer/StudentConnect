"""Deterministic Greenhouse/Lever forms, with explicit pauses for unknown inputs."""

import re
from contextlib import contextmanager
from urllib.parse import urlsplit, urlunsplit

ATS_HOSTS = frozenset(('boards.greenhouse.io', 'job-boards.greenhouse.io',
    'boards.eu.greenhouse.io', 'job-boards.eu.greenhouse.io',
    'jobs.lever.co', 'jobs.eu.lever.co', 'jobs.ashbyhq.com'))
RESOURCE_HOSTS = ATS_HOSTS | frozenset(('boards-api.greenhouse.io',
    'boards-api.eu.greenhouse.io', 'api.lever.co', 'api.eu.lever.co',
    'recruiting.cdn.greenhouse.io', 'cdn.greenhouse.io', 'assets.greenhouse.io'))


class NeedsReview(ValueError):
    """No submission occurred; the form needs user input or a supported adapter."""


def normalized(value):
    return re.sub(r'\s+', ' ', value.replace('*', '').strip()).casefold()


def application_url(value):
    if not isinstance(value, str) or len(value) > 2000 or any(ord(c) < 33 for c in value):
        raise NeedsReview('Unsupported application URL')
    parsed = urlsplit(value)
    if (parsed.scheme != 'https' or parsed.netloc not in ATS_HOSTS or not parsed.path.strip('/')
            or parsed.username or parsed.password):
        raise NeedsReview('Only direct Greenhouse, Lever and Ashby HTTPS postings are supported')
    path = parsed.path.rstrip('/')
    if 'greenhouse.io' in parsed.netloc:
        if not re.fullmatch(r'/[a-zA-Z0-9_-]+/jobs/[0-9]+',path):
            raise NeedsReview('A direct Greenhouse job ID URL is required')
    elif 'lever.co' in parsed.netloc:
        if not re.fullmatch(r'/[a-zA-Z0-9_-]+/[a-zA-Z0-9_-]+(?:/apply)?',path):
            raise NeedsReview('A direct Lever posting URL is required')
        path = path.removesuffix('/apply')
    else:
        if not re.fullmatch(r'/[a-zA-Z0-9_-]+/[a-zA-Z0-9_-]+(?:/application)?',path):
            raise NeedsReview('A direct Ashby posting URL is required')
        path=path.removesuffix('/application')
    # Tracking parameters must not defeat duplicate detection.
    return urlunsplit((parsed.scheme, parsed.netloc, path, '', ''))


@contextmanager
def browser_page():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as runtime:
        browser = runtime.chromium.launch(headless=False)
        try:
            context = browser.new_context(accept_downloads=False, service_workers='block')
            context.set_default_timeout(15000)
            context.set_default_navigation_timeout(30000)

            def restrict(route):
                url = urlsplit(route.request.url)
                if url.scheme == 'https' and url.netloc in RESOURCE_HOSTS:
                    route.continue_()
                else:
                    route.abort()

            context.route('**/*', restrict)
            try:
                yield context.new_page()
            finally:
                context.close()
        finally:
            browser.close()


# Fixed program-owned JavaScript reads controls; remote page text is never code.
CONTROLS = """form => Array.from(form.querySelectorAll('input,select,textarea')).slice(0,201).map((e,i) => ({
 index:i, tag:e.tagName.toLowerCase(), type:(e.type || '').toLowerCase(), name:e.name.slice(0,300),
 label:(Array.from(e.labels || []).map(l=>l.textContent).join(' ').trim() ||
       e.getAttribute('aria-label') || e.name || '').slice(0,301),
 visible:!!(e.getClientRects().length), disabled:e.disabled, required:e.required ||
       e.getAttribute('aria-required')==='true', value:e.value.slice(0,4001), checked:e.checked,
 files:e.type==='file' ? Array.from(e.files).slice(0,2).map(f=>({name:f.name,size:f.size})) : [],
 options:e.tagName==='SELECT' ? Array.from(e.options).slice(0,101).map(o=>({label:o.label.slice(0,300),value:o.value.slice(0,300)})) : []
}))"""


def form_plan(controls, answers):
    """Preflight every control before filling; never invent answers or consent."""
    if not isinstance(controls, list) or len(controls) > 200:
        raise NeedsReview('Application form is too large')
    plan = []
    resume_count = 0
    for field in controls:
        if len(field['label']) > 300 or len(field['options']) > 100:
            raise NeedsReview('Application question exceeds supported limits')
        if field['disabled'] or field['type'] in ('hidden', 'submit', 'button', 'reset'):
            continue
        if field['type'] == 'file':
            if re.search(r'resume|cv', field['label'] + ' ' + field['name'], re.I):
                resume_count += 1
                plan.append((field['index'], 'file', None))
            elif field['required']:
                raise NeedsReview('A required attachment needs user input')
            continue
        if not field['visible']:
            if field['required']:
                raise NeedsReview('A required hidden form control needs review')
            continue
        if field['type'] in ('password', 'radio'):
            raise NeedsReview('Login or radio questions require user input')
        answer = answers.get(normalized(field['label']))
        if answer is None:
            if field['required']:
                raise NeedsReview('A required question has no approved answer')
            continue
        if field['type'] == 'checkbox':
            if answer not in ('yes', 'no') or (field['required'] and answer != 'yes'):
                raise NeedsReview('A consent checkbox needs explicit yes/no approval')
            plan.append((field['index'], 'checkbox', answer == 'yes'))
        elif field['tag'] == 'select':
            options = [option for option in field['options'] if normalized(option['label']) == normalized(answer)]
            if len(options) != 1:
                raise NeedsReview('An approved answer does not match a form option')
            plan.append((field['index'], 'select', options[0]['value']))
        elif field['type'] in ('text', 'email', 'tel', 'url', 'number', 'date', '') or field['tag'] == 'textarea':
            plan.append((field['index'], 'fill', answer))
        else:
            raise NeedsReview('Unsupported application control')
    if resume_count != 1:
        raise NeedsReview('A single identifiable resume upload is required')
    return plan


def submit(page, url, content, answers, before_submit):
    """Commit 'submitting' before clicking; failures thereafter remain uncertain."""
    url = application_url(url)
    host=urlsplit(url).hostname
    destination = url + '/apply' if 'lever.co' in host else url+'/application' if 'ashbyhq.com' in host else url
    page.goto(destination, wait_until='domcontentloaded')
    if application_url(page.url) != url:
        raise NeedsReview('Application redirected to a different posting')
    # Challenges are never solved, dismissed, or bypassed by this worker.
    challenge = page.locator('iframe[src*="recaptcha"],iframe[src*="hcaptcha"],input[autocomplete="one-time-code"]')
    if challenge.count():
        raise NeedsReview('CAPTCHA or verification requires user input')
    selector = '#application_form' if 'greenhouse.io' in host else 'form' if 'ashbyhq.com' in host else 'form.application-form'
    form = page.locator(selector)
    if form.count() != 1 or not form.is_visible():
        raise NeedsReview('This application form is not supported yet')
    action = form.get_attribute('action')
    if action:
        from urllib.parse import urljoin
        target = urlsplit(urljoin(url, action))
        if target.scheme != 'https' or target.netloc not in RESOURCE_HOSTS:
            raise NeedsReview('Unexpected form submission destination')
    controls = form.evaluate(CONTROLS)
    plan = form_plan(controls, answers)
    inputs = form.locator('input,select,textarea')
    for index, operation, answer in plan:
        control = inputs.nth(index)
        if operation == 'file':
            control.set_input_files({'name': 'Resume.docx',
                'mimeType': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
                'buffer': content})
        elif operation == 'select':
            control.select_option(value=answer)
        elif operation == 'checkbox':
            control.set_checked(answer)
        else:
            control.fill(answer)
    # Upload handlers may introduce new questions: validate the final form again.
    final = form.evaluate(CONTROLS)
    shape = lambda fields: [(f['index'], f['label'], f['type'], f['required'], f['options']) for f in fields]
    if shape(final) != shape(controls):
        raise NeedsReview('Application form changed during preparation')
    for index, operation, answer in plan:
        field = final[index]
        if operation == 'file':
            if field['files'] != [{'name':'Resume.docx','size':len(content)}]:
                raise NeedsReview('Resume attachment changed during preparation')
        elif operation == 'checkbox':
            if field['checked'] != answer:
                raise NeedsReview('Approved checkbox answer changed')
        elif field['value'] != answer:
            raise NeedsReview('An approved application answer changed')
    if not form.evaluate('(form) => form.checkValidity()'):
        raise NeedsReview('Application form validation failed')
    button = form.get_by_role('button', name=re.compile(r'^(submit application|submit|apply|send application)$', re.I))
    if button.count() != 1 or not button.is_enabled():
        raise NeedsReview('No single enabled submission button')
    confirmation = page.get_by_text(re.compile(
        r'^(Thank you for applying!?|Your application has been submitted\.?|Application submitted!?|'
        r'Thanks for applying!?|We have received your application\.?)$', re.I))
    if confirmation.count():
        raise NeedsReview('Confirmation already present before submission')
    before_submit()
    button.click(no_wait_after=True)
    confirmation.first.wait_for(state='visible', timeout=30000)
    if urlsplit(page.url).netloc not in ATS_HOSTS:
        raise RuntimeError('Confirmation redirected outside the supported ATS')
    # Return a fixed description rather than storing arbitrary external HTML.
    return 'Browser displayed an application submission confirmation'
