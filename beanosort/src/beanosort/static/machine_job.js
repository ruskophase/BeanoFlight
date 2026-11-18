(() => {
 const panel=document.querySelector('#machine-job');if(!panel)return;
 const $=s=>panel.querySelector(s), allowed={start:['prepared'],pause:['starting','running'],resume:['paused'],end:['starting','running','paused','draining'],stop:['starting','running','paused','draining']};
 let lastState=null;
 async function refresh(){
  try{
   const response=await fetch(panel.dataset.statusUrl,{cache:'no-store'});if(!response.ok)throw Error('Unable to read job status.');
   const d=await response.json(),s=d.status,m=s.metrics||{},n=m.tracked_beans||0;
   $('[data-machine-state]').textContent=d.state.replaceAll('_',' '); const heading=document.querySelector('.page-head h1');if(heading)heading.textContent=d.state[0].toUpperCase()+d.state.slice(1);
   $('[data-machine-time]').textContent=`· ${Math.floor(s.active_seconds||0)} s feeding / ${Math.floor(s.elapsed_seconds||0)} s elapsed`;
   $('[data-machine-progress]').value=s.active_seconds||0;
   const error=s.connection_error||s.error;$('[data-machine-error]').hidden=!error;$('[data-machine-error]').textContent=error||'';
   $('[data-machine-pending]').textContent=d.pending_commands.length?'Waiting for machine: '+d.pending_commands.join(', '):'Numerical journal received through event '+d.cursor;
   for(const form of panel.querySelectorAll('[data-machine-action]'))form.hidden=!allowed[form.dataset.machineAction].includes(d.state);
   const percent=v=>n?`${(100*(v||0)/n).toFixed(1)}%`:'—';
   $('[data-machine-coverage]').textContent=`${n} tracked beans · ${m.measurements||0} stereo measurements · colour ${percent(m.beans_with_colour)} · volume ${percent(m.beans_with_volume)} · ${m.beans_with_two_pairs||0} beans with at least two stereo pairs · ${m.failed_measurements||0} failed attempts · ${m.pending_measurements||0} pending. ${m.measurements_before_deadline||0} samples available before the nominal firing deadline; 0 used for decisions (All Pass-through). ${m.opportunities_skipped_busy||0} frame/bean opportunities skipped while busy.`;
   $('[data-machine-images]').textContent=`${m.image_pairs_saved||0} stereo pairs saved; ${m.image_pairs_skipped||0} image pairs skipped; ${m.image_pairs_failed||0} save failures. ${s.raw_bundle?'RAW recording: '+s.raw_bundle+'. ':''}Images remain on the Jetson.`;
   if(s.raw_recording_report){const raw=s.raw_recording_report;$('[data-machine-images]').textContent+=` RAW frames: CamL ${raw.CamL?.frame_count||0}, CamR ${raw.CamR?.frame_count||0}; ${raw.pairing?.pair_count||0} stereo pairs. ${(raw.integrity_warnings||[]).join('; ')}`;}
   $('[data-machine-change]').hidden=!(d.state==='paused'&&s.pause_drained&&d.cursor>=(s.cursor||0));
   if(d.weigh_url){window.location.assign(d.weigh_url);return;}
   if(lastState!==null&&lastState!==d.state&&d.state==='failed'){window.location.reload();return;}lastState=d.state;
  }catch(e){$('[data-machine-error]').hidden=false;$('[data-machine-error]').textContent=e.message;}
  setTimeout(refresh,1000);
 }
 refresh();
})();
