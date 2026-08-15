# BeanoFlight

BeanoFlight is the CamL bean-identification, tracking, and flight-prediction
application for the Beano sorting rig. It reads the calibrated lossless video
and exact timestamp manifest produced by BeanoFastCap, converts undistorted
image positions into millimetres using PinkPlane's 9.16 mm hole grid, assigns
stable bean IDs, and predicts where and when each bean will cross a virtual
sorting line.

The first release is deliberately a human-verification tool. It does not
operate valves.

## Review and Free-run modes

**Review** is the default. Analyse a clip once, then move freely through the
frames. Each bean is shown with its ID, lifecycle state, measured path,
predicted path, 95% crossing interval, best virtual gate, probability and
arrival time.

**Free-run** processes the recording sequentially at its native frame rate. It
uses a bounded display queue, keeps full-frame history out of the analysis
result, and reports per-frame processing latency. This is the initial stand-in
for a future live CamL frame source.

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
**Choose 20 empty frames for background**. The guided selector presents frames
chosen randomly within twenty evenly distributed sections of the recording,
followed by replacement passes when a candidate contains foreground. Mark each
candidate empty or containing foreground; only human-confirmed empty frames
enter the temporal median. The selected indices and random seed are retained
in the exported analysis. Median calculation is tiled to avoid making a second
full-size stack of all twenty colour frames. A good background is the most
important prerequisite for useful tuning.

## Input contract

Select any of:

- a FastCap recording directory;
- its `postprocess` directory;
- `CamL-calibrated.mkv` directly.

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
assignment, ID lifecycle, async enrichment and sorting-gate probabilities.
Representative real recordings will be added as regression fixtures after the
first detector-tuning session.
