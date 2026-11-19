(() => {
 const section=document.querySelector('[data-fastcam-options]');if(!section)return;
 const field=name=>section.querySelector(`[name="fastcam_${name}"]`), enable=field('enabled');
 const fields=section.querySelector('[data-fastcam-fields]'),trigger=section.querySelector('[data-fastcam-trigger]');
 const message=section.querySelector('[data-fastcam-message]'),source=document.querySelector('[name=machine_source]');
 let loaded=false,busy=false;
 const update=()=>{const live=source?.value==='live';enable.disabled=!loaded||busy||!live;fields.disabled=!enable.checked||enable.disabled;trigger.disabled=!field('external_trigger').checked;section.hidden=!live;
  const duration=Number(document.querySelector('[name=duration_seconds]').value),start=field('start_seconds'),end=field('end_seconds');
  start.max=duration;end.max=duration;end.setCustomValidity(Number(end.value)>Number(start.value)&&Number(end.value)<=duration?'':'End must be after start and within the job duration.');
 };
 async function load(){
  if(busy)return;busy=true;update();message.textContent='Loading current Waveform Generator camera settings…';
  try{
   const response=await fetch('/machine-fastcam/defaults',{cache:'no-store'});
   if(!response.ok||!response.headers.get('content-type')?.includes('application/json'))throw Error('Camera settings unavailable. Check the listener and retry.');
   const data=await response.json();
   for(const [key,value] of Object.entries(data.settings)){const input=field(key==='exposure_ms'?'exposure_us':key);if(!input)continue;if(input.type==='checkbox')input.checked=Boolean(value);else input.value=key==='exposure_ms'?Math.round(value*1000):value??'';}
   for(const [key,value] of Object.entries(data.trigger)){const input=field('trigger_'+key);if(!input)continue;if(input.type==='checkbox')input.checked=Boolean(value);else input.value=value??'';}
   loaded=true;message.textContent=`Loaded ${data.source}. Camera commands have not been sent.`;
  }catch(error){message.textContent=error.message;}
  finally{busy=false;update();}
 }
 section.addEventListener('toggle',()=>{if(section.open&&!loaded)load();});
 section.querySelector('[data-fastcam-reload]').addEventListener('click',load);
 enable.addEventListener('change',update);field('external_trigger').addEventListener('change',update);source?.addEventListener('change',update);
 for(const name of ['start_seconds','end_seconds'])field(name).addEventListener('input',update);
 document.querySelector('[name=duration_seconds]').addEventListener('input',update);
 update();
})();
