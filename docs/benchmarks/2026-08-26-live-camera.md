# Direct-camera 60 FPS acceptance — 2026-08-26

The final production test consumed synchronized CamL/CamR RG10 frames directly
from headless BeanoFastCap. It did not record RAW files and did not use video
playback. FastCap selected the newest hash-valid Camera Tuner product,
`beano-56cdb225d170`, and required a fresh bound witness before opening the
streams.

The test used 15 initial empty frame pairs, followed by continuous bean flow.
No empty tail was required. The bounded runner right-censored the one active
track at frame 601 and the sorter produced its terminal no-action decision.

The complete report is
`/home/doceave/Beano/diagnostics/live-camera-acceptance-final-20260826.json`.

| Measurement | Result |
|---|---:|
| Live synchronized pairs processed | 601 |
| Elapsed time | 9.999 s |
| Achieved rate | 60.105 FPS |
| CamL/CamR sequence drops | 0 / 0 |
| Unmatched CamL/CamR frames | 0 / 0 |
| Maximum stereo timestamp skew | 1 us |
| Mean / maximum capture-to-analysis age | 8.780 / 16.408 ms |
| Mean / maximum frame analysis | 4.672 / 16.065 ms |
| Conservative work-budget misses | 1 |
| Bean records / decisions | 205 / 205 |
| TensorRT jobs completed | 409 / 409 |
| Complete stereo evidence pairs | 409 / 409 |
| Crop or delivery drops | 0 |
| Complete two-sample pools | 204 |
| Deadline single-sample fallbacks | 1 |
| Suspected fragmented identities | 0 |
| Late decisions | 0 |
| Right-censored boundary tracks | 1 |

Thirteen early crop attempts were deferred because the segmented CamR bean was
still clipped at the top edge; subsequent complete stereo crops yielded all 409
inference jobs. Stereo refinement distance was 10.893 px mean, 21.343 px p95,
and 38.057 px maximum, within the configured 64 px bound. The aggregate
full-pipeline result passed. The single conservative work-budget miss did not
overwrite or skip a camera frame, did not increase maximum frame age beyond one
60 FPS interval, and caused no late sorting decision.

Command:

```bash
PYTHONPATH=src .venv/bin/python -m beanoflight.performance_benchmark \
  --live --scenarios full --repeats 1 \
  --target-fps 60 --maximum-frames 601 --prebuffer-frames 0 \
  --crops-per-bean 2 --inference-backend tensorrt \
  --background-samples 15 --bean-start-delay 30 \
  --state-root /home/doceave/Beano \
  --output /home/doceave/Beano/diagnostics/live-camera-acceptance-final-20260826.json
```

## Deleted corrected-geometry replay corpus

The production recording captured after the live acceptance was:

`/home/doceave/Beano/20260826T115835.452374Z-beans-corrected-geometry-60s`

It is bound to Camera Tuner bundle `beano-56cdb225d170` and contains 3,601
synchronized pairs over 59.998 seconds. Both cameras reported zero sequence
drops and the final pairing report contains no unmatched frames. Pair skew was
0 us median and 1 us maximum.

A 10 Hz sample through the production RAW detector verified an empty prefix.
The first accepted bean appeared at 39.898 seconds, after the physical flow
startup delay, and bean detections continued through the final frame. Thus the
recording contains approximately 20.1 seconds of falling-bean content rather
than a full minute of bean flow. Accepted component areas ranged from 2,320 to
13,460 px (5th percentile 3,231 px), so the existing 2,000 px minimum-area
setting remains supported by this corpus. The validation report is
`/home/doceave/Beano/diagnostics/corrected-geometry-60s-recording-validation.json`.

The recording itself was intentionally deleted on 2026-08-26 during storage
cleanup. The compact validation report above remains as evidence, but the path
is no longer a replayable source. The canonical retained replay corpus is
`/home/doceave/Beano/20260816T134132.801241Z-beans`.

## Automated-flow live run

A subsequent attended test used controller-driven flow after the 15-pair empty
background: conveyor reverse at 80 steps/s and vibration at 25% duty. The full
report is
`/home/doceave/Beano/diagnostics/live-camera-automated-flow-20260826.json`.

The run processed 601 live pairs at 59.997 FPS and produced 183 bean records,
366/366 completed TensorRT jobs, 366/366 complete stereo evidence pairs, and
183 terminal decisions. No jobs or crops were dropped, no identities were
suspected of fragmentation, no decisions were late, and all 13 virtual
actuations succeeded. The aggregate benchmark passed.

The bounded latest-frame reader skipped one synchronized camera pair during a
24.088 ms capture-to-analysis transient; CamL and CamR each reported one
sequence gap, with one unmatched CamL candidate. Maximum delivered pair skew
remained 1 us. This was a transport skip, not an incomplete bean outcome. The
controller fail-safe issued `STOPALL` after the measured window because the
external orchestration heartbeat was not refreshed during conveyor
deceleration. Both motors stopped and the driver disabled. The operating
procedure now explicitly maintains heartbeats through the complete eight-second
deceleration interval.
