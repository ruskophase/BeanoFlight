// Browser regression against the real dashboard assets, with a three-bean fixture.
(() => {
  const result = document.createElement('pre');
  result.id = 'navigation-test-result';
  document.body.append(result);
  const checks = [];
  function assert(condition, message) {
    if (!condition) throw new Error(message);
    checks.push(message);
  }
  const el = id => document.getElementById(id);
  const title = () => el('modal-bean-title').textContent;
  const image = () => document.querySelector('.modal-photo-pair img').src;
  const key = value => document.dispatchEvent(new KeyboardEvent('keydown', {key: value}));
  function preferences(current, final) {
    assert(el('toggle-frame').textContent === 'Hide entire frame', 'Entire frame stays open');
    assert(el('full-CamL') && el('full-CamR'), 'Both full-frame panels present');
    assert(el('show-current-paths').checked === current, 'Current trajectory choice retained');
    assert(el('show-final-paths').checked === final, 'Final trajectory choice retained');
  }
  try {
    document.querySelector('[data-page=appearance]').click();
    el('select-dark').click();
    const tiles = document.querySelectorAll('#bean-gallery [data-bean-index]');
    assert(tiles.length === 3, 'Three selectable fixture beans');
    tiles[0].click();
    assert(!el('full-CamL'), 'Initial entire-frame preference is closed');
    el('toggle-frame').click();
    for (const [current, final] of [[false, false], [true, false], [false, true], [true, true]]) {
      for (const [id, value] of [['show-current-paths', current], ['show-final-paths', final]]) {
        if (el(id).checked !== value) el(id).click();
      }
      const firstTitle = title(), firstImage = image();
      const firstFrame = el('full-CamL').src;
      el('modal-next').click();
      assert(title() !== firstTitle && image() !== firstImage, 'Next loads a different bean and crop');
      assert(el('full-CamL').src !== firstFrame, 'Next loads a different full frame');
      preferences(current, final);
      el('modal-prev').click();
      assert(title() === firstTitle && image() === firstImage, 'Previous returns to original bean');
      preferences(current, final);
      key('ArrowRight'); preferences(current, final);
      key('ArrowLeft'); preferences(current, final);
      el('sample-next').click(); preferences(current, final);
      assert(image() !== firstImage, 'Sample navigation changes crop');
      el('sample-prev').click(); preferences(current, final);
      // Navigation also clamps correctly at either end without resetting options.
      key('ArrowLeft'); preferences(current, final);
      key('ArrowRight'); key('ArrowRight'); key('ArrowRight');
      assert(el('modal-next').disabled, 'Next disabled at last bean');
      preferences(current, final);
      key('ArrowLeft'); key('ArrowLeft');
      el('toggle-frame').click();
      el('modal-next').click();
      assert(!el('full-CamL'), 'Closed entire-frame preference stays closed');
      el('modal-prev').click();
      el('toggle-frame').click(); preferences(current, final);
      el('modal-close').click();
      tiles[1].click(); preferences(current, final);
      el('modal-prev').click();
    }
    result.dataset.status = 'passed';
    result.textContent = JSON.stringify({checks: checks.length, combinations: 4});
  } catch (error) {
    result.dataset.status = 'failed';
    result.textContent = error.stack;
  }
})();
