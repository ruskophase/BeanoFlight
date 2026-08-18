# Asynchronous system simulation

The simulation is split at the same process boundaries proposed for the live
sorter. Only small immutable records cross BeanRegistry; each selected 300 x
300 crop crosses a separate bounded image socket.

```text
CamL mmap RG10 (fast) or calibrated MKV (fallback)
       |
       v
 BeanoFlight replay -- track/prediction --> BeanRegistry --> SQLite WAL
       |                                      ^     ^            |
       +-- BGR crop --> Mock Inferencer ------+     |            +--> Monitor
                                                    |
                           BeanoSorter decision ----+
                                 |
                                 +--> virtual gate cycle --> BeanRegistry
```

## Programs

- `beano-registry` is the only database writer. Its console output suppresses
  high-volume `track.updated` messages unless `--log-track-updates` is used.
- `beano-registry-monitor` is read-only. It displays the latest run and keeps
  that snapshot current from coalesced events instead of repeatedly loading all
  historical beans. It makes no-gate and scheduled actuation states explicit,
  and can pause polling.
- `beano-mock-inferencer` receives lossless BGR crops, optionally shows the
  latest crop and activity log, waits a configurable ResNet-like delay and
  writes a seeded category and confidence to the registry.
- `beano-sorter` is a durable event-journal consumer. It owns classification
  policy and timing. Recovery starts from the current cursor plus live/latest
  run snapshots; it never replays unrelated historical runs. Its actuator loop
  turns virtual 5 mm gate dots red while open and records actual open/close
  timestamps.
- `beano-flight` owns detection, identity, tracking, prediction and crop
  selection. Its Simulation tab controls input path, rate, preview, prebuffer,
  replay limit, crops per bean, crop size and sockets.
- `beano-simulation` starts any or all of the above as separate child
  processes. Closing the launcher leaves them running; **Stop all** terminates
  only processes that launcher started. Its selected-by-default **Performance
  mode** suppresses registry event printing, pauses Registry Monitor polling,
  disables crop/activity rendering and gate animation, and opens BeanoFlight
  in Simulation mode with the existing fast replay defaults. These are initial
  GUI states, so a diagnostic display can be re-enabled when needed without
  restarting its worker service.
- `beano-system-test` is the non-GUI replay driver for repeatable acceptance
  tests against already-running registry, inferencer and sorter processes.

The virtual actuator is a separate worker loop inside BeanoSorter for this
simulation. A real valve driver should later be a distinct least-privilege
process which consumes approved actuation plans and returns hardware results.

## Start and run

Install BeanoFlight, open five terminals, and start the first four components:

```bash
beano-registry --database ./beanoflight-simulation.db
beano-registry-monitor
beano-mock-inferencer
beano-sorter
```

Then start the replay GUI:

```bash
beano-flight /path/to/fastcap-bundle
```

Choose exactly 3 human-confirmed empty frames. In **Simulation**, choose a
target rate, leave live playback off for the fairest throughput measurement,
choose the replay prebuffer and replay limit, and run.
Keep **Use memory-mapped RAW fast path** enabled for a complete FastCap bundle.
Use **Unlimited** to measure the sustainable rate without deliberate sleeps.
A 60 FPS target preserves source time at real-time scale but cannot compensate
for a decoder or analysis path taking longer than 16.67 ms; missed deadlines
and achieved FPS make that visible.

For the normal 60 FPS test, start through `beano-simulation`, leave
**Performance mode** selected, and press **Start all**. Clear it only when the
visual diagnostics are more important than a representative throughput
measurement. If the launcher adopts an already-running registry, that external
process retains the console logging mode with which it was originally started.

On the optimized path, each slot owns a read-only 3.06 MiB RAW mapping and a
0.38 MiB 728 x 544 green image. A 60-frame buffer therefore allocates about
22.7 MiB of compact image arrays; up to about 183 MiB of mapped file pages may
also reside in the kernel's reclaimable page cache. The calibrated-MKV fallback
still needs about 272 MiB for 60 decoded BGR frames. BeanoFlight limits either
buffer to 120 frames and replay to 1,000 frames. Prebuffer time is reported
separately and excluded from achieved playback FPS.

On 2026-08-18, the isolated performance matrix replayed all 601 frames of
`20260816T134132.801241Z-beans` five times per scenario while keeping one
Registry process alive throughout. Core replay averaged 59.999 FPS (59.996
minimum). The full Mock Inferencer and Sorter pipeline averaged 59.996 FPS
(59.986 minimum), completed 141 crops and 141 decisions on every repetition,
and dropped no jobs. Mean full-pipeline frame work was 12.53 ms, of which 11.70
ms was analysis: detection was 4.76 ms, tracking 1.07 ms, prediction 0.41 ms,
Registry IPC/commit 5.22 ms and durable crop selection/enqueue 0.68 ms. The
highest sampled temperature was 46.2 C, with no evidence of throttling.
Individual timing spikes can still exceed 16.67 ms, so the deadline counter
remains useful even when later frames catch up and aggregate throughput reaches
60 FPS.

A separate startup check used an 81.7 MiB registry copy containing 1,143 beans
and 14,208 events. The current-run snapshot took 53 ms once; subsequent idle
journal polls averaged 0.20 ms and did not scan SQLite history.

The final replay summary is also persisted under the run session's
`settings.performance` field. It includes achieved FPS; source, detection,
coordinate mapping, tracking, prediction, Registry, crop-dispatch and total
frame timings; SQLite and Registry-operation timings; queue delay; process
CPU/RSS; available clock/temperature samples; prebuffer timing; deadline misses
and crop counters. This allows a slow run to be diagnosed after its GUI closes.

For the supplied exploratory recording, one useful confirmed-empty candidate
set is:

```text
43, 222, 347
```

These are zero-based indices and should still be visually checked. Test-override
text is burned into its calibrated MKV; it is static background, but pixels
hidden beneath the overlay cannot be recovered.

The matching headless invocation is:

```bash
beano-system-test /path/to/20260816T134132.801241Z-beans \
  --background-frames 43,222,347 \
  --optimized-raw \
  --prebuffer-frames 60 \
  --maximum-frames 1000 \
  --crops-per-bean 1 \
  --target-fps 60
```

## Repeatable performance matrix

Use the benchmark command when comparing code or machine configuration. It
creates private IPC endpoints and a temporary database, starts independent
Registry, Mock Inferencer and Sorter processes, and deliberately reuses them
across repetitions so cumulative slowdown is visible. `core` disables crop
dispatch; `full` exercises crop transfer, mock classification and sorting.

```bash
beano-performance-benchmark \
  /path/to/20260816T134132.801241Z-beans \
  --background-frames 43,222,347 \
  --scenarios core,full \
  --repeats 5 \
  --target-fps 60 \
  --maximum-frames 601 \
  --prebuffer-frames 60 \
  --crops-per-bean 1 \
  --output ./performance-report.json
```

The report preserves every run summary and adds per-scenario distributions.
`passed` requires both `all_outcomes_complete` and
`all_within_one_fps_of_target`; a one-FPS tolerance allows normal
operating-system scheduling jitter without hiding sustained under-performance.

`--optimized-raw` selects the performance path. It mmaps native RG10, derives
an sRGB-encoded green plane directly from the two green Bayer sites, and avoids
full-frame colour conversion. `--prefer-raw` instead exercises BeanoFastCap's
full-frame calibrated RAW reference path and is intentionally much slower.
Both require the complete `metadata`, `raw` and `calibration` bundle.

## Crop and backpressure contract

Between one and five crops may be requested per public bean; the default is
one. They are taken from successive fully visible confirmed observations. The
bean centroid is at the centre of the configurable square. A crop that would
cross an image boundary is skipped by default rather than silently padding
partial evidence. The frame thread copies only the selected Bayer ROI (about
0.18 MiB for a 300-pixel crop plus its demosaic halo), then releases the full
RAW mapping. Dark/flat/defect correction, demosaic, white balance, colour
matrix and sRGB encoding run on the crop-dispatch thread. The resulting image
is contiguous `uint8` BGR and is sent as a multipart message containing finite
JSON metadata plus byte-exact image data.

Every crop creates an independent job and classification attached to the same
bean ID. The current sorter intentionally makes its immutable decision from the
first completed classification. Later crop results remain auditable but do not
revise that decision; confidence aggregation is a later policy change.

Before enqueueing, BeanoFlight creates a durable `InferenceJob` through a
compact revision-only Registry acknowledgement. The dispatch worker
colour-processes and sends the crop, then advances the job through `accepted`
and `completed` (or `dropped`/`failed`). Both dispatch and inferencer queues are
bounded; a full local queue is recorded synchronously as a dropped job. Frame
tracking never waits for colour processing or mock inference, and a process
failure cannot leave an unregistered crop in the queue. Neither the crop nor
the source frame is stored in SQLite.

## Replay clock

FastCap timestamps remain the authoritative source-time domain. Each run
session stores a source timestamp and local monotonic-clock anchor. Inferencer
and sorter convert their local completion/scheduling times through that anchor,
so pause/resume does not make a bean arrive while replay is paused. Target FPS
scales source time; unlimited replay performs decisions and virtual cycles
immediately while retaining the original predicted crossing timestamp for
audit.

The current IPC transport is for trusted local processes. Its default Unix
domain sockets have no authentication or encryption and must not be exposed to
an untrusted network.

## Scope of this simulator

The mock category is deterministic for a seed and job ID but has no connection
to crop content. Gate timing is a software simulation, not authority to drive
hardware. Frame drops, delayed messages, corrupt results and process restarts
are planned fault-injection/analysis work after this baseline.
