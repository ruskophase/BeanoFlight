# Live Statistics Capture

The live pipeline captures the numerical evidence needed for later batch
colour, silhouette, apparent-size and volume-proxy analysis. It deliberately
does not generate charts during a run and does not retain bean images.

Capture is enabled by default for direct-camera `beano-system-test` and
`beano-performance-benchmark --live` runs. A capture is written beneath
`<state-root>/live-statistics-captures` unless `--statistics-output-root`
selects another local directory. Use `--no-statistics` only for a controlled
baseline or fault-isolation run.

## Capture contract

Each confirmed bean has a target of two stereo observations and a hard maximum
of two. A bean that leaves the field of view after one successful observation
is explicitly retained as a single-sample fallback. Under pressure, optional
statistics work can produce no sample; it must never delay detection,
inference, sorting or actuation to obtain one.

The capture directory contains:

- `observations.jsonl`: one flat numerical row per successful stereo
  observation, including calibrated CamL and CamR colour, brightness,
  silhouette, ellipse, projected area and paired volume proxies;
- `beans.jsonl`: one row per confirmed bean, with a sample count, fallback
  flag, sampled field-of-view bands and collection failures;
- `capture.json`: settings, calibration/background provenance, aggregate
  coverage, performance timings, failure examples and content hashes.

Tracking can begin collection while a track is tentative so that a short-lived
bean does not lose its first useful view. Consequently, `observations.jsonl`
can contain a small number of rows for tracks which never confirm. Offline
batch processing must join observations to bean IDs in `beans.jsonl` and omit
unmatched rows. `capture.json` separately reports confirmed observations,
total observations and unconfirmed observations.

## Pressure isolation

The validated defaults are one worker, a 160-pixel calibrated crop, a
24-element priority queue with eight slots reserved for first observations,
at most one ROI preparation per camera frame and a 10 ms admission budget. The
frame thread never waits for the worker or for disk I/O.

First observations take queue priority over second observations. Work is
offered only after sorting-critical frame work and only while that work remains
inside the admission budget. The worker runs at a lower scheduler priority on
the CPU reserved for the actuator, where the actuator can pre-empt it; it is
kept away from the general acquisition/detection/inference CPU set. Statistics
exceptions are counted and isolated from sorting.

The implementation also reuses the CamL connected-component labels and the
CamR refinement component already created by stereo localization. It converts
only selected crop pixels to Lab/HSV and uses lookup tables for calibrated
linear-to-sRGB conversion. No full-frame colour image is created for this
feature.

The 160-pixel crop is an intentional throughput tradeoff. A silhouette touching
that crop edge is rejected rather than measured partially. Those failures and
pressure deferrals lower statistics coverage but leave the primary pipeline
unaffected.

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
appropriate for each 80, 90, 100 and 110 steps/s acceptance point. Preserve
the generated capture and report for offline bundle generation and A/B review.
Stop bean flow as soon as `LIVE_CAPTURE_COMPLETE` appears; outcome settlement
and detailed Registry audit collection continue after the camera window and do
not require further beans.

`--live-test-override` may be used only for an attended workflow test after a
real witness has been measured but failed its production limits. It never
changes the witness result. FastCap emits the measured errors, and the run
profile, statistics provenance and benchmark report are permanently marked
`classification: test`, `test_override: true` and `production_valid: false`.
Camera structure, live controller state and stream-integrity checks remain
mandatory.

After every run verify at minimum:

- 60 FPS source timeline and bounded frame age;
- no inference crop drops or failed inference jobs;
- statistics `fatal_error` is empty and queue depth remains bounded;
- first/second/single/zero-sample coverage and the reasons for missed samples;
- both motors are stopped and the controller lease is safely released.

The statistics fields are not yet sorting inputs. A separate offline command
will aggregate the two observations (or the single fallback) into the charts
and batch summaries currently produced by `beano-statistics`.
