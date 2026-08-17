# Asynchronous system simulation

The simulation is split at the same process boundaries proposed for the live
sorter. Only small immutable records cross BeanRegistry; each selected 300 x
300 crop crosses a separate bounded image socket.

```text
Calibrated MKV/RAW
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
- `beano-registry-monitor` is read-only. It displays current runs and beans,
  makes no-gate and scheduled actuation states explicit, and can pause polling.
- `beano-mock-inferencer` receives lossless BGR crops, optionally shows the
  latest crop and activity log, waits a configurable ResNet-like delay and
  writes a seeded category and confidence to the registry.
- `beano-sorter` is a durable event-journal consumer. It owns classification
  policy and timing. Its actuator loop turns virtual 5 mm gate dots red while
  open and records actual open/close timestamps.
- `beano-flight` owns detection, identity, tracking, prediction and crop
  selection. Its Simulation tab controls rate, preview, decoded prebuffer,
  replay limit, crops per bean, crop size and sockets.
- `beano-simulation` starts any or all of the above as separate child
  processes. Closing the launcher leaves them running; **Stop all** terminates
  only processes that launcher started.
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
choose the decoded prebuffer and replay limit, and run.
Use **Unlimited** to measure the sustainable rate without deliberate sleeps.
A 60 FPS target preserves source time at real-time scale but cannot compensate
for a decoder or analysis path taking longer than 16.67 ms; missed deadlines
and achieved FPS make that visible.

The recommended 60-frame decoded prebuffer occupies about 272 MiB at the
current 1456 x 1088 resolution. Its producer thread continues decoding as the
analysis thread consumes frames. A complete 1,000-frame decoded preload would
occupy roughly 4.43 GiB, so BeanoFlight instead limits the buffer to 120 frames
and separately limits the replay to at most 1,000 frames. Prebuffer time is
reported separately and is excluded from achieved playback FPS.

On 2026-08-17, a 601-frame run of
`20260816T134132.801241Z-beans` on the development Jetson, with live playback
off, a 60 FPS target and one crop per bean, measured 33.2 FPS without buffering
and 37.2 FPS with the default 60-frame buffer. Mean source wait fell from 15.3
ms to 9.8 ms and all 140 crops were delivered without a dispatch drop. This is
a useful improvement, but it also shows that decode and OpenCV contention still
need a separate optimization pass before the complete simulation sustains 60
FPS.

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
  --prebuffer-frames 60 \
  --maximum-frames 1000 \
  --crops-per-bean 1 \
  --target-fps 60
```

Add `--prefer-raw` to exercise BeanoFastCap's RAW calibration path. RAW replay
requires the full `metadata`, `raw` and `calibration` bundle. BeanoFlight first
imports the installed `beanofastcap` package, then supports a sibling
`BeanoFastCap/src` development checkout. The calibrated MKV remains the normal
simulation input because RAW calibration is intentionally more expensive.

## Crop and backpressure contract

Between one and five crops may be requested per public bean; the default is
one. They are taken from successive fully visible confirmed observations. The
bean centroid is at the centre of the configurable square. A crop that would
cross an image boundary is skipped by default rather than silently padding
partial evidence. The image is contiguous `uint8` BGR and is sent as a
multipart message containing finite JSON metadata plus exact raw bytes.

Every crop creates an independent job and classification attached to the same
bean ID. The current sorter intentionally makes its immutable decision from the
first completed classification. Later crop results remain auditable but do not
revise that decision; confidence aggregation is a later policy change.

Before enqueueing, BeanoFlight creates an `InferenceJob` in BeanRegistry. Its
status moves through `submitted`, `accepted` and `completed`, or ends as
`dropped`/`failed`. Both dispatch and inferencer queues are bounded. Frame
tracking never waits for mock inference; overload is visible as a durable job
outcome. Neither the crop nor the source frame is stored in SQLite.

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
