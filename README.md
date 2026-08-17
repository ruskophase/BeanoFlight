# BeanoFlight

BeanoFlight is the CamL bean-identification, tracking, and flight-prediction
application for the Beano sorting rig. It reads the calibrated lossless video
and exact timestamp manifest produced by BeanoFastCap, converts undistorted
image positions into millimetres using PinkPlane's 9.16 mm hole grid, assigns
stable bean IDs, and predicts where and when each bean will cross a virtual
sorting line.

The first release is deliberately a human-verification and software-simulation
tool. It does not operate physical valves.

## Review and Simulation modes

**Review** is the default. Analyse a clip once, then move freely through the
frames. Each bean is shown with its ID, lifecycle state, measured path,
predicted path, 95% crossing interval, best virtual gate, probability and
arrival time.

**Simulation** replays the recording sequentially at a selected rate, including
60 FPS or unlimited. Preview can be disabled, the display queue is latest-only,
and full-frame history is never retained. For a complete FastCap bundle, the
default fast path memory-maps native CamL RG10, detects on a 728 x 544 green
plane, point-undistorts centroids for metric tracking, and colour-processes only
selected bean crops. Every eligible bean is sent as a configurable 300 x 300
BGR8 crop over a bounded ZeroMQ path. Source preparation and detector/tracker
latency are reported separately. This is the recorded-source stand-in for a
future live CamL frame source.

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

BeanRegistry holds exclusive OS locks for its SQLite database and both IPC
endpoints. A second instance exits with a clear ownership error instead of
silently replacing a Unix socket while sharing the WAL database.

The simulation adds persistent run sessions, crop-job status, classification,
sorting decisions and virtual actuation results. It still stores no images.
Registry consumers bootstrap from the current journal cursor and current-run
snapshot, so startup work no longer grows with every previous simulation.

## Asynchronous simulation

Five independently startable GUI/service processes exercise the process
boundaries intended for the machine:

```bash
beano-registry --database ./beanoflight-simulation.db
beano-registry-monitor
beano-mock-inferencer
beano-sorter
beano-flight /recordings/example
```

In BeanoFlight, select 3 empty background frames, choose **Simulation**, set
the replay rate, replay prebuffer and crop count, then press **Run**. Leave
**Use memory-mapped RAW fast path** selected for the supplied complete bundle.
Live playback defaults off for throughput. The mock inferencer delays each crop
and adds a deterministic random category/confidence. The sorter applies its
configurable policy and shows virtual 5 mm gates in black or red while active.
Crop previews, activity logs, monitor polling and gate animation can be turned
off independently without stopping their worker services.

`beano-simulation /recordings/example` is a convenience launcher; each button
still creates an independent operating-system process. The launcher adopts a
healthy registry that is already serving the selected database. It blocks
startup when a different database is using the endpoint, or when an old
registry owns the database or endpoint but is not answering. This remains safe
after closing and reopening the launcher, even though its components
deliberately survive closing the launcher window. For repeatable headless
acceptance runs against already-running services, use:

```bash
beano-system-test /recordings/example \
  --background-frames 43,222,347 \
  --optimized-raw \
  --prebuffer-frames 60 \
  --maximum-frames 1000 \
  --crops-per-bean 1 \
  --target-fps 60
```

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

Use **Use current frame as background** on a genuinely empty frame, or choose
**Choose 3 empty frames for background**. The guided selector presents frames
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
  threshold.

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
Representative real recordings will be added as regression fixtures after the
first detector-tuning session.
