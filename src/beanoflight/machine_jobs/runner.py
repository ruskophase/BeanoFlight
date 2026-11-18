"""Headless All Pass-through execution. No valve plan publisher is constructed."""
from concurrent.futures import ThreadPoolExecutor
from collections import Counter, defaultdict
import hashlib
import json
import math
from pathlib import Path
import queue
import shutil
import statistics
import threading
import time

from .store import encode, utc, fingerprint
from .controller import Controller


class Journal:
    def __init__(self, store, job):
        self.store=store;self.job=job;self.queue=queue.Queue(2048);self.error=None
        self.thread=threading.Thread(target=self._work,daemon=True);self.thread.start()
    def _work(self):
        while True:
            value=self.queue.get()
            try:
                if value is None:return
                if not self.error:self.store.append(self.job,*value)
            except Exception as exc:self.error=exc
            finally:self.queue.task_done()
    def put(self,kind,data):
        if self.error:raise RuntimeError('Numerical journal failed') from self.error
        try:self.queue.put((kind,data),timeout=.1)
        except queue.Full:raise RuntimeError('Numerical journal backlog exceeded its bound')
    def flush(self):
        self.queue.join()
        if self.error:raise RuntimeError('Numerical journal failed') from self.error
    def close(self):
        self.queue.put(None);self.thread.join()


class Runner:
    def __init__(self,store,key,request,config):
        self.store=store;self.key=key;self.request=request;self.config=config
        self.commands=queue.Queue();self.state='starting';self.active=0.;self.paused=0.;self.started=time.monotonic()
        self.metrics=Counter();self.error=None;self.controller=None;self.final_sequence=None
        self.thread=threading.Thread(target=self.run,name='machine-job-'+key,daemon=True)
        self.root=Path(config['data_root'])/key
        self.raw_bundle=None;self.raw_report=None;self.source_stats={};self.paused_at=None;self.pause_ready=False
    def command(self,action):
        if action not in {'pause','resume','end','stop'}:raise ValueError('Unknown command')
        self.commands.put(action)
        if action=='stop' and self.controller and self.controller.owned:
            self.controller.api('stop')
        return {'accepted':True,'action':action}
    def status(self):
        return {'state':self.state,'active_seconds':round(self.active,3),
            'paused_seconds':round(self.paused,3),
            'elapsed_seconds':round(time.monotonic()-self.started,3),'duration_seconds':self.request['duration_seconds'],
            'metrics':dict(self.metrics),'final_sequence':self.final_sequence,'error':self.error,
            'image_mode':self.request['image_mode'],'raw_bundle':self.raw_bundle,
            'raw_recording_report':self.raw_report,
            'source_statistics':self.source_stats,'pause_drained':self.pause_ready,
            'images_location':'Jetson only; image bytes are not exported',
            'decision_basis':'All Pass-through; no measurements required and no valve plans issued'}
    def persist(self):self.store.status(self.key,self.status())
    def run(self):
        import cv2
        from beanoflight.analysis import AnalysisEngine
        from beanoflight.calibration import MetricPlaneCalibration,find_pinkplane_homography
        from beanoflight.detection import DetectorSettings,RawGreenDetector
        from beanoflight.source import MMapRawVideoSource
        from beanoflight.live_source import SharedMemoryRawStereoSource,FastCapLiveProducer
        from beanoflight.nozzle_map import resolve_gate_layout
        from .measurements import Measurements
        cv2.setNumThreads(1)
        source=producer=measurer=None;pending=None;held=None;image_futures=[]
        workers=ThreadPoolExecutor(1,thread_name_prefix='stereo-measure')
        images=ThreadPoolExecutor(1,thread_name_prefix='stereo-save')
        journal=Journal(self.store,self.key);beans={};attempts=Counter();samples=defaultdict(list);timely=set()
        self.root.mkdir(parents=True,exist_ok=True)
        live=self.request['source']=='live';paced=self.request.get('paced',True)
        last_tick=time.monotonic();drain_until=None;last_status=0;last_frame_time=None
        bundle=self.request['settings']; fps=bundle['cameras']['frame_rate_hz']
        max_wall=self.request['duration_seconds']+self.config.get('maximum_pause_seconds',600)+60
        pack=Path(self.config['calibration_pack'])
        def emit(kind,data):journal.put(kind,data)
        def capture_event(e):
            if e.get('event')=='finalized':self.raw_bundle=e['path'];self.raw_report=e.get('report')
            if e.get('event')!='frame':emit('capture',e)
        def save_pair(row,left,right):
            target=self.root/'stereo'/str(row['bean_sequence']);target.mkdir(parents=True,exist_ok=True)
            paths=[]
            for cam,im in [('CamL',left),('CamR',right)]:
                path=target/f"{row['frame_index']:08d}-{cam}.png"
                if not cv2.imwrite(str(path),im):raise RuntimeError('Could not save stereo image')
                paths.append(str(path.relative_to(self.root)))
            return {'bean_sequence':row['bean_sequence'],'frame_index':row['frame_index'],'paths':paths}
        def collect_image(future):
            try:
                emit('images',future.result());self.metrics['image_pairs_saved']+=1
            except Exception as exc:
                self.metrics['image_pairs_failed']+=1
                emit('image_failed',{'error':str(exc)})
        def collect(wait=False):
            nonlocal pending,held
            if pending is None or (not wait and not pending[0].done()):return
            future,seq,deadline=pending
            try:
                result,reason=future.result()
                if result is None:
                    self.metrics['failed_measurements']+=1
                    emit('measurement_failed',{'bean_sequence':seq,'reason':reason})
                else:
                    row,left,right=result;row['bean_sequence']=seq
                    row['nominal_decision_deadline_monotonic_ns']=deadline
                    row['available_before_deadline']=deadline is not None and time.monotonic_ns()<=deadline
                    row['used_for_decision']=False
                    row['completed_monotonic_ns']=time.monotonic_ns()
                    self.metrics['measurements']+=1
                    self.metrics['measurement_processing_ms_total']+=row['measurement_ms']
                    if row['available_before_deadline']:
                        self.metrics['measurements_before_deadline']+=1
                        if seq not in timely:
                            timely.add(seq);self.metrics['beans_with_timely_measurements']+=1
                    if not samples[seq]:self.metrics['beans_with_colour']+=1;self.metrics['beans_with_volume']+=1
                    samples[seq].append(row)
                    if len(samples[seq])==2:self.metrics['beans_with_two_pairs']+=1
                    emit('measurement',row)
                    if self.request['image_mode']=='stereo':
                        if len(image_futures)<8:image_futures.append(images.submit(save_pair,row,left,right))
                        else:self.metrics['image_pairs_skipped']+=1
            finally:
                source.release_frame(held);held=None;pending=None;self.metrics['pending_measurements']=0
        def finalize(seq):
            bean=beans[seq]
            if bean.get('finalized'):return
            rows=samples.pop(seq,[])
            numeric={}
            if rows:
                for name,value in rows[0].items():
                    if (name.startswith(('caml_','camr_')) or name in {'projected_area_geomean_mm2','rotational_ellipsoid_volume_proxy_mm3'}) and type(value) in (int,float):
                        numeric[name]=statistics.median(r[name] for r in rows if name in r)
            bean['finalized']=True
            emit('bean',{'bean_sequence':seq,'ordinal':bean['ordinal'],'first_timestamp_ns':bean['first_timestamp_ns'],
                'sample_count':len(rows),'features':numeric,'decision':'pass','action':'not_issued',
                'decision_measurements_used':0,'measurement_attempts':attempts[seq],
                'measurement_status':'measured' if rows else 'missing','terminal_status':bean.get('terminal','run_ended')})
            self.metrics['finalized_beans']+=1
        try:
            # Pinned configuration is copied into the run before opening devices.
            (self.root/'request.json').write_text(encode(self.request))
            from .provenance import tree_hashes, file_hash
            code=tree_hashes(Path(__file__).parent)
            code.update({name:file_hash(Path(__file__).parent.parent/name) for name in (
                'statistics_features.py','source.py','live_source.py','analysis.py','tracking.py','calibration.py','live_clock.py')})
            emit('software',{'files':code,'opencv_version':cv2.__version__})
            emit('manifest',{'schema':'beano-machine-job/v1','request':self.request,
                'source_kind':'live' if live else 'archived_raw_replay','software':'all-pass-v1',
                'clock':'hardware counter with refreshed host mapping' if live else 'recorded timestamps; replay timing is not live timing',
                'measurement_kernel':'beanoflight.statistics_features','volume':'apparent rotational ellipsoid proxy, not weighed mass',
                'nominal_decision_deadline':{'lead_ms':bundle.get('valves',{}).get('lead_ms',0),'notice_ms':4},
                'images_exported':False,'started_at':utc()})
            if shutil.disk_usage(self.root).free<2*1024**3:raise RuntimeError('Less than 2 GiB free on machine')
            if live:
                shutil.copytree(pack,self.root/'calibration',dirs_exist_ok=True)
                pack=self.root/'calibration'
                nozzle_snapshot=self.root/'nozzle-map.json'
                shutil.copyfile(self.config['nozzle_map'],nozzle_snapshot)
                actual={'calibration':tree_hashes(pack),'nozzle_map_sha256':file_hash(nozzle_snapshot)}
                if self.request.get('configuration',{}).get('id')!=fingerprint(actual):
                    raise RuntimeError('Calibration changed before capture startup')
                self.controller=Controller(self.config['controller_url'],self.key);self.controller.acquire()
                self.controller.configure(bundle)
                self.controller.verify_configuration(bundle)
                paths={c:Path('/dev/shm')/('beano-job-'+self.key+'-'+c+'.raw') for c in ('CamL','CamR')}
                producer=FastCapLiveProducer(pack,self.root/'raw',paths,duration_seconds=max_wall,frame_rate_hz=fps,
                    controller_url=self.config['controller_url'].rstrip('/')+'/api/state',test_override=True,
                    record_raw=self.request['image_mode']=='raw',event_callback=capture_event)
                producer.start();producer.clock_snapshot()
                source=SharedMemoryRawStereoSource(pack,paths,frame_count=math.ceil(max_wall*fps),fps=fps,
                    clock_source_timestamp_ns=producer.clock_source_timestamp_ns,clock_monotonic_ns=producer.clock_monotonic_ns,
                    producer_check=producer.check_running,crop_processing='calibrated')
                background=source.acquire_background(15)
                homography=pack/'geometry/homography.json'
            else:
                recording=Path(self.config['recordings'][self.request['source']]['path'])
                source=MMapRawVideoSource(recording,crop_processing='calibrated');fps=source.metadata.fps
                homography=find_pinkplane_homography(recording)
                source.configure_stereo(homography,(2,8,14));background=source.build_background((2,8,14))
                if self.request['image_mode']=='raw':self.raw_bundle=str(recording)  # Existing immutable RAWs, never duplicate.
            calibration=MetricPlaneCalibration.from_pinkplane(homography,image_size_px=(source.metadata.width,source.metadata.height))
            nozzle=nozzle_snapshot if live else None
            layout=resolve_gate_layout(calibration,nozzle)
            engine=AnalysisEngine(calibration,RawGreenDetector(DetectorSettings().updated(close_kernel=3)),background,
                gate_layout=layout,positions_mapper=lambda p:calibration.pixels_to_mm(source.undistort_points(p)))
            engine.tracker.run_id=self.key
            measurer=Measurements(source,background,calibration)
            emit('calibration',{'homography_sha256':hashlib.sha256(Path(homography).read_bytes()).hexdigest(),
                'nozzle_layout':layout.to_dict(),'pipeline':source.pipeline_metadata,
                'settings_sha256':self.request['settings_sha256']})
            # The live source reserves frame zero explicitly after its empty
            # background is ready. Do this before any feeder start or resume.
            if live:source.prime()
            initially_paused=self.request.get('start_paused',False)
            if self.controller:
                if not initially_paused:self.controller.feed(True)
                self.controller.watch_progress=True
            self.state='paused' if initially_paused else 'running'
            last_tick=time.monotonic();replay_origin=last_tick
            if initially_paused:self.paused_at=last_tick
            source_origin=source.timestamp_ns(0) if not live else 0
            index=0;self.persist()
            while index<source.metadata.frame_count:
                tick=time.monotonic();delta=tick-last_tick;last_tick=tick
                if self.state=='running':self.active+=delta if live or paced else 1/fps
                if self.state=='paused':self.paused+=delta
                if self.paused>self.config.get('maximum_pause_seconds',600):raise RuntimeError('Maximum cumulative pause allowance reached')
                if time.monotonic()-self.started>max_wall:raise RuntimeError('Maximum wall duration reached, including pause allowance')
                if self.controller:
                    if self.controller.error:raise RuntimeError(self.controller.error)
                    self.controller.last_progress=tick
                while not self.commands.empty():
                    action=self.commands.get_nowait()
                    if action=='stop':
                        if self.controller:self.controller.api('stop')
                        raise RuntimeError('Immediate Stop all requested')
                    if action=='pause' and self.state=='running':
                        if self.controller:self.controller.feed(False)
                        self.state='paused';self.paused_at=tick;emit('state',{'state':'paused'})
                    if action=='resume' and self.state=='paused':
                        if self.controller:self.controller.feed(True)
                        replay_origin+=tick-self.paused_at
                        self.state='running';self.pause_ready=False;emit('state',{'state':'running'})
                    if action=='end' and self.state in {'running','paused'}:self.state='draining'
                if self.state=='running' and self.active>=self.request['duration_seconds']:self.state='draining'
                if self.state=='draining' and drain_until is None:
                    if self.controller:self.controller.feed(False)
                    drain_until=tick+2 if live else tick
                    emit('state',{'state':'draining'})
                collect()
                remaining=[]
                for f in image_futures:
                    if f.done():collect_image(f)
                    else:remaining.append(f)
                image_futures=remaining
                for seq,b in list(beans.items()):
                    if b.get('terminal') and not b.get('finalized') and (pending is None or pending[1]!=seq):finalize(seq)
                if drain_until is not None and tick>=drain_until:break
                if self.state=='paused' and self.paused_at and tick-self.paused_at>=2:
                    self.pause_ready=pending is None and all(b.get('finalized') for b in beans.values())
                    if self.pause_ready:journal.flush()
                if tick-last_status>=1:
                    self.source_stats=source.stereo_statistics()
                    self.metrics['pending_measurements']=int(pending is not None);self.persist();last_status=tick
                    if shutil.disk_usage(self.root).free<1024**3:raise RuntimeError('Machine storage reserve reached')
                if not live and self.state=='paused':time.sleep(.02);continue
                if not live and paced:
                    due=replay_origin+(source.timestamp_ns(index)-source_origin)/1e9
                    if tick<due:time.sleep(min(due-tick,.02));continue
                frame=source.frame(index);timestamp=source.timestamp_ns(index)
                try:
                    analysis=engine.process(frame,index,timestamp);self.metrics['frames_processed']+=1
                    predictions={p.bean_ref.sequence:p for p in analysis.predictions}
                    observations=[]
                    for track in analysis.tracks:
                        prediction=predictions.get(track.bean_ref.sequence)
                        observations.append({'bean_sequence':track.bean_ref.sequence,'status':track.status.value,
                            'hits':track.hits,'state_mm_and_mm_per_second':list(track.state),
                            'state_covariance':track.covariance,
                            'centroid_px':list(track.history[-1].detection.centroid_px) if track.history else None,
                            'observed_on_this_frame':bool(track.history and track.history[-1].frame_index==index),
                            'predicted_crossing_timestamp_ns':prediction.crossing_timestamp_ns if prediction else None,
                            'predicted_crossing_x_mm':prediction.x_mean_mm if prediction else None,
                            'predicted_crossing_x_std_mm':prediction.x_std_mm if prediction else None,
                            'predicted_crossing_time_std_ms':prediction.time_std_ms if prediction else None})
                    if observations:emit('observations',{'frame_index':index,'timestamp_ns':timestamp,'tracks':observations})
                    if live:
                        clock=producer.clock_snapshot();producer.drain_events()
                        source_host=clock.source_to_monotonic_ns(timestamp)
                        self.metrics['last_frame_age_ms']=round((time.monotonic_ns()-source_host)/1e6,3)
                    else:source_host=round((replay_origin+(timestamp-source_origin)/1e9)*1e9) if paced else time.monotonic_ns()
                    candidates=[]
                    for t in analysis.tracks:
                        seq=t.bean_ref.sequence
                        if t.status.value=='tentative' or t.hits<engine.tracker_settings.confirmation_hits:continue
                        if seq not in beans:
                            self.pause_ready=False
                            beans[seq]={'ordinal':len(beans),'first_timestamp_ns':t.history[0].timestamp_ns}
                            self.metrics['tracked_beans']+=1;emit('tracked',{'bean_sequence':seq,'ordinal':beans[seq]['ordinal'],
                                'first_timestamp_ns':beans[seq]['first_timestamp_ns']})
                        if t.status.value in {'exited','cancelled'}:beans[seq]['terminal']=t.status.value
                        if not beans[seq].get('finalized') and t.history and t.history[-1].frame_index==index and t.status.value=='confirmed':candidates.append(t)
                    self.metrics['measurement_opportunities']+=len(candidates)
                    if pending is None and candidates:
                        t=min(candidates,key=lambda t:(len(samples[t.bean_ref.sequence]),attempts[t.bean_ref.sequence],t.bean_ref.sequence))
                        seq=t.bean_ref.sequence;attempts[seq]+=1;p=predictions.get(seq)
                        crossing_host=(clock.source_to_monotonic_ns(p.crossing_timestamp_ns) if live else source_host+p.crossing_timestamp_ns-timestamp) if p else None
                        deadline=crossing_host-round((bundle.get('valves',{}).get('lead_ms',0)+4)*1e6) if p else None
                        held=frame;pending=(workers.submit(measurer.measure,frame,t.history[-1].detection.centroid_px,index,timestamp),seq,deadline)
                        self.metrics['pending_measurements']=1
                        self.metrics['measurement_attempts']+=1
                        self.metrics['opportunities_skipped_busy']+=max(0,len(candidates)-1)
                    else:self.metrics['opportunities_skipped_busy']+=len(candidates)
                    if not live and not paced:collect(True)  # exhaustive replay is explicitly not a live throughput test
                finally:
                    if held is not frame:source.release_frame(frame)
                index+=1;last_frame_time=tick
            if self.controller:self.controller.feed(False);self.controller.watch_progress=False
            self.state='draining';self.persist();collect(True)
            for seq in beans:finalize(seq)
            for f in image_futures:collect_image(f)
            image_futures=[]
            self.source_stats=source.stereo_statistics()
            journal.flush()
        except Exception as exc:
            self.error=f'{type(exc).__name__}: {exc}'
            if self.controller and self.controller.owned:
                try:self.controller.api('stop')
                except Exception:pass
            try:
                collect(True)
                for seq in beans:finalize(seq)
            except Exception:pass
        finally:
            workers.shutdown(wait=True);images.shutdown(wait=True)
            if measurer:measurer.close()
            if producer:
                try:
                    producer.close()
                    if self.request['image_mode']=='raw' and not self.raw_bundle:raise RuntimeError('RAW recording did not finalize successfully')
                except Exception as exc:self.error=self.error or str(exc)
            if source:
                try:source.close()
                except Exception as exc:self.error=self.error or str(exc)
            if self.controller:
                try:self.controller.close()
                except Exception as exc:self.error=self.error or str(exc)
            self.state='failed' if self.error else 'completed'
            self.metrics['measurements_used_for_decisions']=0
            self.metrics['beans_without_measurements']=self.metrics['tracked_beans']-self.metrics['beans_with_colour']
            try:
                journal.flush();self.final_sequence=self.store.append(self.key,self.state,self.status())
            except Exception as exc:self.state='failed';self.error=self.error or str(exc)
            journal.close();self.persist()
            (self.root/'result.json').write_text(encode(self.status()))
