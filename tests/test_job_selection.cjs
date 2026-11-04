'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const code = fs.readFileSync(path.join(__dirname, '../frontend/static/js/job-selection.js'), 'utf8');

function harness() {
  const requests = [], timers = new Map(), history = [];
  let timerId = 0;
  function element(extra = {}) {
    return Object.assign({
      attributes: {}, listeners: {},
      setAttribute(name, value) { this.attributes[name] = value; },
      removeAttribute(name) { delete this.attributes[name]; },
      hasAttribute(name) { return name in this.attributes; },
      addEventListener(name, fn) { this.listeners[name] = fn; },
      removeEventListener(name) { delete this.listeners[name]; }
    }, extra);
  }
  const status = element({textContent: ''});
  function makePanel(jobId) {
    return element({dataset: {jobId}, scrollTop: 77, replaceWith(next) { currentPanel = next; }});
  }
  let currentPanel = makePanel('1');
  const links = ['1', '2', '3'].map(jobId => {
    const tile = {selected: jobId === '1', classList: {toggle(name, value) { tile.selected = value; }}};
    return element({href: `http://localhost/jobs?page=2&min_score=70&job=${jobId}#job-detail`,
      tile, closest() { return tile; }});
  });
  const list = element({querySelectorAll() { return links; }, contains(link) { return links.includes(link); }});
  const document = {
    getElementById(id) { return {'match-list': list, 'job-selection-status': status, 'job-detail': currentPanel}[id]; },
    importNode(node) { return node; }
  };
  const window = element({location: new URL('http://localhost/jobs?page=2&min_score=70'),
    history: {pushState(state, title, url) { history.push(url); window.location = new URL(url); }},
    setTimeout(fn) { const id = ++timerId; timers.set(id, fn); return id; },
    clearTimeout(id) { timers.delete(id); }});
  const fetch = (url, options) => new Promise((resolve, reject) => requests.push({url, options, resolve, reject}));
  class DOMParser {
    parseFromString(text) { return {getElementById() { return text === 'missing' ? null : makePanel(text); }}; }
  }
  vm.runInNewContext(code, {document, window, fetch, DOMParser, URL, AbortController});
  function click(index, extra = {}) {
    const event = Object.assign({button: 0, defaultPrevented: false,
      target: {closest() { return links[index]; }}, preventDefault() { this.defaultPrevented = true; }}, extra);
    list.listeners.click(event);
    return event;
  }
  async function respond(index, jobId, ok = true) {
    requests[index].resolve({ok, text: async () => jobId});
    await new Promise(resolve => setImmediate(resolve));
  }
  return {window, list, status, links, requests, timers, history, click, respond,
    panel: () => currentPanel, flush: () => new Promise(resolve => setImmediate(resolve))};
}

async function run() {
  const h = harness();
  const originalList = h.list;
  assert.equal(h.click(1).defaultPrevented, true);
  assert.equal(h.panel().attributes['aria-busy'], 'true');
  assert.equal(h.requests[0].options.redirect, 'error');
  await h.respond(0, '2');
  assert.equal(h.panel().dataset.jobId, '2');
  assert.equal(h.panel().scrollTop, 0);
  assert.equal(h.list, originalList);
  assert.equal(h.links[1].tile.selected, true);
  assert.equal(h.links[1].attributes['aria-current'], 'true');
  assert.equal(h.links[0].tile.selected, false);
  assert.equal(h.window.location.searchParams.get('min_score'), '70');
  assert.equal(h.window.location.searchParams.get('page'), '2');
  assert.equal(h.history.length, 1);
  assert.equal(h.timers.size, 0);
  for (const modifier of ['ctrlKey', 'metaKey', 'shiftKey', 'altKey']) {
    assert.equal(h.click(2, {[modifier]: true}).defaultPrevented, false);
  }
  assert.equal(h.click(2, {button: 1}).defaultPrevented, false);
  assert.equal(h.requests.length, 1);

  h.window.location = new URL('http://localhost/jobs?page=2&min_score=70');
  h.window.listeners.popstate();
  await h.respond(1, '1');
  assert.equal(h.panel().dataset.jobId, '1');
  assert.equal(h.history.length, 1); // Back navigation must not push another entry.

  for (const [body, ok] of [['missing', true], ['3', true], ['2', false]]) {
    const failed = harness();
    failed.click(1);
    await failed.respond(0, body, ok);
    assert.equal(failed.panel().dataset.jobId, '1');
    assert.equal(failed.history.length, 0);
    assert.match(failed.status.textContent, /Could not load/);
    assert.equal(failed.timers.size, 0);
  }
  const timeout = harness();
  timeout.click(1);
  [...timeout.timers.values()][0]();
  assert.equal(timeout.requests[0].options.signal.aborted, true);
  timeout.requests[0].reject(new Error('timeout'));
  await timeout.flush();
  assert.match(timeout.status.textContent, /Could not load/);
  assert.equal(timeout.panel().dataset.jobId, '1');

  const race = harness();
  race.click(1);
  race.click(2);
  assert.equal(race.requests[0].options.signal.aborted, true);
  await race.respond(1, '3');
  await race.respond(0, '2');
  assert.equal(race.panel().dataset.jobId, '3');
  assert.equal(race.history.length, 1);

  const cancel = harness();
  cancel.click(1);
  cancel.click(0);
  assert.equal(cancel.requests[0].options.signal.aborted, true);
  await cancel.respond(0, '2');
  assert.equal(cancel.panel().dataset.jobId, '1');
  assert.equal(cancel.history.length, 0);

  const cached = harness();
  cached.window.listeners.pagehide({persisted: true});
  cached.click(1);
  await cached.respond(0, '2');
  assert.equal(cached.panel().dataset.jobId, '2');
  cached.click(2);
  cached.window.listeners.pagehide({persisted: false});
  assert.equal(cached.requests[1].options.signal.aborted, true);
  await cached.respond(1, '3');
  assert.equal(cached.panel().dataset.jobId, '2');
  assert.equal(cached.list.listeners.click, undefined);
  assert.equal(cached.window.listeners.popstate, undefined);
  console.log('Job selection interaction checks passed.');
}

run().catch(error => { console.error(error); process.exitCode = 1; });
