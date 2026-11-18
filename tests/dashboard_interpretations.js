(async () => {
  const result = document.createElement('pre');
  result.id = 'navigation-test-result';
  document.body.append(result);
  let checks = 0;
  function assert(condition, message) {
    if (!condition) throw new Error(message);
    checks++;
  }
  function text(id) {
    const paragraph = document.getElementById(id + '-interpretation');
    assert(paragraph, 'Interpretation exists for ' + id);
    assert(!/NaN|undefined|Infinity/.test(paragraph.textContent), 'No invalid values in ' + id);
    assert(paragraph.previousElementSibling !== null, 'Interpretation follows plot content');
    return paragraph.textContent;
  }
  try {
    const scenario = window.BEANO_BATCH_DATA.test_case;
    if (scenario === 'standalone') {
      await new Promise(requestAnimationFrame);
      const value = text('standalone-chart');
      assert(value.includes('in CamR than in CamL'), 'Standalone direction is CamR');
      assert(value.includes('100.00% in CamR'), 'Standalone count is correct');
    } else {
      document.querySelector('[data-page=stereo]').click();
      await new Promise(requestAnimationFrame);
      const scatter = text('area-scatter'), area = text('ratio-chart'), light = text('delta-chart');
      const focus = text('focus-ratio-chart'), focusBands = text('focus-band-chart');
      const areaBands = text('area-band-chart'), levels = text('focus-level-chart');
      if (scenario === 'mixed') {
        assert(scatter.includes('in CamR than in CamL'), 'Scatter uses displayed per-camera areas');
        assert(area.includes('in CamL than in CamR') && area.includes('60.00% larger in projected area in CamL'), 'Area histogram uses paired area ratio, with correct counts');
        assert(light.includes('60.00% brighter in CamL'), 'Lightness subtraction sign is correct');
        assert(focus.includes('100.00% in CamR'), 'Focus ratios aggregate per bean');
        assert(focusBands.includes('CamR has higher measured focus proxy in bands 1, 10; CamL in bands 5'), 'Focus height direction is correct');
        assert(areaBands.includes('CamR has larger projected area in bands 5; CamL in bands 1; equal medians in bands 10'), 'Area height direction and equality are correct');
        assert(levels.includes('CamL (green) has the higher median focus proxy in bands 5; CamR (blue) in bands 1, 10'), 'Separate focus levels interpreted correctly');
        assert(area.includes('does not locate the waterfall'), 'No unsupported waterfall movement claim');
      } else if (scenario === 'equal') {
        for (const value of [scatter, area, light, focus]) assert(value.includes('All measured values are equal'), 'Equality does not pick a camera');
        assert(areaBands.includes('equal medians in bands 1'), 'Equal band reported');
        assert(areaBands.includes('Only one band is plotted'), 'Single band is not a trend');
      } else if (scenario === 'missing') {
        for (const value of [scatter, area, light, focus]) assert(value.includes('No valid paired'), 'Missing measurements handled');
        for (const value of [focusBands, areaBands, levels]) assert(value.includes('Not enough'), 'Missing height data handled');
      } else if (scenario === 'sparse') {
        assert(area.includes('Fewer than 20 beans'), 'Small cohort warning');
        for (const value of [focusBands, areaBands, levels]) assert(value.includes('Not enough'), 'Sparse bands not interpreted');
      }
      document.querySelector('[data-page=size]').click();
      document.querySelector('[data-page=stereo]').click();
      await new Promise(requestAnimationFrame);
      assert(document.querySelectorAll('.chart-interpretation').length === 7, 'Redraws do not duplicate interpretations');
    }
    result.dataset.status = 'passed';
    result.textContent = JSON.stringify({scenario, checks});
  } catch (error) {
    result.dataset.status = 'failed';
    result.textContent = error.stack;
  }
})();
