// Feedback for native page requests. Never change/retry the submitted payload.
(() => {
  const dialog = document.querySelector('[data-workflow-busy]');
  if (!dialog) return;
  const title = dialog.querySelector('[data-busy-title]');
  const main = document.querySelector('main');
  let active = false;
  let previousFocus;

  function show(message = 'Loading…') {
    if (active) return;
    active = true;
    previousFocus = document.activeElement;
    title.textContent = message;
    main?.setAttribute('aria-busy', 'true');
    dialog.showModal();
  }

  function reset() {
    active = false;
    if (dialog.open) dialog.close();
    main?.removeAttribute('aria-busy');
    if (previousFocus?.isConnected) previousFocus.focus();
    previousFocus = null;
  }

  function formMessage(form, url) {
    if (form.dataset.busyMessage) return form.dataset.busyMessage;
    if ((form.getAttribute('method') || 'get').toLowerCase() === 'get') return linkMessage(url);
    const path = url.pathname;
    if (/\/jobs\/[^/]+\/transition$/.test(path)) {
      const state = new FormData(form).get('target');
      if (state === 'awaiting_reconciliation') return 'Finalising the execution boundary…';
      if (state === 'finishing') return 'Recording the finish boundary…';
      if (state === 'paused') return 'Recording the workflow pause…';
      return 'Updating the execution boundary…';
    }
    if (/\/weigh\/accept$/.test(path)) return 'Recording final reconciliation and updating stock…';
    if (/\/weigh\/review$/.test(path)) return 'Calculating reconciliation and bean estimates…';
    if (/\/reconcile$/.test(path)) return 'Recording reconciliation and population estimates…';
    if (/\/release$/.test(path)) return 'Releasing reconciled outputs…';
    if (/\/start$/.test(path)) return 'Preparing the run and population records…';
    if (/\/simulate$/.test(path)) return 'Processing the next particle batch…';
    if (/\/replay$/.test(path)) return 'Checking replayed particle data…';
    if (/\/import$/.test(path)) return 'Importing and checking run evidence…';
    if (/\/pause-for-change$/.test(path)) return 'Pausing the workflow and loading container choices…';
    if (/\/change-containers$/.test(path)) return 'Recording the container boundary…';
    if (path === '/jobs' || /\/jobs\/[^/]+\/edit$/.test(path)) return 'Preparing the sorting job…';
    if (/\/execute$/.test(path)) return 'Recording material movements and population estimates…';
    return 'Saving changes…';
  }

  function linkMessage(url) {
    if (/\/jobs\/[^/]+\/weigh$/.test(url.pathname)) return 'Calculating container weights and bean estimates…';
    if (/\/populations$/.test(url.pathname)) return 'Loading population charts and bean photos…';
    if (/\/jobs\/[^/]+$/.test(url.pathname)) return 'Loading the sorting job and population evidence…';
    if (/\/recipes\/[^/]+$/.test(url.pathname)) return 'Loading recipe statistics and bean photos…';
    return 'Loading…';
  }

  // Native validation and local wizard handlers run first. A cancelled/invalid
  // submission must remain editable, with no waiting overlay.
  document.addEventListener('submit', event => {
    if (event.defaultPrevented) return;
    const form = event.target;
    if (!(form instanceof HTMLFormElement)) return;
    // Inputs named "method" or "target" shadow DOM properties on these forms.
    const method = (form.getAttribute('method') || 'get').toLowerCase();
    const target = form.getAttribute('target');
    if (method === 'dialog' || form.hasAttribute('data-busy-skip') || (target && target !== '_self')) return;
    const url = new URL(form.getAttribute('action') || window.location.href, window.location.href);
    if (url.origin !== window.location.origin) return;
    if (active) { event.preventDefault(); return; }
    show(formMessage(form, url));
  });

  document.addEventListener('click', event => {
    if (event.defaultPrevented || event.button !== 0 || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
    const link = event.target.closest?.('a[href]');
    if (!link || link.hasAttribute('download') || link.hasAttribute('data-busy-skip') ||
        (link.target && link.target !== '_self')) return;
    const url = new URL(link.href, window.location.href);
    if (url.origin !== window.location.origin || !['http:', 'https:'].includes(url.protocol)) return;
    // Contents anchors and in-page chart controls must continue without a veil.
    if (url.pathname === window.location.pathname && url.search === window.location.search && link.getAttribute('href').includes('#')) return;
    if (active) { event.preventDefault(); return; }
    show(link.dataset.busyMessage || linkMessage(url));
  });

  // Escape cannot cancel work already submitted to the server. History restore
  // clears the previous page's pending state; errors load a fresh ordinary page.
  dialog.addEventListener('cancel', event => event.preventDefault());
  window.addEventListener('pageshow', reset);
  window.BeanoSortBusy = {show, reset};
})();
