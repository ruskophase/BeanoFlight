const sorterFrame = document.querySelector('[data-sorter-frame]');
window.addEventListener('message', event => {
  if (event.origin !== location.origin || event.source !== sorterFrame.contentWindow) return;
  if (event.data?.type === 'sorter-panel-height' && Number.isFinite(event.data.height)) {
    sorterFrame.style.height = Math.max(700, Math.min(16000, event.data.height + 4)) + 'px';
  }
});
