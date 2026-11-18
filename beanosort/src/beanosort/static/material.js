// Inline review only. Exact stock changes and stale-form checks run on the server.
(() => {
  const form = document.querySelector('[data-material-form]');
  if (!form) return;
  const preview = form.querySelector('[data-material-preview]');
  const kind = form.dataset.materialForm;
  const reason = form.elements.namedItem('reason');
  function mg(value) {
    const parts = String(value).match(/^(\d+)(?:\.(\d{1,6}))?$/);
    return parts ? BigInt(parts[1])*1000000n + BigInt((parts[2] || '').padEnd(6,'0')) : null;
  }
  function kg(value) {
    const sign = value < 0n ? '-' : ''; value = value < 0n ? -value : value;
    const fraction = (value%1000000n).toString().padStart(6,'0').replace(/0+$/,'');
    return `${sign}${value/1000000n}${fraction ? '.'+fraction : ''}`;
  }
  function say(lines) {
    preview.replaceChildren(...lines.map(text => {const p=document.createElement('p'); p.textContent=text; return p;}));
  }
  const source = form.elements.namedItem('source_id');
  const destination = form.elements.namedItem('destination_id');
  if (destination) for (const option of destination.options) option.dataset.originallyDisabled=String(option.disabled);
  function update() {
    if (kind === 'contents_move') {
      const input=form.elements.namedItem('moved_mass');
      const empty=form.elements.namedItem('empty_source').checked;
      input.disabled=empty; input.required=!empty;
      form.querySelector('[data-moved-mass-label]').classList.toggle('mass-not-required',empty);
      form.querySelector('[data-whole-move-note]').hidden=!empty;
      form.querySelector('[data-population-assumption]').hidden=empty;
      form.elements.namedItem('representative').disabled=empty;
      input.setCustomValidity(''); reason.required=false;
      destination.setCustomValidity('');
      for (const option of destination.options) option.disabled=option.dataset.originallyDisabled==='true' || Boolean(source.value && option.value===source.value);
      if (destination.value && source.value===destination.value) destination.value='';
      if (!source.value || !destination.value) {say([empty ? 'Choose two containers to transfer the entire recorded source balance.' : 'Choose two containers and enter the measured net mass moved.']);return;}
      const before=BigInt(source.selectedOptions[0].dataset.massMg);
      const moved=empty ? before : mg(input.value);
      if (moved===null || moved<=0n) {say(['Enter the measured net mass moved.']);return;}
      const existing=BigInt(destination.selectedOptions[0].dataset.massMg);
      const remainder=empty ? 0n : before-moved;
      const final=existing+moved;
      const capacity=destination.selectedOptions[0].dataset.capacityMg;
      if (!empty && remainder<=0n) input.setCustomValidity('For a complete movement, select Empty the source. Partial movements must leave material.');
      if (capacity && final>BigInt(capacity)) destination.setCustomValidity("The resulting contents exceed the destination's nominal capacity.");
      say([`Source: ${kg(before)} kg recorded → ${kg(remainder)} kg ${empty ? '(emptied)' : '(calculated remainder, not separately weighed)'}.`,
           `Destination: ${kg(existing)} kg recorded + ${kg(moved)} kg ${empty ? 'from the recorded source balance' : 'measured'} → ${kg(final)} kg.`,
           empty && !destination.selectedOptions[0].dataset.portion ? 'The existing portion ID will be retained.' : 'New portions will record the split or combination and its ancestry.']);
    } else {
      const select=form.elements.namedItem('container_id');
      const input=form.elements.namedItem('weight');
      const basis=form.elements.namedItem('weighing_basis').value;
      const removed=kind==='product_packaging' && basis==='removed';
      if (kind==='product_packaging') input.min=removed ? '0.000001' : '0';
      input.setCustomValidity('');
      const reading=mg(input.value);
      if (!select.value || reading===null) {say(['Choose a container and enter its current scale reading.']);return;}
      const option=select.selectedOptions[0];
      const gross=basis==='gross';
      if (gross && !option.dataset.tareMg) {input.setCustomValidity('Record the container tare first, or choose a net weighing.');say(['This container needs a recorded tare for a gross weighing. You can use a net weighing from an already tared scale.']);return;}
      const before=BigInt(option.dataset.massMg);
      const after=removed ? before-reading : reading-(gross ? BigInt(option.dataset.tareMg) : 0n);
      if (after<0n) {
        const message=removed ? 'The mass removed cannot exceed the recorded contents.' : 'Gross weight cannot be less than container tare.';
        input.setCustomValidity(message);say([message]);return;
      }
      const difference=after-before;
      if (kind === 'product_packaging') {
        const packaged=before-after;
        if (packaged<=0n) {
          const message=removed ? 'Enter a positive mass of contents removed.' : 'Remaining contents must be less than the recorded contents. Use Reconciliation for a correction or increase.';
          input.setCustomValidity(message);say([message]);
          return;
        }
        say([`Recorded contents: ${kg(before)} kg → ${removed ? 'Calculated' : 'Measured'} contents remaining: ${kg(after)} kg.`,
             `Final product leaving inventory: ${kg(packaged)} kg (${removed ? 'measured mass of contents removed; source container not reweighed' : 'recorded contents minus measured remainder'}).`,
             after===0n ? 'The source portion will close and the container will be freed.' : 'The remaining contents keep their portion ID.',
             'Recorded at Packaging desk. Individual packets and product stock are not tracked.']);
        return;
      }
      const lines=[`Recorded contents: ${kg(before)} kg → New net contents: ${kg(after)} kg.`,
                   `Adjustment: ${difference>0n ? '+' : ''}${kg(difference)} kg. ${after===0n ? 'The recorded portion will close and the container will be freed.' : option.dataset.portion ? 'The portion ID will be retained.' : 'Previously unrecorded material will receive a new portion ID with unknown origin and composition.'}`];
      if (option.dataset.capacityMg && after>BigInt(option.dataset.capacityMg)) lines.push('The measured mass exceeds nominal capacity. Check the units and weighing; the actual reading will be recorded.');
      say(lines);
    }
  }
  form.addEventListener('input',update);form.addEventListener('change',update);update();
})();
