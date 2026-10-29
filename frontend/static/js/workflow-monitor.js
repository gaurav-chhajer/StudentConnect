(() => {
  'use strict';
  const refreshButton = document.getElementById('wm-refresh');
  const toggle = document.getElementById('wm-auto');
  const status = document.getElementById('wm-refresh-status');
  if (!refreshButton || !toggle || !status) return;
  let enabled = !new URL(window.location.href).searchParams.has('before');
  let busy = false;
  let timer;
  let controller;
  const menu = document.querySelector('.wm-mobile-nav');
  const mobile = window.matchMedia('(max-width: 700px)');
  function fitMenu() { if (menu) menu.open = !mobile.matches; }
  fitMenu();
  mobile.addEventListener('change', fitMenu);
  function formatTimes() {
    document.querySelectorAll('#monitor-live time[datetime]').forEach(element => {
      const date = new Date(element.dateTime);
      if (!Number.isNaN(date.getTime())) {
        element.textContent = date.toLocaleString('en-IN', {day:'2-digit', month:'short', hour:'2-digit', minute:'2-digit', second:'2-digit'});
        element.title = date.toISOString();
      }
    });
  }
  function schedule() {
    window.clearTimeout(timer);
    toggle.setAttribute('aria-pressed', String(enabled));
    toggle.textContent = enabled ? 'Auto-refresh on' : 'Auto-refresh paused';
    if (enabled) timer = window.setTimeout(() => refresh(false), 10000);
  }
  async function refresh(manual) {
    if (busy) return;
    if (!manual && (document.hidden || document.querySelector('#monitor-live :focus'))) {
      schedule();
      return;
    }
    busy = true;
    controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), 8000);
    refreshButton.disabled = true;
    try {
      const response = await fetch(window.location.href, {cache:'no-store', signal:controller.signal, redirect:'error'});
      if (!response.ok) throw new Error('snapshot_unavailable');
      const page = new DOMParser().parseFromString(await response.text(), 'text/html');
      const next = page.getElementById('monitor-live');
      const current = document.getElementById('monitor-live');
      if (!next || !current) throw new Error('snapshot_unavailable');
      const opened = Array.from(current.querySelectorAll('details[open]')).map(element => element.id);
      current.replaceWith(document.importNode(next, true));
      opened.forEach(id => { const element = document.getElementById(id); if (element) element.open = true; });
      formatTimes();
      status.classList.remove('wm-refresh-error');
      status.textContent = enabled ? 'Updated. Checks again in 10 seconds.' : 'Updated. Auto-refresh is paused.';
    } catch (error) {
      status.classList.add('wm-refresh-error');
      status.textContent = 'Could not refresh. Saved data remains visible. Check your connection or sign in again, then refresh.';
    } finally {
      window.clearTimeout(timeout);
      controller = null;
      busy = false;
      refreshButton.disabled = false;
      schedule();
    }
  }
  refreshButton.addEventListener('click', () => refresh(true));
  toggle.addEventListener('click', () => {
    enabled = !enabled;
    status.textContent = enabled ? 'Checks for updates every 10 seconds.' : 'Paused. Refresh manually to update.';
    schedule();
  });
  window.addEventListener('pagehide', () => { window.clearTimeout(timer); if (controller) controller.abort(); mobile.removeEventListener('change', fitMenu); });
  formatTimes();
  schedule();
})();
