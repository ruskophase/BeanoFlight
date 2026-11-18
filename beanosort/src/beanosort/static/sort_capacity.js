// Capacity decisions are committed on the server; polling updates fill bars and warnings.
(() => {
  const panel=document.querySelector('[data-capacity-monitor]');
  if (!panel || !['prepared','running','paused','failed','finishing','awaiting_reconciliation'].includes(panel.dataset.status)) return;
  const messages=panel.querySelector('[data-capacity-messages]');
  const actions=panel.querySelector('[data-capacity-actions]');
  const fills=document.querySelector('[data-receiver-fill]');
  const grid=fills?.querySelector('[data-fill-grid]');
  let state=panel.dataset.status;
  let refreshPending=false;
  function kg(mg) {return (Number(mg)/1000000).toFixed(6).replace(/\.?0+$/,'') || '0';}
  function newFillCard(id) {
    const card=document.createElement('article');card.className='receiver-fill-card';card.dataset.fillReceiver=id;
    card.innerHTML='<h3 data-fill-title></h3><a data-fill-container></a><p class="receiver-fill-value" data-fill-value></p>'+
      '<div class="receiver-fill-track" data-fill-track><span class="receiver-fill-amount green" data-fill-amount aria-hidden="true"></span>'+
      '<span class="receiver-fill-nominal" data-fill-marker aria-hidden="true"></span></div>'+
      '<div class="receiver-fill-axis" data-fill-axis aria-hidden="true"><span>0%</span><span class="receiver-fill-nominal-label" data-fill-nominal>100%</span><span class="receiver-fill-max" data-fill-max></span></div>'+
      '<p class="muted receiver-fill-note" data-fill-note></p>';
    return card;
  }
  function renderFills(rows) {
    if (!fills || !grid) return;
    fills.hidden=rows.length===0;
    const percentages=rows.map(row=>Number(row.percent_exact)).filter(Number.isFinite);
    const scale=Math.ceil(Math.max(100,...percentages)/25)*25;
    fills.dataset.scale=String(scale);
    const cards=new Map([...grid.querySelectorAll('[data-fill-receiver]')].map(card=>[card.dataset.fillReceiver,card]));
    for (const row of rows) {
      const card=cards.get(row.destination_id) || newFillCard(row.destination_id);
      cards.delete(row.destination_id);
      if (!card.parentNode) grid.append(card);
      card.querySelector('[data-fill-title]').textContent=row.stream;
      const link=card.querySelector('[data-fill-container]');link.href='/containers/'+encodeURIComponent(row.container_id);link.textContent=row.label;
      const value=card.querySelector('[data-fill-value]');
      value.textContent=`${row.mass_known?'≈ ':'At least '}${kg(row.mass_mg)} kg / ${row.capacity_mg===null?'nominal capacity not recorded':kg(row.capacity_mg)+' kg nominal'}`;
      const known=row.percent_exact!==null && Number.isFinite(Number(row.percent_exact));
      const percent=known ? Math.max(0,Number(row.percent_exact)) : 0;
      if (known) {const strong=document.createElement('strong');strong.textContent=row.fill_percent+'%';value.append(' · ',strong);}
      const amount=card.querySelector('[data-fill-amount]');amount.style.width=(percent*100/scale)+'%';
      amount.className='receiver-fill-amount '+(row.level==='normal'?'green':row.level);
      const track=card.querySelector('[data-fill-track]');
      if (known) {
        track.setAttribute('role','progressbar');track.setAttribute('aria-label',`${row.stream} · ${row.label} fill`);
        track.setAttribute('aria-valuemin','0');track.setAttribute('aria-valuemax',String(scale));track.setAttribute('aria-valuenow',String(percent));
        track.setAttribute('aria-valuetext',`${row.mass_known?'Estimated':'At least'} ${row.fill_percent}% of nominal capacity`);
      } else {
        track.setAttribute('role','img');track.setAttribute('aria-label','Nominal capacity not recorded; fill percentage unavailable');
        for (const name of ['aria-valuemin','aria-valuemax','aria-valuenow','aria-valuetext']) track.removeAttribute(name);
      }
      const marker=card.querySelector('[data-fill-marker]');marker.style.left=(10000/scale)+'%';marker.hidden=!known;
      card.querySelector('[data-fill-axis]').hidden=!known;
      card.querySelector('[data-fill-nominal]').style.left=(10000/scale)+'%';
      const maximum=card.querySelector('[data-fill-max]');maximum.textContent=scale+'%';maximum.hidden=scale===100;
      card.querySelector('[data-fill-note]').textContent=row.basis+(known?'':'. Fill percentage is unavailable without a nominal capacity.');
    }
    for (const card of cards.values()) card.remove();
  }
  async function refresh() {
    if (refreshPending) return;
    refreshPending=true;
    try {
      const response=await fetch(panel.dataset.url,{credentials:'same-origin',cache:'no-store',headers:{Accept:'application/json'}});
      if (!response.ok || !response.headers.get('content-type')?.includes('application/json')) return;
      const data=await response.json();
      renderFills(data.receivers);
      const warnings=data.receivers.filter(row=>row.level!=='normal');
      panel.hidden=warnings.length===0;
      messages.replaceChildren(...warnings.map(row=>{
        const wrapper=document.createElement('div');wrapper.className=`capacity-message ${row.level}`;
        const title=document.createElement('strong');
        title.textContent=`${row.stream} · ${row.label}: ${row.mass_known?'≈ ':'at least '}${row.fill_percent}% of nominal capacity`;
        const body=document.createElement('p');
        body.textContent=`${kg(row.mass_mg)} kg / ${kg(row.capacity_mg)} kg nominal. ${row.basis}. ${data.status==='paused'?'Workflow paused; resuming overrides this level. Next pause: '+row.resume_pause_percent+'%.':'Next automatic pause: '+row.next_pause_percent+'%.'}`;
        wrapper.append(title,body);return wrapper;
      }));
      if (state!==data.status) {
        actions.replaceChildren();state=data.status;
        if (state==='paused') {
          const form=document.createElement('form');form.method='post';form.action=panel.dataset.transition;
          for (const [name,value] of Object.entries({csrf:panel.dataset.csrf,target:'running',reason:'Operator reviewed receiving capacity and explicitly resumed'})) {
            const input=document.createElement('input');input.type='hidden';input.name=name;input.value=value;form.append(input);
          }
          const button=document.createElement('button');button.textContent='Override capacity warning & resume';form.append(button);
          const link=document.createElement('a');link.className='button secondary';link.href=panel.dataset.url.replace('/capacity-status','');link.textContent='View paused job / change containers';
          actions.append(form,link);
        }
      }
    } catch (_) { /* Keep the most recent warning visible during a connection gap. */ }
    finally {refreshPending=false;}
  }
  const timer=setInterval(()=>{if (!document.hidden) refresh();},2000);
  window.addEventListener('pagehide',()=>clearInterval(timer),{once:true});
})();
