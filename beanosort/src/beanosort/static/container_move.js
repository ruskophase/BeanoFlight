// Selecting a vessel reloads its current location, revision and valid destinations.
// Only the explicit, confirmed POST records a physical move.
const moveContainerPicker = document.querySelector('[data-move-container-picker]');
if (moveContainerPicker) {
  moveContainerPicker.addEventListener('change', () => {
    const previousMove = document.querySelector('[data-move-container-record]');
    if (previousMove) {
      previousMove.querySelectorAll('input, select, button').forEach(control => { control.disabled = true; });
    }
    if (moveContainerPicker.value) moveContainerPicker.form.requestSubmit();
    else window.location.assign(moveContainerPicker.form.action);
  });
  // Back/forward restoration must not revive a stale container revision or choices.
  window.addEventListener('pageshow', (event) => {
    if (event.persisted) window.location.reload();
  });
}
