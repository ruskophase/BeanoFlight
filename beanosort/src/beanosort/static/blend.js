(() => {
  const destination = document.querySelector('[data-blend-destination]');
  if (!destination) return;
  const form = destination.form;
  const inputs = [...form.querySelectorAll('[name="portion_ids"]')];
  const expected = form.querySelector('[name="destination_portion_id"]');
  const summary = form.querySelector('[data-blend-summary]');
  const kg = value => {
    const fraction = (value % 1000000n).toString().padStart(6, '0').replace(/0+$/, '');
    return (value / 1000000n).toString() + (fraction ? '.' + fraction : '');
  };
  function update() {
    const option = destination.selectedOptions[0];
    const existing = option?.dataset.portionId || '';
    expected.value = existing;
    destination.setCustomValidity('');
    if (!destination.value) {
      summary.textContent = 'Choose a destination to review its existing contents and the combined total.';
      return;
    }
    const selected = inputs.filter(input => input.checked);
    const portions = new Set(selected.map(input => input.value));
    let total = selected.reduce((sum, input) => sum + BigInt(input.dataset.massMg), 0n);
    let note = 'Empty destination.';
    if (existing) {
      const alreadySelected = portions.has(existing);
      if (!alreadySelected) total += BigInt(option.dataset.massMg);
      portions.add(existing);
      note = `${kg(BigInt(option.dataset.massMg))} kg already in the destination; ` +
        (alreadySelected ? 'counted once among the selected ingredients.' : 'included automatically as an ingredient.');
    }
    summary.textContent = `${note} Combined total: ${kg(total)} kg from ${portions.size} portions.`;
    if (portions.size < 2) {
      destination.setCustomValidity('Choose at least two portions in total, including any existing destination contents.');
    }
  }
  form.addEventListener('change', update);
  window.addEventListener('pageshow', update);
  update();
})();
