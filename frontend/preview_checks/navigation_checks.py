"""Local browser regression checks for navigation animation and loader lifecycle.

Uses an isolated Flask test database and intercepts all browser requests locally.
Run: .venv/Scripts/python.exe -B frontend/preview_checks/navigation_checks.py
"""
from pathlib import Path
import sys
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tests.test_web_flows import WebFlowTests
from playwright.sync_api import sync_playwright


def main():
    fixture = WebFlowTests()
    fixture.setUp()
    try:
        fixture.seed_user()
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel='msedge', headless=True)
            try:
                context = browser.new_context()
                page = context.new_page()
                errors = []
                page.on('pageerror', lambda error: errors.append(str(error)))

                def serve(route):
                    url = urlsplit(route.request.url)
                    response = fixture.client.get(url.path + ('?' + url.query if url.query else ''))
                    try:
                        route.fulfill(status=response.status_code,
                                      headers={'content-type': response.content_type}, body=response.data)
                    finally:
                        response.close()

                page.route('**/*', serve)
                page.goto('http://navigation.test/')
                assert page.locator('#boot.is-starting').count() == 1, 'First visit intro missing'
                page.goto('http://navigation.test/jobs')
                assert page.locator('#boot').count() == 0, 'Intro repeated on navigation'
                assert page.locator('html').get_attribute('data-theme') == 'dark'
                assert page.evaluate("getComputedStyle(document.querySelector('.app')).getPropertyValue('--jp-bg').trim()") == '#321d17'
                assert page.evaluate("getComputedStyle(document.querySelector('.jobs-filters input')).backgroundColor") == 'rgb(72, 43, 35)'
                page.locator('#theme').click()
                page.wait_for_function("document.documentElement.dataset.theme === 'light'")
                page.wait_for_timeout(400)
                assert page.evaluate("localStorage.getItem('jp-theme')") == 'light'
                assert page.evaluate("getComputedStyle(document.querySelector('.app')).getPropertyValue('--jp-bg').trim()") == '#fff9f3'
                light_field = page.evaluate("getComputedStyle(document.querySelector('.jobs-filters input')).backgroundColor")
                assert light_field == 'rgb(255, 253, 249)', f'Unexpected light filter fill: {light_field}'
                page.goto('http://navigation.test/resume')
                assert page.locator('html').get_attribute('data-theme') == 'light', 'Theme did not persist across pages'
                assert page.evaluate("getComputedStyle(document.querySelector('.app')).getPropertyValue('--jp-bg').trim()") == '#fff9f3'
                page.locator('#theme').click()
                page.wait_for_function("document.documentElement.dataset.theme === 'dark'")
                assert page.evaluate("localStorage.getItem('jp-theme')") == 'dark'
                page.goto('http://navigation.test/jobs')
                assert page.locator('html').get_attribute('data-theme') == 'dark', 'Dark theme did not persist across pages'
                page.reload()
                assert page.locator('#boot').count() == 0, 'Intro repeated on reload'
                page.evaluate("""() => {
                    const loader = document.getElementById('page-loader');
                    loader.classList.add('is-active'); loader.setAttribute('aria-hidden', 'false');
                    window.dispatchEvent(new PageTransitionEvent('pageshow', {persisted:true}));
                }""")
                assert page.locator('#page-loader').get_attribute('aria-hidden') == 'true'
                # A later handler can own a link without triggering the whole-page loader.
                page.evaluate("""() => {
                    const link = document.querySelector('#primary-nav a');
                    document.addEventListener('click', event => event.preventDefault(), {once:true});
                    link.click();
                }""")
                page.wait_for_timeout(400)
                assert page.locator('#page-loader').get_attribute('aria-hidden') == 'true'
                for options in [{'ctrlKey': True}, {'metaKey': True}, {'shiftKey': True}, {'button': 1}]:
                    page.evaluate("""options => {
                        const anchor = document.querySelector('#primary-nav a');
                        anchor.addEventListener('click', e => e.preventDefault(), {once:true});
                        anchor.dispatchEvent(new MouseEvent('click', {bubbles:true,cancelable:true,...options}));
                    }""", options)
                    page.wait_for_timeout(400)
                    assert page.locator('#page-loader').get_attribute('aria-hidden') == 'true'
                context.close()
                reduced = browser.new_context(reduced_motion='reduce')
                reduced_page = reduced.new_page()
                reduced_page.route('**/*', serve)
                reduced_page.goto('http://navigation.test/jobs')
                assert reduced_page.locator('#boot').count() == 0, 'Reduced-motion intro blocked content'
                reduced.close()
                assert not errors, errors
                print('Passed: palette colors, theme toggle and persistence, intro, navigation, reload, history restoration, modified clicks, reduced motion; no browser errors.')
            finally:
                browser.close()
    finally:
        fixture.doCleanups()


if __name__ == '__main__':
    main()
