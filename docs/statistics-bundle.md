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
