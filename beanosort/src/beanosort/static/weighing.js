// Exact milligram arithmetic for gross/tare display. Server revalidates all input.
const weighingForm = document.querySelector('[data-weighing-form]');
if (weighingForm) {
  function milligrams(value) {
    const parts = value.match(/^(\d+)(?:\.(\d{1,6}))?$/);
    return parts ? BigInt(parts[1])*1000000n+BigInt((parts[2] || '').padEnd(6,'0')) : null;
  }
  function kilograms(value) {
    const fraction=(value%1000000n).toString().padStart(6,'0');
    return (value/1000000n).toString()+'.'+(value%1000n===0n ? fraction.slice(0,3) : fraction);
  }
  for (const row of weighingForm.querySelectorAll('[data-weigh-row]')) {
    const gross=row.querySelector('[data-weigh-gross]');
    const tare=row.querySelector('[data-weigh-tare]');
    const confirmation=row.querySelector('[data-weigh-confirm]');
    function update() {
      const tareMg=tare ? milligrams(tare.value) : gross.dataset.knownTare ? BigInt(gross.dataset.knownTare) : null;
      if (tare && tareMg!==null && !gross.dataset.manuallyEdited && gross.dataset.predictedNet!=='') {
        gross.value=kilograms(tareMg+BigInt(gross.dataset.predictedNet));
      }
      const grossMg=milligrams(gross.value);
      gross.setCustomValidity('');
      if (grossMg!==null && tareMg!==null && grossMg<tareMg) gross.setCustomValidity('Gross weight cannot be less than container tare.');
      row.querySelector('[data-net-weight]').textContent=grossMg!==null && tareMg!==null && grossMg>=tareMg ?
        `Net beans: ${kilograms(grossMg-tareMg)} kg.` : 'Enter gross weight and a known tare to calculate net beans.';
    }
    gross.addEventListener('input',()=>{gross.dataset.manuallyEdited='true';confirmation.checked=false;update();});
    tare?.addEventListener('input',()=>{confirmation.checked=false;update();});
    update();
  }
}
