// Copy the exact mass string supplied by the server; leave manual edits alone.
const processingSource = document.querySelector('[data-processing-source]');
if (processingSource) {
  const quantity = processingSource.form.elements.namedItem('planned_mass');
  const origin = document.querySelector('[data-source-origin]');
  function showSourceOrigin() {
    if (origin) origin.textContent = processingSource.selectedOptions[0]?.dataset.sourceDescription || '';
  }
  function defaultProcessingQuantity() {
    quantity.value = processingSource.selectedOptions[0]?.dataset.availableMass || '';
    quantity.dispatchEvent(new Event('input', {bubbles: true}));
  }
  processingSource.addEventListener('change', defaultProcessingQuantity);
  processingSource.addEventListener('change', showSourceOrigin);
  showSourceOrigin();
  // Saved partial quantities on an existing preparation must survive page load.
  if (processingSource.value && !quantity.value) defaultProcessingQuantity();
}

// A vessel can have only one role in this sorting job. Restore choices when a
// selection changes; the server independently enforces the same constraint.
const sortingDestinations = [...document.querySelectorAll('[data-sort-destination]')];
if (sortingDestinations.length) {
  const recipeChoice = document.querySelector('[name="recipe_id"]');
  function updateDestinationChoices(event) {
    const hopper = recipeChoice?.selectedOptions?.[0]?.dataset.hopperEmptying === 'true' || sortingDestinations[0]?.form?.dataset.hopperEmptying === 'true';
    const selected = sortingDestinations.find(select => select.name === 'selected_id');
    if (selected) {
      selected.disabled = hopper; selected.required = !hopper;
      selected.closest('label').hidden = hopper;
      if (hopper) selected.value = '';
    }
    const sourceAllowed = select => hopper && select.name === 'pass_through_id';
    const sourceContainer = processingSource?.selectedOptions[0]?.dataset.sourceContainer || sortingDestinations[0]?.form?.dataset.sourceContainer;
    const changed = event?.currentTarget;
    if (sortingDestinations.includes(changed) && changed.value) {
      for (const other of sortingDestinations) if (other !== changed && other.value === changed.value) other.value = '';
    }
    const seen = new Set();
    for (const select of sortingDestinations) {
      if (select.value && ((select.value === sourceContainer && !sourceAllowed(select)) || seen.has(select.value))) select.value = '';
      if (select.value) seen.add(select.value);
    }
    for (const select of sortingDestinations) {
      if (select.value && select.value === sourceContainer && !sourceAllowed(select)) select.value = '';
      const taken = new Set(sortingDestinations.filter(other => other !== select).map(other => other.value).filter(Boolean));
      for (const option of select.options) {
        const unavailable = Boolean(option.value && ((option.value === sourceContainer && !sourceAllowed(select)) || taken.has(option.value)) && option.value !== select.value);
        option.disabled = unavailable; option.hidden = unavailable;
      }
    }
  }
  sortingDestinations.forEach(select => select.addEventListener('change',updateDestinationChoices));
  processingSource?.addEventListener('change',updateDestinationChoices);
  recipeChoice?.addEventListener('change',updateDestinationChoices);
  updateDestinationChoices();
}

// Display-only estimates. The server calculates and pins decimal quantities.
document.querySelectorAll('[data-count-preview]').forEach((output) => {
  const input = document.querySelector(`[name="${output.dataset.countPreview}"]`);
  const optIn = document.querySelector('[name="estimate_particles"]');
  if (!input) return;
  function update() {
    const populationMean = processingSource?.selectedOptions[0]?.dataset.populationMeanMg;
    const count = populationMean ? Number(input.value) * 1e6 / Number(populationMean) : Number(input.value) * 1e9 / Number(output.dataset.meanUg);
    output.textContent = optIn && !optIn.checked ? 'Particle count will remain unknown.' :
      (Number.isFinite(count) && count > 0 ?
        `Approximately ${Math.round(count).toLocaleString()} particles · calculated prediction, not a census.` :
        'Enter a positive mass to preview the estimated particle count.');
  }
  input.addEventListener('input', update);
  optIn?.addEventListener('change', update);
  update();
});

// Bounded requests only; no hardware calls or transaction held between batches.
document.querySelectorAll('[data-simulate-all]').forEach((button) => {
  const stop = document.querySelector('[data-simulate-stop]');
  const status = document.querySelector('[data-simulation-status]');
  const progress = document.querySelector('[data-simulation-progress]');
  let running = false;
  stop.addEventListener('click', () => {
    running = false;
    stop.disabled = true;
    status.textContent = 'Stopping after the current batch…';
  });
  button.addEventListener('click', async () => {
    if (running) return;
    running = true;
    button.disabled = true;
    stop.hidden = false;
    stop.disabled = false;
    status.textContent = 'Generating synthetic particles in resumable batches…';
    status.setAttribute('aria-busy', 'true');
    try {
      while (running) {
        const body = new URLSearchParams({csrf: button.dataset.csrf});
        const response = await fetch(button.dataset.simulateAll, {
          method: 'POST', body, credentials: 'same-origin', headers: {Accept: 'application/json'}
        });
        if (!response.ok || !response.headers.get('content-type')?.includes('application/json')) {
          throw new Error('Generation stopped. Reload to inspect the job state or sign in again. Committed batches are saved.');
        }
        const data = await response.json();
        progress.value = data.received;
        status.textContent = `${data.received.toLocaleString()} / ${data.expected.toLocaleString()} synthetic particles saved. Detailed profiles refresh when generation stops.`;
        if (data.complete || data.paused) running = false;
        if (running) await new Promise((resolve) => setTimeout(resolve, 80));
      }
      window.BeanoSortBusy?.show('Refreshing run populations and evidence…');
      window.location.reload();
    } catch (error) {
      status.textContent = error.message || 'Connection interrupted; reload to resume saved progress.';
    } finally {
      running = false;
      button.disabled = false;
      stop.hidden = true;
      status.setAttribute('aria-busy', 'false');
    }
  });
});
