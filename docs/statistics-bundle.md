# Offline Statistics Bundles

`beano-statistics` is the exhaustive prototype for calibrated stereo colour,
projected size, ellipse geometry and approximate-volume analysis. It consumes
the seekable CamL/CamR RAW recording, uses the production detector and tracker,
and processes a maximum of three complete observations from the top, middle
and bottom thirds of each confirmed track.

The default background frames `2,8,14` are the empty frames confirmed for the
August 2026 five-minute recordings. Pass a different explicit set when using
another recording.

```bash
beano-statistics /path/to/recording \
  --background-frames 2,8,14 \
  --output-root /path/to/statistics-bundles
```

Multiple recording paths may be supplied. The command writes one atomic,
self-contained directory per recording. Existing bundles are protected unless
`--overwrite` is explicit.

Add `--save-bean-photos` to retain a calibrated CamL/CamR JPEG pair for each
successfully sampled, confirmed bean. The representative sample prefers the
middle field-of-view band. `beans.csv` and `beans.jsonl` link to the photographs;
`photos/index.json` records their bean IDs, sample indices and exact source
frame indices/timestamps. The bundle manifest hashes the photographs too.
Open `dashboard/index.html` for the full interactive statistics dashboard:
appearance, size/volume, stereo agreement, batch timeline and capture health.
Every chart selection opens bean tiles with both calibrated photographs and
small CamL/CamR mean-colour swatches. The enlarged view and review collection
allow selected pairs to be inspected and exported with their measurements and
photo paths. It works directly from disk without a web server or network
connection, and uses the offline bundle's own bean IDs rather than attempting
to match a separate replay run.
This is offline export from RAW, not extra disk I/O on the live sorting path.
Beans with no valid sample have no fabricated photo. JPEGs are review images;
numerical statistics are computed from the calibrated arrays before encoding.

For an older photo-bearing bundle that lacks this dashboard, run
`beano-statistics-dashboard /path/to/statistics-bundle`. This installs or
upgrades the dashboard assets and refreshes the existing bundle manifest; it does not
reprocess RAW frames or replace the measurements or photographs.

## Neighbouring-bean segmentation diagnostics

Run `beano-segmentation-study /path/to/recording` to replay the statistics
observations for beans #120, #1197, #1403 and #1550, a deterministic set of
measurement outliers and ordinary controls. It compares blur, threshold and
closing settings against the original RAW masks, saves per-view photo/mask
montages and writes an aggregate report under
`postprocess/segmentation-study-v2/`. It does not change the source recording,
statistics bundle or live detector. Existing study outputs are protected.

The report distinguishes *separating two shapes* from knowing which separated
shape belongs to a tracked bean. No setting should be promoted solely because
it reduces apparent size outliers; identity, control-bean stability and
cross-frame/cross-camera consistency require separate validation. Actual
occlusion remains an unresolved case rather than an assumed successful split.
The production and offline detector closing-kernel default is now 3×3, following
review of the thirteen additional tracks in the 2026-09-16 recording. To
compare against the previous behavior, use `--detector-close-kernel 5` and a
separate `--output-root`; the manifest records the chosen kernel.
Use `beano-segmentation-compare /path/to/original-bundle
/path/to/close3-bundle /path/to/new-comparison-dir` to compare complete
replays without equating split tracks to original bean IDs.
Use `beano-segmentation-review /path/to/original-bundle
/path/to/close3-bundle /path/to/new-review-dir` to export the experimental
tracks left unmatched by a one-to-one temporal/position match as paired
CamL/CamR photographs. The HTML gallery and CSV/JSON indices include source
frame IDs and nearby original tracks. These are candidates for human review,
not confirmed newly detected physical beans.
`beano-segmentation-synthetic /path/to/recording` adds a known-answer stress
test: isolated recorded RAW silhouettes are composited at controlled,
non-overlapping gaps onto the recorded empty background. The synthetic score
tests separation and mask overlap; it cannot validate natural occlusion or
track identity by itself.

## Contents

- `beans.csv` and `beans.jsonl` contain robust per-track medians, track/sample
  coverage, two-view appearance, shape, size proxies and appearance-outlier
  scores.
- `observations.csv` and `observations.jsonl` retain the individual calibrated
  stereo samples from which each bean row was derived.
- `charts/` contains batch-level lightness/colour, size/volume and stereo-view
  agreement plots.
- `outliers/contact-sheet.png` ranks unusual calibrated colours for human
  review. These are review candidates, not automatic reject labels.
- `summary.json` reports coverage, distributions, failures and separate timing
  for calibrated RAW materialization and the bounded two-view feature kernel.
  Kernel wall time (the live latency cost) and summed two-camera CPU time are
  separate. Per-active-frame workload also exposes bursts of several newly
  eligible beans that a live scheduler must bound.
- `manifest.json` freezes the recording, calibration, homography, settings,
  software version, field definitions and content hashes.

## Interpretation limits

Colour is measured only inside an eroded foreground silhouette after the full
Camera-Tuner dark/flat/defect, white-balance, colour-matrix and sRGB path. CIE
Lab is the preferred batch descriptor; camera RGB is retained for inspection.
The outlier score is a robust within-recording distance and therefore says
“unusual in this batch,” not “metal” or “foreign object.” Labelled nuts, bolts
and odd beans are still required to calibrate a classifier or decision rule.

Area, perimeter and ellipse values describe projected silhouettes. The local
homography Jacobian provides approximate mm² at each centroid. The two cameras
view approximately opposing faces rather than orthogonal axes, so neither
independently measures hidden thickness. The reported sphere and rotational-
ellipsoid values are deliberately named volume proxies and must not be treated
as physical volume until checked against objects with measured dimensions or
displacement.

## Relationship to the live 60 FPS pipeline

The feature extractor works on a bounded calibrated crop and mask; it never
requires a full-frame colour conversion. Bundle timing separates the feature
kernel from calibrated RAW materialization and also reports their combined
per-job wall time. This distinction matters: normal live classification uses
the faster uncalibrated `ml-fast` crop, so it does **not** already pay the full
calibrated-colour materialization cost.

The live collector is now implemented as a separate bounded, lower-priority
worker. It targets exactly two numerical stereo measurements per confirmed
bean, falls back to one and never attempts a third. It reuses segmentation
work, performs no live charting and retains no images. Optional work is
deferred or lost under pressure rather than blocking the sorting path. Three
calibrated samples remain useful in this exhaustive offline prototype but
exceed the measured sustained capacity of the highest-pressure recording.

See [Live Statistics Capture](live-statistics-capture.md) for the runtime and
file contract, and the [2026-08-30 playback pressure test](benchmarks/2026-08-30-live-statistics-playback.md)
for the five-minute validation results. Direct-camera 80/90/100/110 steps/s
acceptance is recorded in the [2026-08-31 live matrix](benchmarks/2026-08-31-inference-attached-live-matrix.md).

## Bundles from live numerical captures

`beano-live-statistics-bundle` consumes a completed, hash-valid inference-
attached capture. It aggregates the retained one or two measurements per
confirmed bean, applies the global camera dark/white-balance/colour-matrix
calibration to the masked means, derives pixel-domain ellipse and volume
proxies, and writes labelled, grid-lined charts. It does not require or create
bean images or a contact sheet.

```bash
beano-live-statistics-bundle /path/to/live-capture \
  --output-root /path/to/statistics-bundles
```

For one capture, `--output /exact/bundle/path` selects the exact destination.

The generated `dark-bean-candidates.png` shows the batch lightness histogram,
the one-sided `mean L* - 2 sample SD` threshold and the approximate colours of
flagged beans in the Lab a*/b* plane. `dark-bean-candidates.csv` provides the
corresponding bean rows. This is a review screen, not a sorting rule: under a
roughly normal healthy-bean distribution, approximately 2.3% of healthy beans
are inherently below a one-sided two-SD threshold. A production threshold must
therefore be calibrated against labelled known-good and deliberately dark
beans, preferably alongside the included median/MAD robust reference.

Live inference uses the linear `ml-fast` image path. Because only numerical
masked means are retained, the offline bundle can apply the global linear
calibration and sRGB transfer but cannot reconstruct spatial flat-field/defect
correction or the exact mean of a per-pixel nonlinear Lab transform. Fields
and chart titles identify these values as approximate.
