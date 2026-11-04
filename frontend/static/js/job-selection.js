(() => {
  'use strict';
  const list = document.getElementById('match-list');
  const status = document.getElementById('job-selection-status');
  let panel = document.getElementById('job-detail');
  if (!list || !status || !panel) return;
  let pending;
  let sequence = 0;

  function highlight(jobId) {
    list.querySelectorAll('.job-tile .jt-link').forEach(link => {
      const selected = new URL(link.href, window.location.href).searchParams.get('job') === jobId;
      link.closest('.job-tile').classList.toggle('is-selected', selected);
      if (selected) link.setAttribute('aria-current', 'true');
      else link.removeAttribute('aria-current');
    });
  }

  async function select(url, push) {
    const request = ++sequence;
    if (pending) pending.abort();
    const controller = new AbortController();
    pending = controller;
    const timeout = window.setTimeout(() => controller.abort(), 8000);
    panel.setAttribute('aria-busy', 'true');
    status.textContent = 'Loading selected matchâ€¦';
    try {
      // Reuse authenticated, escaped HTML; retain the surrounding page and its scroll position.
      const response = await fetch(url.href, {signal: controller.signal, cache: 'no-store', redirect: 'error'});
      if (!response.ok) throw new Error('match_unavailable');
      const page = new DOMParser().parseFromString(await response.text(), 'text/html');
      const next = page.getElementById('job-detail');
      const requested = url.searchParams.get('job');
      if (!next || !next.dataset.jobId || (requested && next.dataset.jobId !== requested)) {
        throw new Error('match_unavailable');
      }
      if (request !== sequence) return;
      const replacement = document.importNode(next, true);
      // Update history only after validating the response; failed requests retain the old URL.
      if (push) window.history.pushState(null, '', url.href);
      panel.replaceWith(replacement);
      panel = replacement;
      panel.scrollTop = 0;
      highlight(panel.dataset.jobId);
      status.textContent = 'Selected match updated.';
    } catch (error) {
      if (request !== sequence) return; // Superseded selections and page shutdown own cancellation.
      status.textContent = 'Could not load the selected match. Previous details remain visible. Try again or reload to sign in.';
    } finally {
      window.clearTimeout(timeout);
      if (request === sequence) {
        panel.removeAttribute('aria-busy');
        pending = null;
      }
    }
  }

  function click(event) {
    if (event.defaultPrevented || event.button !== 0 || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
    const link = event.target.closest('.jt-link');
    if (!link || !list.contains(link) || (link.target && link.target !== '_self') || link.hasAttribute('download')) return;
    const url = new URL(link.href, window.location.href);
    if (url.origin !== window.location.origin || url.pathname !== window.location.pathname || !url.searchParams.get('job')) return;
    event.preventDefault();
    // Abort a pending different selection even when returning to the currently displayed job.
    if (url.searchParams.get('job') === panel.dataset.jobId) {
      sequence += 1;
      if (pending) pending.abort();
      pending = null;
      panel.removeAttribute('aria-busy');
      status.textContent = 'Selected match updated.';
      return;
    }
    void select(url, true);
  }

  function restore() {
    void select(new URL(window.location.href), false);
  }
  function cleanup(event) {
    sequence += 1;
    if (pending) pending.abort();
    pending = null;
    panel.removeAttribute('aria-busy');
    if (event.persisted) return;
    list.removeEventListener('click', click);
    window.removeEventListener('popstate', restore);
    window.removeEventListener('pagehide', cleanup);
  }
  list.addEventListener('click', click);
  window.addEventListener('popstate', restore);
  window.addEventListener('pagehide', cleanup);
})();
