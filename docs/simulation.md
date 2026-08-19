# Asynchronous system simulation

The simulation is split at the same process boundaries proposed for the live
sorter. Only small immutable records cross BeanRegistry; each selected 224 x
224 crop crosses a separate bounded image socket.

```text
CamL mmap RG10 (fast) or calibrated MKV (fallback)
       |
       v
 BeanoFlight replay -- async audit --------> BeanRegistry --> SQLite WAL
       |                 |                       ^  ^             |
       |                 +-- direct context ---> |  |             +--> Monitor
       +-- BGR crop --> Mock Inferencer ==ACK==> Sorter
                                                |  |
                       decision audit -----------+  |
                                                v  |
                                      BeanoActuator ==USB==> ESP32 LEDs
                                                +-- observed cycle audit
```

## Programs

- `beano-registry` is the only database writer. Its console output suppresses
  high-volume `track.updated` messages unless `--log-track-updates` is used.
- `beano-registry-monitor` is read-only. It displays the latest run and keeps
  that snapshot current from coalesced events instead of repeatedly loading all
  historical beans. It makes no-gate and scheduled actuation states explicit,
  and can pause polling.
- `beano-mock-inferencer` receives lossless BGR crops, optionally shows the
  latest crop and activity log, preserves each source frame's new-bean group as
  one simulated GPU batch, and writes a seeded category and confidence to the
  registry. Its default timing model charges for two views through a shared
  ResNet18 backbone and a feature-fusion head, even though only CamL is
  transported in this release.
- `beano-sorter` joins direct classification evidence with BeanoFlight's direct
  track/prediction context. Registry notifications and the SQLite journal recover
  a dropped message, sequence gap or restart. It owns classification policy and
  timing. Recovery starts from the current cursor plus live/latest run snapshots;
  it never replays unrelated historical runs. The GUI is a policy/settings
  console. Its screen gate mirror and activity log are optional diagnostics.
  An approved absolute valve plan is sent to BeanoActuator before its audit
  write, so SQLite latency cannot postpone the gate deadline. If no individual gate
  qualifies, an optional adjacent pair can qualify on combined probability.
- `beano-actuator` owns the ESP32-S2 USB connection. It clock-synchronizes the
  board, admits acknowledged plans, converts host timestamps to the board clock,
  and persists observed hardware cycles. The board's fixed GPTimer schedule and
  watchdog own the final LED edge timing.
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

BeanoSorter retains its virtual actuator only as a test fallback when no
actuation endpoint is configured. The launcher always selects the separate
BeanoActuator/ESP32 path.

## Start and run

Install BeanoFlight, open six terminals, and start the first five components:

```bash
beano-registry --database ./beanoflight-simulation.db
beano-registry-monitor
beano-actuator
beano-sorter --actuator ipc:///tmp/beanoflight-actuation-plans.ipc
beano-mock-inferencer
```

Then start the replay GUI:

```bash
beano-flight /path/to/fastcap-bundle
```

Choose exactly 3 human-confirmed empty frames. In **Simulation**, choose a
target rate, leave live playback off for the fairest throughput measurement,
choose the replay prebuffer and replay limit, and run.
Keep **Use memory-mapped RAW fast path** enabled for a complete FastCap bundle.
Leave **Resize smaller complete crops near frame edge** enabled to classify a
fully visible bean at its earliest observation. Clear it to wait until a full
centred crop of the requested size fits inside the sensor frame.
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

On 2026-08-19, two final event-driven acceptance runs replayed all 601 frames of
`20260816T134132.801241Z-beans` with background frames 43, 222 and 347. Both
achieved 59.999 FPS, identified 158 beans, completed 157 jobs and dropped none.
Forty-five jobs used a complete smaller source crop resized to 224 x 224; this
raised same-frame crop availability from the earlier 47/154 to 89/157. Mean
first-detection-to-classification time was 57.70-61.23 ms. The stable mock
population contained 49 reject decisions: the runs completed 34 and 38 bean
actuation cycles respectively, with every completed cycle covering its
predicted crossing. The latest run used the combined-probability fallback for
16 decisions and opened two gates for 22 decisions. Eleven decisions remained
too late and none failed solely for lack of gate probability. Their lateness
was p50 11.95 ms and p95 83.96 ms; that p95 is one tail case in a small set.
Latest-run shadow analysis predicts that 5, 10, 20 and 50 ms of additional
notice would recover 4, 5, 8 and 9 of those 11 respectively. Mean analysis was
10.94-11.11 ms. Individual timing spikes still exceed 16.67 ms, so the deadline
counter remains useful even when later frames catch up and aggregate throughput
reaches 60 FPS.

After moving Registry frame/result traffic to atomic batches, deferring sorter
audit writes, and moving WAL checkpoints off the writer, two fixed-seed
acceptance runs each processed all 601 frames at 59.999 FPS with no stale skips
or crop drops. Mean analysis fell to 6.21-6.24 ms and the largest SQLite event
save was 6.01 ms. All 157 jobs completed in both runs; 40 and 45 reject
decisions actuated successfully, while 9 and 4 were too late. Eleven
reject-category results per run were explicitly recorded as below the 0.75
confidence threshold. Late-decision p95 was 18.80-26.70 ms. This is a two-run
confirmation, not yet the replacement five-run baseline.

The 2026-08-19 ESP32-S2 integration acceptance run used the same recording,
frames 43/222/347, 224 x 224 crops and 60-frame prebuffer. It processed all 601
frames at 59.999 FPS, completed all 157 inference jobs, and produced 52 reject
actuations. The board acknowledged and completed all 52 cycles with no failed
or late sorting decisions. Observed GPIO open/close error was 0.052 ms at p50,
0.091 ms at p95 and below 0.10 ms maximum. This is a single hardware acceptance
run, not a statistical timing qualification.

A separate startup check used an 81.7 MiB registry copy containing 1,143 beans
and 14,208 events. The current-run snapshot took 53 ms once; subsequent idle
journal polls averaged 0.20 ms and did not scan SQLite history.

The final replay summary is also persisted under the run session's
`settings.performance` field. It includes achieved FPS; source, detection,
coordinate mapping, tracking, prediction, Registry, crop-dispatch and total
frame timings; SQLite and Registry-operation timings; queue delay; process
CPU/RSS; available clock/temperature samples; prebuffer timing; deadline misses
and crop counters. It separates processed FPS from source-timeline FPS and
records stale-frame count plus mean/max frame age. Full-pipeline benchmark
output additionally contains a
per-bean timing ledger, p50/p95 lateness, equivalent sorting-line extension and
shadow recovery counts for 5-50 ms of extra notice. This allows a slow run to
be diagnosed after its GUI closes.

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
  --crop-processing ml-fast \
  --crop-size 224 \
  --prebuffer-frames 60 \
  --maximum-frames 1000 \
  --crops-per-bean 1 \
  --target-fps 60
```

Stale-frame dropping is enabled by default with a 30 ms age ceiling, matching
the bounded-latency behaviour expected from a live camera. Use
`--keep-stale-frames` only for exhaustive offline analysis, or adjust the ceiling
with `--maximum-frame-age-ms`. The BeanoFlight GUI exposes the same policy.

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
  --crop-processing ml-fast \
  --crop-size 224 \
  --crops-per-bean 1 \
  --output ./performance-report.json
```

Add `--esp32-actuator` for an isolated hardware-backed run. By default it opens
the development board's stable `/dev/serial/by-path` name; override that with
`--esp32-port`. The resulting JSON records the chosen port, hardware-cycle
failures and observed open/close timing distributions.

The report preserves every run summary and adds per-scenario distributions.
`passed` requires both `all_outcomes_complete` and
`all_within_one_fps_of_target`; a one-FPS tolerance allows normal
operating-system scheduling jitter without hiding sustained under-performance.

`--optimized-raw` selects the performance path. It mmaps native RG10, derives
an sRGB-encoded green plane directly from the two green Bayer sites, and avoids
full-frame colour conversion. Its default `--crop-processing ml-fast` profile
linearly maps sensor values to 8-bit BGR with bilinear demosaic and deliberately
omits dark/flat/defect correction, white balance, colour matrix and sRGB
transfer. `--crop-processing calibrated` retains the former crop reference
path for accuracy and timing comparisons. `--prefer-raw` instead exercises
BeanoFastCap's full-frame calibrated RAW reference path and is intentionally
much slower. These RAW modes require the complete `metadata`, `raw` and
`calibration` bundle.

## Crop and backpressure contract

Between one and five crops may be requested per public bean; the default is
one at 224 x 224. Identity and motion tracking still start at the initial
tentative detection. When the bean bounding box is fully inside the sensor but
the centred 224 x 224 window is not, the default adaptive policy takes the
largest complete centred square containing the bean and resizes it to the
requested model size. It never pads with unseen top-edge evidence. If the
actual bean bounding box touches an image edge, crop selection always waits for
a later observation. Clear the BeanoFlight checkbox—or pass
`--no-adaptive-edge-resize` to either headless runner—to require the complete
requested-size window as well.
The frame thread copies only the selected Bayer ROI (about 0.10 MiB including
its demosaic halo), then releases the full RAW mapping. With the default
`ml-fast` profile, the dispatch thread performs only sensor-level linear
conversion and bilinear demosaic before producing contiguous `uint8` BGR.
This representation is intended for a model trained on the same pipeline, not
for display. The selectable `calibrated` reference additionally applies the
frozen photometric corrections and sRGB encoding.

Every crop creates an independent `classification_evidence` job attached to the
same bean ID and ensemble ID. It records the full class order, probability
vector and logits. The default BeanoFlight setting requests two temporal
samples from two selected source frames. There is only one logical stereo
inference for a bean on each frame pair; the simulator does not run the same
pair twice. BeanoSorter mean-pools both vectors locally on its dedicated result
path and only that immutable `classification_pooled` result drives sorting.
BeanRegistry persists the evidence and independently guarantees the same pool
for recovery and audit.

Waiting is bounded by the valve deadline. BeanoSorter subtracts open lead,
minimum notice and the configurable **Pool reserve ms** from the predicted
crossing. It drains evidence already queued at that cutoff before using a
one-sample deadline fallback. Every decision stores a separate
`classification_decision_basis` containing the exact pool it used, even if the
Registry's canonical pool concurrently completes with two samples. Registry and
timing-ledger output therefore distinguish complete pools from fallbacks without
the former audit race.

All crops selected in one source frame are enqueued and transported atomically
as an explicit frame batch. The dispatch worker overlaps RAW colour processing
with the Registry round trip. On crop frames it first commits the crop-owning
tracks and jobs, sends the crop batch, then persists the other tracks; unrelated
history therefore does not consume the current crop deadline. Queued track-only
frames may share a transaction while retaining per-bean timestamp order.

The mock GPU uses a physical crossing estimate to choose among frame batches
that are already waiting; it never merges frames or delays a frame to make a
larger batch. GPU execution and CPU-side result publication use separate
workers, allowing the next simulated TensorRT batch to start while the previous
result is sent to the sorter and commits. The direct batch carries no pixels and
uses a bounded non-blocking queue; the Registry completion remains its durable
fallback. Both queues remain bounded. Jobs finish as `completed`,
`dropped`, or `failed`; neither crops nor source frames are stored in SQLite.

## Source-frame stereo inference model

The mock inferencer incurs one batch-level delay for exactly the bean group
detected in one source frame. It neither waits for later frames nor merges
unrelated detections. The conservative default TensorRT FP16 curve is expressed
in input images: 2 images/1 bean is 15 ms, 4/2 is 18 ms, 8/4 is 23 ms, 16/8 is
32 ms and 20/10 is 38 ms. Intermediate sizes are linearly interpolated. Normal
batches receive up to 15% seeded jitter; one percent also receive a seeded
15--30 ms tail penalty.

The GUI reports batch count and size, queue and service latency, rare tails,
deadline misses and drops. Each evidence result stores the same fields in its
Registry enrichment, including a source-frame batch ID and explicit
`stereo_pair_complete: false` marker.

This is currently a compute-load model, not fabricated stereo evidence: the
transport still contains one physical CamL crop per job. When CamR crop pairing
is added, the same logical job boundary can carry the two synchronized views;
the classifier should pass both views through a shared-weight backbone and
fuse their feature vectors rather than combine trained network weights.

## Replay clock

FastCap timestamps remain the authoritative source-time domain. Each run
session stores a source timestamp and local monotonic-clock anchor. The sorter
converts each approved source-time valve window to local monotonic deadlines
once, avoiding Registry polling in the hot actuator loop. Inferencer completion
times are converted through the same anchor, and pause/resume does not make a
bean arrive while replay is paused. Target FPS scales source time; unlimited
replay performs decisions and virtual cycles immediately while retaining the
original predicted crossing timestamp for audit.

The current IPC transport is for trusted local processes. Its default Unix
domain sockets have no authentication or encryption and must not be exposed to
an untrusted network.

## Scope of this simulator

The mock category is deterministic for a seed, camera and bean sequence, so a
new run ID or earlier crop frame does not silently change the test population.
It has no connection to crop content. The ESP32 outputs are indicator-only in
this release: they validate schedule delivery and edge timing, but are not
authority to drive valves. Frame drops, delayed messages, corrupt results and process restarts
are planned fault-injection/analysis work after this baseline.
