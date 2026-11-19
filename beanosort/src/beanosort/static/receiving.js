// Receiving is committed once, after every container has a net scale reading
// and confirmed placement. No machine controls or per-particle requests occur.
const receivingForm = document.querySelector('[data-receiving-form]');
if (receivingForm) {
  const steps = [...receivingForm.querySelectorAll('[data-receiving-step]')];
  const progress = [...document.querySelectorAll('.receiving-progress li')];
  let currentStep = 0;
  let nextLoad = 1;
  receivingForm.noValidate = true;
  receivingForm.querySelectorAll('[data-receiving-next], [data-receiving-back], [data-add-load]').forEach(node => node.hidden = false);

  function showStep(index) {
    currentStep = index;
    steps.forEach((step, i) => step.hidden = i !== index);
    progress.forEach((item, i) => {
      if (i === index) item.setAttribute('aria-current', 'step');
      else item.removeAttribute('aria-current');
    });
  }
  function validateStep(index) {
    for (const field of steps[index].querySelectorAll('input,select,textarea')) {
      if (!field.checkValidity()) {
        showStep(index);
        field.reportValidity();
        return false;
      }
    }
    return true;
  }
  function matching(area, key) {
    return receivingForm.querySelector(`${area} [data-load="${key}"]`);
  }
  function enteredCapacityLimit(value) {
    const parts = value.match(/^(\d*)(?:\.(\d{1,6}))?$/);
    if (!parts || (!parts[1] && !parts[2])) return null;
    const milligrams = BigInt(parts[1] || '0') * 1000000n + BigInt((parts[2] || '').padEnd(6, '0'));
    if (milligrams <= 0n) return null;
    const limit = milligrams * 6n / 5n;
    const fraction = (limit % 1000000n).toString().padStart(6, '0').replace(/0+$/, '');
    return (limit / 1000000n).toString() + (fraction ? '.' + fraction : '');
  }
  function updateRows() {
    const rows = [...receivingForm.querySelectorAll('[data-container-rows] [data-load]')];
    const selected = rows.map(row => row.querySelector('select').value);
    rows.forEach((row, index) => {
      const key = row.dataset.load;
      const select = row.querySelector('select');
      const option = select.selectedOptions[0];
      const weightRow = matching('[data-weight-rows]', key);
      const storageRow = matching('[data-storage-rows]', key);
      const tare = row.querySelector('[name="receiving_tare_mass"]');
      const capacity = row.querySelector('[name="receiving_capacity"]');
      if ((row.dataset.selectedContainer || '') !== select.value) {
        tare.value = ''; capacity.value = '';
        weightRow.querySelector('[name="receiving_net_mass"]').value = '';
        weightRow.querySelector('[name="tared"]').checked = false;
        storageRow.querySelector('[name="receiving_location_id"]').value = '';
        row.dataset.selectedContainer = select.value;
      }
      // Keep the row's own choice usable and remove choices taken elsewhere.
      // Recomputing each time also restores them after changing/removing a row.
      for (const candidate of select.options) {
        const taken = Boolean(candidate.value && candidate.value !== select.value && selected.includes(candidate.value));
        candidate.disabled = taken;
        candidate.hidden = taken;
      }
      const needsTare = Boolean(select.value && !option?.dataset.tare);
      const needsCapacity = Boolean(select.value && !option?.dataset.capacity);
      tare.required = needsTare; capacity.required = needsCapacity;
      row.querySelector('[data-tare-entry]').hidden = !needsTare;
      row.querySelector('[data-capacity-entry]').hidden = !needsCapacity;
      row.querySelector('[data-missing-weights-note]').hidden = !needsTare && !needsCapacity;
      capacity.setCustomValidity('');
      const limit = option?.dataset.receivingLimit || (needsCapacity ? enteredCapacityLimit(capacity.value) : null);
      if (needsCapacity && capacity.value && !limit) capacity.setCustomValidity('Enter a positive decimal capacity in kg, with at most six decimal places.');
      row.querySelector('[data-load-number]').textContent = index + 1;
      row.querySelector('[name="receiving_container_revision"]').value = option?.dataset.revision || '';
      row.querySelector('[data-container-weights]').textContent = select.value ?
        `Tare: ${option.dataset.tare ? option.dataset.tare + ' kg (registered)' : 'enter below'}. Nominal capacity: ${option.dataset.capacity ? option.dataset.capacity + ' kg (registered)' : 'enter below'}.` :
        'Choose a container to see its registered tare and capacity.';
      const title = select.value ? option.dataset.label : `Container ${index + 1}`;
      weightRow.querySelector('[data-container-title]').textContent = title;
      storageRow.querySelector('[data-container-title]').textContent = title;
      const mass = weightRow.querySelector('[name="receiving_net_mass"]');
      if (limit) mass.max = limit;
      else mass.removeAttribute('max');
      weightRow.querySelector('[data-receiving-limit]').textContent = limit ?
        `Nominal capacity: ${option.dataset.capacity || capacity.value} kg. Receiving limit: ${limit} kg (20% allowance).` :
        'Nominal capacity is not recorded for this container.';
      select.setCustomValidity(select.value && selected.filter(value => value === select.value).length > 1 ?
        'Choose a different empty container for each load.' : '');
      const remove = row.querySelector('[data-remove-load]');
      remove.hidden = rows.length === 1;
      remove.onclick = () => {
        row.remove(); weightRow.remove(); storageRow.remove(); updateRows();
      };
    });
    const choices = rows[0]?.querySelector('select').options.length - 1 || 0;
    receivingForm.querySelector('[data-add-load]').disabled = rows.length >= Math.min(20, choices);
  }
  receivingForm.addEventListener('change', event => {
    if (event.target.name === 'receiving_container_id') updateRows();
  });
  receivingForm.addEventListener('input', event => {
    if (['receiving_tare_mass', 'receiving_capacity'].includes(event.target.name)) updateRows();
  });
  receivingForm.addEventListener('focusout', event => {
    const input = event.target;
    if (input.name === 'receiving_net_mass' && input.value && input.checkValidity()) {
      input.value = Number(input.value).toFixed(3);
    }
  });
  receivingForm.querySelector('[data-add-load]').addEventListener('click', () => {
    const key = String(nextLoad++);
    for (const [template, area] of [['container','container'], ['weight','weight'], ['storage','storage']]) {
      const fragment = document.querySelector(`[data-${template}-template]`).content.cloneNode(true);
      fragment.querySelector('[data-load]').dataset.load = key;
      const identity = fragment.querySelector('[name="receiving_load_id"]');
      if (identity) identity.value = key;
      const tared = fragment.querySelector('[name="tared"]');
      if (tared) tared.value = key;
      receivingForm.querySelector(`[data-${area}-rows]`).append(fragment);
    }
    updateRows();
  });
  receivingForm.querySelectorAll('[data-receiving-next]').forEach(button => button.addEventListener('click', () => {
    if (validateStep(currentStep)) showStep(currentStep + 1);
  }));
  receivingForm.querySelectorAll('[data-receiving-back]').forEach(button => button.addEventListener('click', () => showStep(currentStep - 1)));
  receivingForm.addEventListener('submit', event => {
    if (currentStep < steps.length - 1) {
      event.preventDefault();
      if (validateStep(currentStep)) showStep(currentStep + 1);
      return;
    }
    for (let i = 0; i < steps.length; i++) {
      if (!validateStep(i)) { event.preventDefault(); return; }
    }
  });
  updateRows();
  showStep(0);
}
