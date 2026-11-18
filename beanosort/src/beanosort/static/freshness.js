// Age the last server receipt locally. Never keep a stale value labelled fresh
// just because the operator has left the page open. No machine requests occur.
const freshnessLoadedAt = performance.now();
function updateFreshness() {
  const elapsed = (performance.now() - freshnessLoadedAt) / 1000;
  document.querySelectorAll('[data-received-age]').forEach((element) => {
    const initial = element.dataset.receivedAge;
    const fresh = initial !== '' && Number(initial) + elapsed < Number(element.dataset.staleSeconds);
    element.textContent = fresh ? 'Recent data received' : 'Stale / no connection confirmed';
  });
}
updateFreshness();
setInterval(updateFreshness, 1000);
document.addEventListener('visibilitychange', updateFreshness);
