# BeanoFlight

BeanoFlight is the CamL bean-identification, tracking, and flight-prediction
application for the Beano sorting rig. It reads the calibrated lossless video
and exact timestamp manifest produced by BeanoFastCap, converts undistorted
image positions into millimetres using PinkPlane's 9.16 mm hole grid, assigns
stable bean IDs, and predicts where and when each bean will cross a virtual
sorting line.

The current release is deliberately a human-verification and system-simulation
tool. Its ESP32-S2 output is restricted to low-current gate-indicator LEDs; it
does not drive physical valves.

## Review and Simulation modes

**Review** is the default. Analyse a clip once, then move freely through the
frames. Each bean is shown with its ID, lifecycle state, measured path,
predicted path, 95% crossing interval, best virtual gate, probability and
arrival time.

**Simulation** replays the recording sequentially at a selected rate, including
60 FPS or unlimited. Preview can be disabled, the display queue is latest-only,
and full-frame history is never retained. For a complete FastCap bundle, the
default fast path memory-maps synchronized native CamL/CamR RG10 frames,
detects only on CamL's 728 x 544 green plane, point-undistorts centroids for
metric tracking, and processes only selected bean regions. The CamL centroid is
transferred through the PinkPlane homography into CamR, then refined against a
local CamR foreground mask before the two views are cropped. IDs and
trajectories begin on the first CamL detection, while inference waits only
while the segmented bean itself touches either image edge. If
the bean is complete but a centred 224 x 224 sensor crop would cross the frame
edge, the largest complete centred source crop is resized to 224 x 224 without
inventing pixels; the crop size and resize flag are retained for audit. The default
`ml-fast` crop path performs linear sensor-level conversion and bilinear Bayer
demosaic without brightness or colour calibration; the former calibrated sRGB
path remains selectable as a reference. All newly eligible crops from one
source frame are transported as one explicit batch. Source preparation and
detector/tracker latency are reported separately. This is the recorded-source
stand-in for future synchronized live camera sources.

## BeanRegistry

BeanoFlight includes the live system's process-safe bean-state boundary. The
`BeanRegistry` is the single owner of current identities, track revisions,
predictions, ML enrichments and sorting decisions. It can run in-process or as
the `beano-registry` service with SQLite WAL persistence and acknowledged
ZeroMQ command/query calls.

```bash
beano-registry --database /var/lib/beanoflight/beanoflight.db
```

The service also publishes bounded state notifications for monitoring. Every
event has a persistent stream sequence; critical consumers recover through the
`events_since` query rather than assuming publish/subscribe delivery. Frame
images are never written to the registry or its database.

The inferencer sends each completed probability vector over a dedicated,
bounded inference-to-sorter socket before committing it to BeanRegistry. The
message contains job metadata, probabilities and logits only—never an image.
BeanoFlight sends the current tracks, predictions and replay-clock anchor over
a second bounded socket before crop dispatch. BeanoSorter joins those two local
messages, mean-pools temporal evidence and schedules an eligible valve without
a Registry query or write in the normal decision path. The pooled result and
decision are then persisted by its audit worker. Each selected frame contributes
only one logical CamL/CamR inference per bean; the second requested sample
remains a later frame, not a duplicate inference of the same frame pair.

The direct inference notification uses up to three bounded 5 ms acknowledgement
attempts. The sorter acknowledges only after validating the batch and admitting
it to its bounded ingress queue; accepted batch IDs make a retry idempotent.
A negative or missing acknowledgement leaves BeanRegistry as the recovery path.
BeanRegistry still atomically stores every completed job and can
materialize the same immutable
`classification_pooled` result. Registry notifications and the durable event
journal recover a lost direct message. If the later temporal sample would
consume the safe valve window, the sorter uses an auditable first-sample
deadline fallback. Thus neither routine persistence nor polling is present in
the valve-decision path, while restart and delivery recovery remain durable.
The exact pool used by every decision is additionally stored as
`classification_decision_basis`; it remains accurate if Registry independently
finalizes a different pool during a second-result/deadline race.

BeanRegistry holds exclusive OS locks for its SQLite database and both IPC
endpoints. A second instance exits with a clear ownership error instead of
silently replacing a Unix socket while sharing the WAL database.

The simulation adds persistent run sessions, crop-job status, classification,
sorting decisions and virtual actuation results. It still stores no images.
Registry consumers bootstrap from the current journal cursor and current-run
snapshot, so startup work no longer grows with every previous simulation.
Frame commits use bounded undo data rather than copying the complete hot state.
The durable record cache is capped while records remain queryable from SQLite,
and bulk monitor/recovery queries do not populate that cache.

## Asynchronous simulation

Six independently startable GUI/service processes exercise the process
boundaries intended for the machine:

```bash
beano-registry --database ./beanoflight-simulation.db
beano-registry-monitor
beano-actuator
beano-inferencer --backend tensorrt
beano-sorter --actuator ipc:///tmp/beanoflight-actuation-plans.ipc
beano-flight /recordings/example
```

In BeanoFlight, select 3 empty background frames, choose **Simulation**, set
the replay rate, replay prebuffer and crop count, then press **Run**. Leave
**Use memory-mapped RAW fast path** selected for the supplied complete bundle.
Live playback defaults off for throughput. The inferencer treats all bean
crops selected in the same frame as one GPU batch. Its selectable TensorRT
backend runs a real shared-weight ResNet18 tower through `layer1` for each view,
fuses feature maps, and runs the remaining backbone once. The default paired
path transports distinct, synchronized CamL and CamR crops with their frame,
timestamp, projected-centroid and refined-centroid provenance. The mock backend
remains available for deterministic timing/category tests. The sorter waits for
a confirmed trajectory before it makes an immutable decision, then applies its
configurable policy and sends an absolute gate plan to BeanoActuator.
BeanoSorter's GUI is primarily a settings
console; its screen gate mirror is an opt-in diagnostic. Hardware gate state is
visualized by the ESP32 indicator LEDs.
When no individual gate reaches the configured probability threshold, the
sorter may select the strongest adjacent pair if their combined, disjoint
crossing probability qualifies.
Crop previews, activity logs, monitor polling and diagnostic gate animation can be turned
off independently without stopping their worker services.

## ESP32-S2 indicator actuator

`beano-actuator` is the only host process that opens the ESP32 USB device. It
clock-synchronizes with the board, converts approved host-monotonic plans to
absolute board timestamps, and records observed `OPEN`/`CLOSE` events back in
BeanRegistry. The board uses a fixed schedule table and GPTimer tick, supports
overlapping plans through per-gate reference counts, validates CRC32 on every
line, rejects late or excessive pulses, and forces every output low after a
500 ms communications watchdog timeout.

In Performance mode the launcher reserves one CPU for BeanoActuator and one for
BeanoSorter when at least four CPUs are available. Actuator plan admission and
native-USB I/O share one kernel-woken event loop; Registry audit workers run at
lower scheduling priority and use compact acknowledgements so audit work cannot
normally displace a gate deadline.

The 21 active-high indicator outputs are:

```text
G-10..G+3  -> GPIO1..GPIO14
G+4..G+6   -> GPIO16..GPIO18
G+7        -> GPIO21
G+8..G+10  -> GPIO33..GPIO35
```

GPIO15 is reserved for the board status LED. GPIO19 and GPIO20 are reserved for
native USB. Connect each selected GPIO to an LED through its own series resistor
(1 kΩ is a conservative starting value), then connect the LED cathode to GND.
These 3.3 V pins must never drive a solenoid or valve directly.

The firmware and build instructions are in
[`firmware/esp32_s2_actuator`](firmware/esp32_s2_actuator/README.md).

`beano-simulation /recordings/example` is a convenience launcher; each button
still creates an independent operating-system process. The launcher adopts a
healthy registry that is already serving the selected database. It blocks
startup when a different database is using the endpoint, or when an old
registry owns the database or endpoint but is not answering. A responsive older
Registry is labelled as compatibility mode; the inferencer automatically uses
its original atomic batch-completion operation. This remains safe
after closing and reopening the launcher, even though its components
deliberately survive closing the launcher window. Its default **Performance
mode** suppresses registry event printing and starts with monitor polling, crop
and activity views, and gate animation disabled. BeanoFlight opens directly in
Simulation mode with mmap RAW, prebuffering and no live playback. Clear the
launcher checkbox for the fully visual profile, or re-enable an individual
display in its own GUI after startup. For repeatable headless acceptance runs
against already-running services, use:

```bash
beano-system-test /recordings/example \
  --background-frames 43,222,347 \
  --optimized-raw \
  --crop-processing ml-fast \
  --crop-size 224 \
  --prebuffer-frames 60 \
  --maximum-frames 1000 \
  --crops-per-bean 2 \
  --target-fps 60
```

For an isolated multi-run performance test, `beano-performance-benchmark`
starts a private Registry, Inferencer and Sorter, keeps them alive across
all repetitions, and writes one JSON report containing stage timings and
outcomes. See [simulation.md](docs/simulation.md#repeatable-performance-matrix)
for the reference command and current results. Pass
`--no-adaptive-edge-resize` to reproduce the BeanoFlight simulation
checkbox-off crop policy while holding the other benchmark settings fixed.
The benchmark also applies process-wide CPU roles, gives direct inference
evidence a dedicated IPC I/O context and defers BeanoSorter's cyclic garbage
collection until shutdown.
Pass `--esp32-actuator` to include the connected indicator board and require
observed hardware cycles in the acceptance result.

See [simulation.md](docs/simulation.md) for the data flow, crop policy, clock
contract and operating sequence.

## OpenCV pipeline inspector

Turn on **Inspect frozen frame step-by-step** in the **Pipeline steps** tab.
The current video frame is frozen and the Previous/Next buttons walk through:

1. input frame;
2. grayscale conversion;
3. Gaussian blur;
4. absolute background difference;
5. fixed threshold;
6. morphological close;
7. morphological open;
8. optional dilation;
9. all connected components;
10. accepted and rejected bean shapes.

Every view has a display-only caption containing the stage name and the exact
settings used for that image. Detector values use small Spinbox increments.
Changing a value invalidates the existing track results, because IDs must never
silently combine observations made with different settings.

Detection defaults to half-resolution processing for the 60 FPS budget. Area,
width, height, centroids and bounding boxes are still reported in native image
pixels, and inspector masks are enlarged to the native display size without
smoothing so individual processing pixels remain visible.

Enter three zero-based indices in **Background frames** and choose **Build
entered frames**, or use **Choose 3 empty frames for background**. The field
defaults to `43,222,347`; Simulation validates and uses those entered frames
even if a single temporary Review background was selected earlier. The guided
selector presents frames
chosen randomly within three evenly distributed sections of the recording,
followed by replacement passes when a candidate contains foreground. Mark each
candidate empty or containing foreground; only human-confirmed empty frames
enter the temporal median. The dialog also accepts upper- or lower-case `U`/`Y`
to use a frame and `N` to skip it. The selected indices and random seed are
retained in the exported analysis. Median calculation is tiled to avoid making
a second full-size stack of all three colour frames. A good background is the
most important prerequisite for useful tuning.

## Input contract

Select any of:

- a FastCap recording directory;
- its `postprocess` directory;
- `CamL-calibrated.mkv` directly.

**Open RAW bundle** remains available for slow, fully calibrated Review and
pipeline comparison. Normal Review should use the calibrated MKV. Simulation's
separate **memory-mapped RAW fast path** requires the complete bundle, avoids
full-frame demosaic/colour/remap, and is the preferred performance input.

When `pairs.csv` is beside the video, video frame `n` uses that row's
`left_timestamp_ns`. If the sidecar is absent or invalid, BeanoFlight clearly
labels the session as using nominal FPS timestamps.

BeanoFlight finds `homography.json` beside the video. The mapping must be a
PinkPlane v2 mapping in the `undistorted` coordinate domain and must contain
the recorded CamL hole centres. The generated metric coordinate system has its
origin at the CamL image centre, +x to the right, and +y downwards.

## Install and run

Python 3.10 or newer is required. On Debian, Ubuntu, or Jetson Linux, install
Tk if it is not already present:

```bash
sudo apt install python3-tk
```

Then install in an isolated environment. On a Jetson, use
`--system-site-packages` when the system OpenCV build should be retained.

```bash
cd BeanoFlight
python3 -m venv --system-site-packages .venv
source .venv/bin/activate
python -m pip install -e .
beano-flight /recordings/example
```

An explicit calibration can be supplied when the video was copied away from
its FastCap bundle:

```bash
beano-flight CamL-calibrated.mkv --homography /calibration/homography.json
```

The 9.16 mm pitch and 30 mm sorting-line offset are editable CLI inputs:

```bash
beano-flight recording/ --hole-pitch-mm 9.16 --sorting-offset-mm 30
```

## Current tracking model

- Static-background OpenCV segmentation with no adaptive exposure or image
  normalization.
- Native-pixel component defaults of at least 2,000 px area, 50 px width and
  50 px height.
- Exact optimal assignment for the expected small set of at most ten beans.
- Four-state Kalman model: horizontal/vertical position and velocity.
- Known gravity as the vertical control input, with process noise for drag,
  rotation and imperfect centroid measurements.
- Immediate run-scoped ID, followed by tentative, confirmed, occluded, exited
  or cancelled lifecycle states.
- Configurable 50-pixel left and right birth margins. A first bounding box
  touching a margin is explicitly edge-rejected and receives no ID; a valid
  existing track is not renamed if it later enters a margin.
- Twenty-one virtual 5 mm gates, with `G0` centred on image `x = 0`.
- Gaussian crossing probabilities and a default 35% virtual actuation
  threshold, including an optional adjacent-gate combined-probability policy.

See [architecture.md](docs/architecture.md),
[bean-registry.md](docs/bean-registry.md),
[metric-calibration.md](docs/metric-calibration.md), and
[operations.md](docs/operations.md) for the implementation and review
contracts.

## Development

```bash
make test
PYTHONPATH=src python3 -m compileall -q src
```

The test suite covers metric fitting, every diagnostic detector stage, guided
background sampling, edge rejection and suppression, exact timestamps, global
assignment, ID lifecycle, registry revision/idempotency rules, SQLite recovery,
byte-exact crop IPC, bounded frame prefetch, mmap RAW lifecycle, deferred crop
calibration, async mock inference,
sorting decisions and virtual gate actuation.
It also covers the persisted per-bean timing ledger, live event-driven sorter,
adaptive edge crop and shadow notice calculations.
Representative real recordings will be added as regression fixtures after the
first detector-tuning session.
