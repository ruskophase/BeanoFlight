# Live Statistics Capture

The live pipeline retains the numerical evidence needed for later batch
colour, silhouette, apparent-size and volume-proxy analysis. It does not make
charts during a run and does not retain bean images.

Capture is enabled by default for direct-camera `beano-system-test` and
`beano-performance-benchmark --live` runs. A capture is written beneath
`<state-root>/live-statistics-captures` unless `--statistics-output-root`
selects another local directory. Use `--no-statistics` only for a controlled
baseline or fault-isolation run.

## Capture contract

Each confirmed bean targets two observations and has a hard maximum of two.
The first observation is mandatory; the second is desirable and can be
omitted under pressure or when the bean has only one valid inference crop.

The normal path is inference-attached. The frame thread adds the existing
CamL detector component, cached CamR refinement component and native geometry
to the first two selected inference jobs. After inference has successfully
received the materialized stereo crop, the statistics worker measures those
same image arrays. There is no second RAW crop, homography projection, CamR
foreground search or demosaic on the normal path.

If a confirmed bean never receives stereo inference evidence, one compact
deferred CamL candidate is retained. After inference drains at batch shutdown,
that candidate is used only if the bean still has zero observations. A fully
measurable candidate records CamL colour and silhouette primitives. If the bean
never becomes fully visible even in CamL, a baseline area/geometry row is still
written with `feature_enrichment_valid: false`; missing CamR and colour values
remain explicitly absent rather than being fabricated.

The capture directory contains:

- `observations.jsonl`: one numerical row per observation;
- `beans.jsonl`: one row per confirmed bean, including sample count, sampled
  field-of-view bands and collection failures;
- `capture.json`: settings, calibration/background provenance, coverage,
  timings, failure examples and content hashes.

Tracking can attach the first inference sample while a track is tentative.
Consequently, `observations.jsonl` can contain a small number of rows for tracks
which never confirm. Offline processing must join observations to bean IDs in
`beans.jsonl` and omit unmatched rows. `capture.json` reports confirmed, total
and unconfirmed observation counts separately.

## Stored primitives

For each available view, the online worker stores:

- native detection/refinement pixel areas, centroids, bounding boxes and
  stereo synchronization/refinement metadata;
- masked BGR pixel count, sum, sum of squares, mean and standard deviation;
- silhouette pixel count, crop-edge flag, bounding box, centroid, variance and
  covariance spatial moments;
- source/crop scale, capture path, inference job ID and enrichment-validity
  flags.

The sums, sums of squares and counts preserve the information needed to combine
two samples without retaining pixels. Colour normalization, white-balance or
camera-to-camera correction, perceptual colour spaces, ellipse axes, metric
area conversion and volume proxies are intentionally deferred to the future
offline batch tool. This keeps expensive or policy-dependent transformations
out of the sorting path.

The capture schema is `beanoflight-live-statistics-capture/v2`; observations
use `beanoflight-live-statistics-observation/v2`. Consumers must use
`measurement_view_count`, `camr_measurement_available` and
`feature_enrichment_valid` before using paired or colour-derived features.

## Pressure isolation

The validated defaults are one worker and a 24-element priority queue with
eight positions protected from second observations. The common frame-thread
attachment averages about 0.4 ms on the Jetson and only copies compact masks
and scalar evidence. It never waits for the statistics worker or disk I/O.

The worker runs at lower scheduler priority on CPU5, where latency-critical
work can pre-empt it, and is kept away from the general acquisition, detection
and inference CPU set. Its two small view kernels run sequentially on that CPU
to avoid scheduler hand-offs. First observations have queue priority; a full
primary queue falls back synchronously to the already-known geometry row.
Second observations may be discarded, but the first-record invariant remains
observable in the bean ledger and benchmark acceptance result.

Online work is limited to masked BGR aggregates and spatial moments. It does
not calculate Lab/HSV values, percentiles, ellipse fits, calibrated metric
scales or volume proxies. Exceptions are isolated from inference and sorting;
an enrichment exception writes the baseline row and records its reason.

## Live acceptance command

The normal attended command is:

```bash
beano-performance-benchmark \
  --live --scenarios full --repeats 1 \
  --target-fps 60 --maximum-frames 3601 --prebuffer-frames 0 \
  --crops-per-bean 2 --inference-backend tensorrt \
  --state-root /home/doceave/Beano \
  --output ./live-performance-report.json
```

The newest complete, hash-valid Camera Tuner bundle is selected automatically.
Keep the field empty until `LIVE_BACKGROUND_READY`, then follow the attended
motor-control protocol in [operations.md](operations.md). A one-minute run is
appropriate for each 80, 90, 100 and 110 steps/s acceptance point. Stop bean
flow when `LIVE_CAPTURE_COMPLETE` appears; Registry settlement and statistics
shutdown can finish without further beans.

After every run verify:

- the source timeline remains 60 FPS with bounded frame age;
- inference has no crop drops or failed jobs;
- statistics `fatal_error` is empty and queue depth remains bounded;
- `beans_without_samples` is zero and
  `all_confirmed_beans_have_statistics` is true;
- CamL-only and baseline-only fallbacks are reviewed rather than treated as
  paired measurements;
- both motors are stopped and the controller lease is safely released.

The statistics fields are not sorting inputs. The separate offline batch tool
will aggregate two observations, fall back to one, perform colour/size
derivations and generate the charts currently prototyped by
`beano-statistics`.
