# Inference-attached live statistics matrix

Date: 2026-08-31

This attended matrix exercised real synchronized camera input at 60 fps with
the inference-attached statistics implementation from commit `9d5748e`. The
fresh measured witness was explicitly accepted by the operator under
`TEST OVERRIDE`, so the runs are non-production test evidence.

The conveyor ran in reverse with 10 steps/s² acceleration. Vibration was 25%
at 80 and 90 steps/s and 27% at 100 and 110 steps/s. Each initial measurement
window contained 3,601 delivered camera pairs. Motors started only after the
15-pair empty background and stopped at `LIVE_CAPTURE_COMPLETE`; the conveyor
was supervised to zero speed and driver-disabled after every run.

| Conveyor | Vibration | FPS | Source FPS | Skipped | Mean analysis | Confirmed beans | Two samples | One sample | No samples | Jobs | Drops/failures | Queue max | Formal result |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|:---:|
| 80 steps/s | 25% | 59.968 | 60.018 | 3 | 3.624 ms | 633 | 633 | 0 | 0 | 1,267 | 0 / 0 | 4 | FAIL |
| 90 steps/s | 25% | 59.926 | 60.009 | 5 | 3.545 ms | 610 | 609 | 1 | 0 | 1,221 | 0 / 0 | 4 | FAIL |
| 100 steps/s | 27% | 59.871 | 60.004 | 8 | 3.733 ms | 645 | 644 | 1 | 0 | 1,291 | 0 / 0 | 4 | FAIL |
| 110 steps/s | 27% | 60.001 | 60.018 | 1 | 3.542 ms | 616 | 616 | 0 | 0 | 1,232 | 0 / 0 | 4 | PASS |

Across the matrix, 2,504/2,504 confirmed beans had at least one statistics
observation and 2,502/2,504 had two observations. The intended one-observation
fallback covered the other two beans. All 5,011 inference jobs completed with
no dropped or failed jobs, and statistics queue depth never exceeded 4 of 24.

This materially improved on the 2026-08-30 independent low-priority collector.
That matrix left 49/459, 112/662, 182/796 and 170/1,042 confirmed beans without
observations at 80, 90, 100 and the completed 110 steps/s retry, respectively.

## Five-minute 110 steps/s follow-up

The follow-up used 110 steps/s reverse, 27% vibration and 18,001 delivered
camera pairs:

- 6,916 confirmed beans in 302.16 seconds;
- 22.89 beans/s over the complete interval and 25.16 beans/s after first bean
  arrival;
- first confirmed bean at frame 1,509, or 25.15 seconds into processing;
- camera-minute bean totals of 805, 1,728, 1,494, 1,484 and 1,405;
- 6,913 beans with two samples, three with one and zero without samples;
- 13,830 completed inference/statistics jobs with zero drops or failures;
- queue depth 5 of 24;
- source timeline 60.004 fps, achieved processing 59.574 fps and 130 stale
  camera pairs skipped;
- mean analysis 5.447 ms and maximum frame age 35.665 ms;
- maximum observed temperature approximately 54.4 °C.

The one-minute runs did not measure a full minute of bean flow. First arrival
occurred at 24.82, 26.38, 32.38 and 26.18 seconds for 80, 90, 100 and 110
steps/s, leaving only 35.20, 33.63, 27.63 and 33.83 seconds of detected flow.
Their raw confirmed-bean totals therefore cannot establish proportional
throughput. A controlled speed-response experiment should use equal vibration,
equal starting bean mass and a discarded warm-up interval.

## Bounded-shutdown outcome race

The 80, 90, 100 and five-minute 110 reports retained formal failures when one
or more final boundary tracks did not receive a terminal decision. All
confirmed beans retained statistics and the decision counts equalled or
exceeded confirmed-bean counts, but unsettled runs intentionally omitted
detailed per-bean outcome records.

The evidence points to an ordering race rather than sustained pressure. A
cancellation context can reach the Sorter before final inference evidence adds
the bean to the recovery watch. Later evidence can then retain an earlier
active context while the already-filtered cancellation is no longer available.
The one-minute 110 run happened to settle fully. This race should be fixed and
regression-tested before bounded reports are treated as formal acceptance
evidence.

The full local evidence is under
`diagnostics/inference-attached-live-statistics-20260831`, outside the source
repository. JSONL row counts were reconciled against every capture report.
After the final run, the motors were verified off, conveyor speed was zero,
the driver was disabled, the controller lease was released, and `STOPALL`
disabled trigger and strobe.
