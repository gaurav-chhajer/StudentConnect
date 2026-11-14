"""Offline Chromium checks; every HTTP request is fulfilled by Flask's test client."""

import os
import unittest
from pathlib import Path
from urllib.parse import urlsplit
from unittest.mock import patch

from playwright.sync_api import sync_playwright

from backend import admin
from tests import test_admin_integration


class AdminBrowserChecks(test_admin_integration.AdminIntegrationTests):
    # Only browser methods are run by load_tests below; fixture setup is reused.
    def coffee_palette(self):
        root=Path(__file__).resolve().parents[1]
        output=root/'tests/artifacts/coffee-palette'
        output.mkdir(parents=True,exist_ok=True)
        scan='''() => {
            const failures=[];
            const properties=['color','backgroundColor','backgroundImage','borderTopColor',
                'borderRightColor','borderBottomColor','borderLeftColor','outlineColor',
                'boxShadow','textShadow','fill','stroke','accentColor'];
            for (const el of document.querySelectorAll('body *')) {
                if (!el.getClientRects().length || getComputedStyle(el).visibility==='hidden') continue;
                for (const pseudo of [null,'::before','::after',
                    ...(el.matches('input[type=file]') ? ['::file-selector-button'] : [])]) {
                    const style=getComputedStyle(el,pseudo);
                    if (pseudo && pseudo!=='::file-selector-button' && ['none','normal'].includes(style.content)) continue;
                    for (const property of properties) {
                        for (const match of style[property].matchAll(/rgba?\\((\\d+), (\\d+), (\\d+)(?:, ([\\d.]+))?\\)/g)) {
                            if (match[4]==='0') continue;
                            const [r,g,b]=match.slice(1,4).map(Number);
                            const max=Math.max(r,g,b),min=Math.min(r,g,b),delta=max-min;
                            if (!delta || delta/max<=.035) continue;
                            let hue=max===r ? (g-b)/delta : max===g ? (b-r)/delta+2 : (r-g)/delta+4;
                            hue=(hue*60+360)%360;
                            if (hue>=175 && hue<=285) failures.push(`${el.tagName}.${el.className} ${pseudo||''} ${property}: ${match[0]}`);
                        }
                    }
                }
            }
            return [...new Set(failures)];
        }'''
        with patch.dict(os.environ,{'PLAYWRIGHT_BROWSERS_PATH':str(root/'data/browser-binaries')}), sync_playwright() as runtime:
            browser=runtime.chromium.launch(headless=True)
            try:
                context=browser.new_context(service_workers='block',reduced_motion='reduce')
                try:
                    def serve(route):
                        url=urlsplit(route.request.url)
                        if url.hostname!='localhost':
                            route.abort()
                            return
                        with self.client.get(url.path+('?' + url.query if url.query else '')) as response:
                            headers={key:value for key,value in response.headers if key.lower() not in ('content-length','set-cookie')}
                            route.fulfill(status=response.status_code,headers=headers,body=response.data)
                    context.route('**/*',serve)
                    page=context.new_page()
                    paths=['/','/jobs','/resume','/settings','/settings#companies','/settings#email',
                           '/applications','/analytics','/feedback','/admin/workflow-monitor',
                           '/accounts/invitations','/admin/feedback','/missing-palette-page']
                    paths += ['/admin/area/'+key for key,_ in admin.PAGES]
                    for authenticated in (True,False):
                        if not authenticated:
                            with self.client.session_transaction() as session:
                                session.clear()
                        for width in (1440,390):
                            page.set_viewport_size({'width':width,'height':1000})
                            for path in paths if authenticated else ['/','/login','/register','/forgot-password','/reset-password','/verify-account']:
                                page.goto('about:blank')
                                response=page.goto('http://localhost'+path)
                                self.assertEqual(response.status,404 if path=='/missing-palette-page' else 200,path)
                                page.locator('#boot').wait_for(state='hidden')
                                for theme in ('light','dark'):
                                    page.evaluate('(theme) => document.documentElement.dataset.theme=theme',theme)
                                    page.evaluate('() => new Promise(requestAnimationFrame)')
                                    self.assertEqual(page.evaluate(scan),[],(path,width,theme,'default'))
                                    controls=page.locator('button:visible, a.btn:visible, input:not([type=hidden]):visible, select:visible, textarea:visible')
                                    for index in range(controls.count()):
                                        control=controls.nth(index)
                                        if not control.is_enabled(): continue
                                        control.evaluate('el => el.focus()')
                                        page.keyboard.press('Tab')
                                        page.keyboard.press('Shift+Tab')
                                        page.evaluate('() => new Promise(requestAnimationFrame)')
                                        self.assertEqual(page.evaluate(scan),[],(path,width,theme,'focus',index))
                                        control.hover(force=True)
                                        page.evaluate('() => new Promise(requestAnimationFrame)')
                                        self.assertEqual(page.evaluate(scan),[],(path,width,theme,'hover',index))
                                    page.evaluate('document.activeElement.blur()')
                                    page.mouse.move(1,1)
                                    page.evaluate('window.scrollTo(0,0)')
                                    page.screenshot(path=str(output/f'{"account" if authenticated else "public"}-{path.replace("/","_").replace("#","_")}-{width}-{theme}.png'),full_page=True)
                finally:
                    context.close()
            finally:
                browser.close()

    def header_controls(self):
        root=Path(__file__).resolve().parents[1]
        output=root/'tests/artifacts/header-controls'
        output.mkdir(parents=True,exist_ok=True)
        with patch.dict(os.environ,{'PLAYWRIGHT_BROWSERS_PATH':str(root/'data/browser-binaries')}), sync_playwright() as runtime:
            browser=runtime.chromium.launch(headless=True)
            try:
                context=browser.new_context(service_workers='block',reduced_motion='reduce')
                try:
                    def serve(route):
                        url=urlsplit(route.request.url)
                        if url.hostname!='localhost':
                            route.abort()
                            return
                        with self.client.get(url.path+('?' + url.query if url.query else '')) as response:
                            headers={key:value for key,value in response.headers if key.lower() not in ('content-length','set-cookie')}
                            route.fulfill(status=response.status_code,headers=headers,body=response.data)
                    context.route('**/*',serve)
                    page=context.new_page()
                    for width in (1440,900,390):
                        page.set_viewport_size({'width':width,'height':900})
                        page.goto('http://localhost/settings')
                        page.locator('#boot').wait_for(state='hidden')
                        self.assertEqual(page.locator('.jp-more').count(),0)
                        self.assertLessEqual(page.evaluate('document.documentElement.scrollWidth'),width)
                        for theme in ('light','dark'):
                            page.evaluate('(theme) => {document.documentElement.dataset.theme=theme}',theme)
                            for selector in ('.jp-signout','.jp-theme-toggle'):
                                control=page.locator(selector)
                                for state in ('default','hover','focus'):
                                    page.mouse.move(1,1)
                                    page.locator('#main').focus()
                                    if state=='hover':
                                        control.hover()
                                    elif state=='focus':
                                        control.focus()
                                        page.keyboard.press('Tab')
                                        page.keyboard.press('Shift+Tab')
                                        self.assertTrue(control.evaluate("el => el.matches(':focus-visible')"))
                                    page.evaluate('() => new Promise(requestAnimationFrame)')
                                    style=control.evaluate('''(el) => {
                                        const style=getComputedStyle(el),probe=document.createElement('span');
                                        el.parentElement.append(probe);
                                        const colors={};
                                        for (const token of ['card','soft','ink','line','accent']) {
                                            probe.style.color=`var(--jp-${token})`;
                                            colors[token]=getComputedStyle(probe).color;
                                        }
                                        probe.remove();
                                        return {image:style.backgroundImage,bg:style.backgroundColor,color:style.color,
                                            border:style.borderTopColor,outline:style.outlineColor,colors};
                                    }''')
                                    self.assertEqual(style['image'],'none',(width,theme,selector,state))
                                    self.assertEqual(style['bg'],style['colors']['card' if state=='default' else 'soft'])
                                    self.assertEqual(style['color'],style['colors']['ink'])
                                    self.assertEqual(style['border'],style['colors']['line' if state=='default' else 'accent'])
                                    if state=='focus':
                                        self.assertEqual(style['outline'],style['colors']['accent'])
                                    page.locator('.site-header').screenshot(path=str(output/f'{width}-{theme}-{selector[1:]}-{state}.png'))
                finally:
                    context.close()
            finally:
                browser.close()

    def browser_checks(self):
        root=Path(__file__).resolve().parents[1]
        output=root/'tests/artifacts/admin-integration'
        output.mkdir(parents=True,exist_ok=True)
        with patch.dict(os.environ,{'PLAYWRIGHT_BROWSERS_PATH':str(root/'data/browser-binaries')}), sync_playwright() as runtime:
            browser=runtime.chromium.launch(headless=True)
            try:
                context=browser.new_context(service_workers='block',reduced_motion='reduce')
                try:
                    errors=[]
                    context.on('page',lambda page: page.on('pageerror',lambda error:errors.append(str(error))))
                    def serve(route):
                        url=urlsplit(route.request.url)
                        if url.hostname!='localhost':
                            route.abort()
                            return
                        path=url.path+('?' + url.query if url.query else '')
                        with self.client.open(path,method=route.request.method,data=route.request.post_data,
                             content_type=route.request.headers.get('content-type')) as response:
                            headers={key:value for key,value in response.headers if key.lower() not in ('content-length','set-cookie')}
                            route.fulfill(status=response.status_code,headers=headers,body=response.data)
                    context.route('**/*',serve)
                    page=context.new_page()
                    for width in (1440,900,390):
                        page.set_viewport_size({'width':width,'height':1000})
                        for key,_ in admin.PAGES:
                            page.goto('http://localhost/admin/area/'+key)
                            page.locator('#boot').wait_for(state='hidden')
                            self.assertEqual(page.locator('.aside2 a').count(),11)
                            self.assertLessEqual(page.evaluate('document.documentElement.scrollWidth'),width)
                            page.screenshot(path=str(output/f'{key}-{width}.png'),full_page=True)
                    page.goto('http://localhost/admin/area/users')
                    page.get_by_role('button',name='View',exact=True).click()
                    self.assertTrue(page.locator('dialog.admin-drawer').is_visible())
                    page.get_by_role('button',name='Close',exact=True).first.click()
                    page.goto('http://localhost/settings')
                    page.get_by_role('link',name='Companies',exact=True).click()
                    page.locator('#companies').wait_for(state='visible')
                    self.assertTrue(page.locator('#companies').is_visible())
                    self.assertFalse(page.locator('#profile').is_visible())
                    page.get_by_role('link',name='Email',exact=True).click()
                    page.locator('#email').wait_for(state='visible')
                    self.assertTrue(page.locator('#email').is_visible())
                    page.get_by_role('link',name='Feedback',exact=True).click()
                    self.assertTrue(page.locator('#feedback-dialog').is_visible())
                    page.get_by_role('button',name='Close',exact=True).click()
                    page.goto('http://localhost/applications')
                    page.get_by_role('link',name='Analytics',exact=True).click()
                    self.assertTrue(page.get_by_role('heading',name='Response rate',exact=True).is_visible())
                    page.screenshot(path=str(output/'tracker-analytics-390.png'),full_page=True)
                    self.assertEqual(errors,[])
                finally:
                    context.close()
            finally:
                browser.close()


def load_tests(loader, tests, pattern):
    return unittest.TestSuite([AdminBrowserChecks('header_controls'), AdminBrowserChecks('browser_checks'), AdminBrowserChecks('coffee_palette')])
