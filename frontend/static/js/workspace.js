/* Progressive enhancement: server routes own account roles, forms and data. */
(() => {
  'use strict';
  const adminHashes = new Map(['overview','queue','email','quotas','users','invites','companies','jobs','quality','audit','system'].map(page => [`#admin-${page}`, page]));
  function routeAdminHash() {
    if (location.hash === '#admin') location.replace('/admin');
    else if (adminHashes.has(location.hash)) location.replace(`/admin/area/${adminHashes.get(location.hash)}`);
  }
  routeAdminHash();
  window.addEventListener('hashchange', routeAdminHash);
  document.addEventListener('click', event => {
    const opener = event.target.closest('[data-open-dialog]');
    if (opener) {
      const dialog = document.getElementById(opener.dataset.openDialog);
      if (dialog && typeof dialog.showModal === 'function') {
        event.preventDefault();
        dialog.showModal();
      }
    }
    const close = event.target.closest('[data-close-dialog]');
    if (close) close.closest('dialog')?.close();
  });
  const tabs = [...document.querySelectorAll('[data-settings-tab]')];
  const panes = [...document.querySelectorAll('[data-settings-pane]')];
  if (tabs.length) {
    const select = () => {
      const hash = location.hash.slice(1);
      const chosen = hash === 'companies' || hash === 'email' ? hash : 'profile';
      panes.forEach(pane => { pane.hidden = pane.dataset.settingsPane !== chosen; });
      tabs.forEach(tab => {
        if (tab.dataset.settingsTab === chosen) tab.setAttribute('aria-current', 'page');
        else tab.removeAttribute('aria-current');
      });
      if (hash === 'preferences') document.getElementById('preferences')?.scrollIntoView();
    };
    window.addEventListener('hashchange', select);
    select();
    // Hidden required controls must be reachable when the browser reports errors.
    document.getElementById('settings-form')?.addEventListener('invalid', event => {
      const pane = event.target.closest('[data-settings-pane]');
      if (pane?.hidden) {
        location.hash = pane.dataset.settingsPane;
        select();
      }
    }, true);
  }
})();
