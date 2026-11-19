#!/usr/bin/env python3
"""Offline saved-JPEG replay with an injected writer stall. Never opens cameras."""
import argparse
import hashlib
import importlib
import json
from pathlib import Path
import subprocess
import sys
import time
from urllib.request import urlopen


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--module-directory',type=Path,required=True)
    p.add_argument('--source',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True,help='New directory labelled offline-only')
    p.add_argument('--status-url',default='http://127.0.0.1:8765/api/status')
    p.add_argument('--stall-seconds',type=float,default=1.3)
    args=p.parse_args()
    sys.path.insert(0,str(args.module_directory.resolve()))
    module=importlib.import_module('recorder')  # Recorder only; no CameraStream import.
    def idle():
        with urlopen(args.status_url,timeout=3) as response:status=json.load(response)
        if any(status.get(k) is not False for k in ('camera_running','recording','armed','finishing')):
            raise RuntimeError('Camera must remain idle during offline replay')
    idle()
    source=args.source.resolve()
    original_hash=hashlib.sha256(source.read_bytes()).hexdigest()
    packets=json.loads(subprocess.check_output(['ffprobe','-v','error','-select_streams','v:0',
        '-show_entries','packet=pos,size','-of','json',str(source)],timeout=30))['packets']
    frames=[]
    with source.open('rb') as stream:
        for packet in packets:
            stream.seek(int(packet['pos']));jpeg=stream.read(int(packet['size']))
            assert jpeg.startswith(b'\xff\xd8')
            native=jpeg[:2]+b'\xff\xe1\x00\x04\x00\x00'+jpeg[2:]
            assert module.recording_jpeg(native)==jpeg
            frames.append(native)
    args.output.mkdir(parents=True,exist_ok=False)
    (args.output/'OFFLINE-REPLAY-ONLY.txt').write_text('Historical frames, injected output stall; no new camera or material evidence.\n')
    recorder=module.VideoRecorder(args.output,180)
    class StalledPipe:
        def __init__(self,pipe):self.pipe=pipe;self.count=0
        def write(self,data):
            self.count+=1
            if self.count==1800:time.sleep(args.stall_seconds)
            return self.pipe.write(data)
        def close(self):return self.pipe.close()
        @property
        def closed(self):return self.pipe.closed
    recorder.process.stdin=StalledPipe(recorder.process.stdin)
    started=time.monotonic();next_idle=started+1;submitted=0
    try:
        for i in range(5400):
            delay=started+i/180-time.monotonic()
            if delay>0:time.sleep(delay)
            if time.monotonic()>=next_idle:
                idle();next_idle=time.monotonic()+1
            if not recorder.enqueue(frames[i%len(frames)],round(i/180*1e6),i):
                raise RuntimeError('Replay frame rejected: '+str(recorder.error))
            submitted+=1
    finally:
        recorder.stop();recorder.thread.join(25)
        if recorder.running:
            recorder.process.kill();recorder.thread.join(3)
    report=recorder.diagnostics(history=True)
    probe=json.loads(subprocess.check_output(['ffprobe','-v','error','-select_streams','v:0',
        '-show_entries','stream=nb_frames,duration','-of','json',str(recorder.path)],timeout=15))['streams'][0]
    result={'physical_camera_opened':False,'source_unchanged':original_hash==hashlib.sha256(source.read_bytes()).hexdigest(),
        'submitted':submitted,'avi_frames':int(probe['nb_frames']),'avi_seconds':float(probe['duration']),
        'injected_stall_seconds':args.stall_seconds,'elapsed_seconds':time.monotonic()-started,'recorder':report}
    result['passed']=bool(result['source_unchanged'] and submitted==recorder.frames==result['avi_frames']==5400 and
        not recorder.error and report['finalized'] and report['peak_buffered_frames']>180 and
        report['max_write_ms']>=args.stall_seconds*1000)
    (args.output/'result.json').write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps({k:v for k,v in result.items() if k!='recorder'}),flush=True)
    print(json.dumps({'peak_buffered_frames':report['peak_buffered_frames'],'peak_buffered_bytes':report['peak_buffered_bytes'],
        'max_write_ms':report['max_write_ms'],'diagnostics_path':report['diagnostics_path']}),flush=True)
    if not result['passed']:raise SystemExit(1)


if __name__=='__main__':main()
