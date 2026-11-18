"""Native RAW recorder with every-frame shared previews for the live job runner."""
import argparse
import json
from pathlib import Path
import signal
from beanofastcap.capture import CaptureSession
from beanofastcap.recording_bundle import RecordingPlan


def main():
    p=argparse.ArgumentParser()
    for flag in ('calibration-pack','output','left-shm','right-shm'): p.add_argument('--'+flag,type=Path,required=True)
    p.add_argument('--duration',type=float,required=True);p.add_argument('--fps',type=float,required=True)
    p.add_argument('--controller-url',required=True);p.add_argument('--test-override',action='store_true')
    a=p.parse_args();a.output.mkdir(parents=True,exist_ok=True)
    plan=RecordingPlan(a.output,'machine-job',a.duration,a.fps,5.,'exploratory',True,a.calibration_pack,a.test_override)
    session=CaptureSession(plan,controller_url=a.controller_url,finish_on_stop=True,
        preview_paths={'CamL':a.left_shm,'CamR':a.right_shm},preview_every=1,
        event_callback=lambda e:print(json.dumps(e),flush=True))
    for sig in (signal.SIGINT,signal.SIGTERM): signal.signal(sig,lambda *_:session.stop())
    session.run()

if __name__=='__main__': main()
