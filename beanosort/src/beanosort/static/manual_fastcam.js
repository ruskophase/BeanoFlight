(() => {
 const panel=document.querySelector('#manual-fastcam');if(!panel)return;
 const $=s=>panel.querySelector(s),form=$('[data-camera-settings]'),inputs=Object.fromEntries([...form.elements].filter(x=>x.name).map(x=>[x.name,x]));
 let state=null,connected=false,busy=false,dirty=false,revision=0,counter=0,pendingId=null,commandError='',connectionError='',imageURL=null,imageEpoch=0;
 const previewNames=new Set(['preview_fps','disable_preview','disable_preview_during_recording']);
 function clearPreview(){imageEpoch++;if(imageURL)URL.revokeObjectURL(imageURL);imageURL=null;$('[data-camera-preview]').hidden=true;$('[data-camera-preview]').removeAttribute('src');}
 async function request(path,body){
  const abort=new AbortController(),timer=setTimeout(()=>abort.abort(),12000);
  try{
   const response=await fetch(panel.dataset.url+'/'+path,{cache:'no-store',signal:abort.signal,...(body?{method:'POST',body}:{})});
   if(!response.ok){let message='Camera request failed; refresh status before trying again.';try{const e=await response.json();message=e.error||e.detail||message;}catch(_){}throw Error(message);}
   return await response.json();
  }catch(e){throw Error(e.name==='AbortError'?'Camera request timed out. It was not retried; check status before another action.':e.message);}
  finally{clearTimeout(timer);}
 }
 function render(){
  const c=state?.camera,s=c?.settings,active=Boolean(c?.camera_running||c?.recording||c?.armed||c?.finishing);
  const owner=connected&&state?.controls_available&&state?.manual,locked=busy||Boolean(state?.busy),recording=Boolean(c?.recording||c?.armed||c?.finishing);
  $('[data-camera-state]').textContent=state&&!state.controls_available?'This job is not running or paused; camera controls are unavailable.':!connected?'Camera status unavailable / disconnected':`FastCamUSB: ${c?.state||'stopped'} · ${state.manual?'Manual camera mode':'Automatic camera window / manual mode not selected'} · status received ${new Date(state.last_received_at*1000).toLocaleTimeString()}`;
  const error=commandError||connectionError||state?.connection_error||state?.error||c?.error||'';
  $('[data-camera-error]').textContent=error;$('[data-camera-error]').hidden=!error;
  $('[data-camera-takeover]').hidden=Boolean(state?.manual);
  for(const button of panel.querySelectorAll('[data-camera-action]')){
   const a=button.dataset.cameraAction;
   button.disabled=locked||!(a==='take_manual'?connected&&state?.controls_available:owner);
   if(a==='start')button.disabled ||= active||dirty;
   if(a==='record')button.disabled ||= !c?.camera_ready||recording||dirty;
   if(a==='stop_recording')button.disabled ||= !recording||Boolean(c?.finishing);
   if(a==='stop_camera')button.disabled ||= !active;
  }
  $('[data-camera-action=record]').textContent=s?.external_trigger?'Record (arm)':'Record';
  const command=state?.last_command;
  $('[data-camera-command]').textContent=locked?'Camera command in progress…':command?`${command.action}: ${command.state}${command.error?' · '+command.error:''}`:'';
  $('[data-camera-command]').setAttribute('aria-busy',String(locked));
  form.querySelector('fieldset').disabled=!owner||locked;
  for(const [key,input] of Object.entries(inputs)){
   input.disabled=active&&!previewNames.has(key);
   if(s&&!dirty){if(input.type==='checkbox')input.checked=Boolean(s[key]);else input.value=key==='exposure_us'?Math.round(s.exposure_ms*1000):s[key]??'';}
  }
  form.querySelector('button').disabled=!dirty||locked;
  $('[data-camera-unsaved]').textContent=dirty?' Unsaved changes':'';
  const h=state?.high_speed;
  $('[data-camera-trigger]').textContent=h?.available?`GPIO33: ${h.enabled?'ON':'OFF'} · ${Number(h.frequency_hz).toFixed(3)} Hz · current timing retained.`:'GPIO33 trigger status unavailable. Free-running recording does not need GPIO33.';
  $('[data-camera-metrics]').textContent=c?`Capture ${Number(c.capture_fps||0).toFixed(1)} fps · stream ${Number(c.stream_fps||0).toFixed(1)} fps · ${c.recorded_frames||0} frames reported written. AVI playback rate: ${c.playback_fps||s?.fps} fps.`:'';
  const q=c?.recorder,p=q?.policy;
  $('[data-camera-diagnostics]').textContent=q?`Buffer ${q.buffered_frames}/${p?.frame_limit} frames; peak ${q.peak_buffered_frames}. Longest write ${Number(q.max_write_ms||0).toFixed(1)} ms; ${q.stall_count} stalls. ${q.failure_at?'Recorder failure time (Pi UTC): '+q.failure_at:''}`:'';
  $('[data-camera-files]').textContent=[c?.video_path,c?.timestamp_path,q?.diagnostics_path].filter(Boolean).join(' · ');
  const preview=connected&&state?.controls_available&&c?.camera_running&&!c.preview_disabled&&$('[data-camera-show-preview]').checked&&!document.hidden;
  if(!preview)clearPreview();
  $('[data-camera-preview-note]').textContent=!preview?(c?.preview_disabled?'Camera preview is disabled in its settings.':'Preview paused or camera stopped.'):(imageURL?'Live preview':'Waiting for a preview frame…');
 }
 async function poll(){
  if(!busy){const version=revision;
   try{const next=await request('status');if(version===revision){state=next;connected=Boolean(next.connected);connectionError='';
    if(pendingId&&next.last_command?.id===pendingId&&next.last_command.state!=='requested'){
     if(next.last_command.state==='acknowledged'&&next.last_command.action==='settings')dirty=false;
     pendingId=null;
    }render();}}
   catch(e){if(version===revision){connected=false;connectionError=e.message;clearPreview();render();}}
  }
  setTimeout(poll,1000);
 }
 async function command(payload){
  if(busy||state?.busy)return;busy=true;revision++;commandError='';pendingId=panel.dataset.prefix+'-'+(++counter);render();
  const body=new FormData();body.set('csrf',panel.dataset.csrf);body.set('key',pendingId);body.set('payload',JSON.stringify(payload));
  try{await request('commands',body);state={...state,busy:true};}
  catch(e){commandError=e.message;}
  finally{busy=false;revision++;render();}
 }
 for(const button of panel.querySelectorAll('[data-camera-action]'))button.addEventListener('click',()=>command({action:button.dataset.cameraAction}));
 form.addEventListener('input',()=>{dirty=true;render();});
 form.addEventListener('submit',event=>{
  event.preventDefault();if(!form.checkValidity())return;
  const settings={};
  for(const [key,input] of Object.entries(inputs)){
   const name=key==='exposure_us'?'exposure_ms':key;
   const value=input.type==='checkbox'?input.checked:key==='device'?input.value.trim()||null:input.type==='number'||key==='width'?Number(input.value)/(key==='exposure_us'?1000:1):input.value.trim();
   if(value!==state?.camera?.settings[name])settings[name]=value;
  }
  if(Object.keys(settings).length)command({action:'settings',settings});else{dirty=false;render();}
 });
 async function preview(){
  const c=state?.camera;
  if(connected&&state?.controls_available&&c?.camera_running&&!c.preview_disabled&&!document.hidden&&$('[data-camera-show-preview]').checked){
   const epoch=imageEpoch,abort=new AbortController(),timer=setTimeout(()=>abort.abort(),5000);
   try{const response=await fetch(panel.dataset.url+'/frame',{cache:'no-store',signal:abort.signal});
    if(response.ok){const blob=await response.blob();if(epoch===imageEpoch&&!document.hidden){const previous=imageURL;imageURL=URL.createObjectURL(blob);$('[data-camera-preview]').src=imageURL;$('[data-camera-preview]').hidden=false;if(previous)URL.revokeObjectURL(previous);}}
   }catch(_){}finally{clearTimeout(timer);}
  }
  setTimeout(preview,200);
 }
 $('[data-camera-show-preview]').addEventListener('change',()=>{clearPreview();render();});
 document.addEventListener('visibilitychange',()=>{clearPreview();render();});
 render();poll();preview();
})();
