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
   if($('[data-machine-manual-camera]')){const c=s.manual_fastcam,action=c?.last_command;$('[data-machine-manual-camera]').textContent=c?.manual?`Manual high-speed camera: ${c.camera?.state||'status pending'}. ${c.busy?'Camera command in progress. ':''}${action?action.action+': '+action.state+'. ':''}${c.error||''}`:'';}
   if($('[data-machine-fastcam]')){
    const f=s.fastcam||{state:'waiting'},r=f.camera_report||{},q=r.recorder,p=q?.policy||r.recorder_policy||{},el=$('[data-machine-fastcam]');
    const stale=f.state==='recording'&&f.last_status_received_at&&Date.now()/1000-f.last_status_received_at>4;
    el.textContent=`FastCamUSB: ${f.state}. ${f.error||f.recording_error||''} ${f.recording_failed_early?'Recorder failed before the scheduled stop. ':f.window_ended_early||f.ended_early?'Recording window ended early. ':''}${f.start_acknowledged?'Recording start acknowledged. ':''}${f.stop_acknowledged?'Recording stop acknowledged. ':''}${r.recorded_frames!==undefined?r.recorded_frames+' frames reported written. ':''}${f.monitor_error||stale?'Recorder telemetry is unavailable or stale; current recording state is unknown. ':''}`;
    el.classList.toggle('notice',Boolean(f.error||f.recording_error||f.ended_early||f.monitor_error||stale));
    $('[data-machine-fastcam-files]').textContent=[r.video_path,r.timestamp_path,q?.diagnostics_path].filter(Boolean).join(' · ');
    const details=$('[data-machine-fastcam-diagnostics]');
    if(details){
     const mb=v=>(Number(v||0)/1048576).toFixed(1),ms=v=>Number(v||0).toFixed(1);
     details.textContent=q?`Buffer: ${q.buffered_frames} / ${p.frame_limit} frames, ${mb(q.buffered_bytes)} / ${mb(p.byte_limit)} MiB; peak ${q.peak_buffered_frames} frames (${mb(q.peak_buffered_bytes)} MiB). Longest write: ${ms(q.max_write_ms)} ms; ${q.stall_count} writes ≥ ${p.stall_threshold_ms} ms. ${q.rejected_frames} rejected; ${q.abandoned_frames} accepted frames not written. ${q.current_write_ms>p.stall_threshold_ms?'Current write waiting '+ms(q.current_write_ms)+' ms. ':''}${q.failure_at?'Pi recorder failure time (UTC): '+q.failure_at+'. ':''}${f.failure_observed_elapsed_seconds!==undefined&&f.failure_observed_elapsed_seconds!==null?'Failure first received at job +'+Number(f.failure_observed_elapsed_seconds).toFixed(1)+' s. ':''}${q.diagnostics_error?'Diagnostics could not be saved: '+q.diagnostics_error:''}`:'Recorder diagnostics are unavailable for this recording.';
    }
   }
   if(s.raw_recording_report){const raw=s.raw_recording_report;$('[data-machine-images]').textContent+=` RAW frames: CamL ${raw.CamL?.frame_count||0}, CamR ${raw.CamR?.frame_count||0}; ${raw.pairing?.pair_count||0} stereo pairs. ${(raw.integrity_warnings||[]).join('; ')}`;}
   $('[data-machine-change]').hidden=!(d.state==='paused'&&s.pause_drained&&d.cursor>=(s.cursor||0));
   if(d.weigh_url){window.location.assign(d.weigh_url);return;}
   if(lastState!==null&&lastState!==d.state&&d.state==='failed'){window.location.reload();return;}lastState=d.state;
  }catch(e){$('[data-machine-error]').hidden=false;$('[data-machine-error]').textContent=e.message;}
  setTimeout(refresh,1000);
 }
 refresh();
})();
