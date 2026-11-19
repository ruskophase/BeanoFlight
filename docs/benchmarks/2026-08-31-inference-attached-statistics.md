# Inference-attached Statistics Playback — 2026-08-31

The final inference-attached collector was replayed through the genuine stereo
TensorRT, Registry and Sorter pipeline at a camera-paced 60 FPS. Every retained
recording was tested using two requested inference/statistics samples per bean.

| Recording | Processed frames | Timeline FPS | Skipped | Analysis mean / p95 ms | Inference drops | Confirmed beans | Two samples | One sample | Zero samples | CamL recoveries | Max queue |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 80 steps/s | 18,000 | 60.000 | 1 | 5.38 / 11.00 | 0 | 5,157 | 5,152 | 5 | 0 | 1 | 4 |
| 90 steps/s | 18,001 | 60.000 | 0 | 5.58 / 11.45 | 0 | 5,386 | 5,383 | 3 | 0 | 1 | 4 |
| 110 steps/s, vibration 27% | 17,989 | 60.000 | 12 | 7.10 / 15.02 | 0 | 7,259 | 7,253 | 6 | 0 | 1 | 5 |
| Release shifted 5 mm toward CamR | 3,601 | 60.000 | 0 | 4.33 / 11.67 | 0 | 558 | 557 | 1 | 0 | 0 | 4 |
| Original 10-second clip | 601 | 59.998 | 0 | 5.21 / 10.67 | 0 | 156 | 156 | 0 | 0 | 0 | 5 |

Coverage was 100% in all five recordings: 18,516 confirmed beans and zero
without a record. All 37,017 inference crops completed with no crop or
inference-job drop. The highest-pressure five-minute 110-step/s run held 59.96
processed FPS and a 60.000 FPS source timeline while its statistics queue
peaked at 5 of 24.

Three long recordings each contained one confirmed bean which never produced
a stereo inference sample. The zero-sample safety net recovered a CamL record
for each. In the 80 and 90-step/s cases the bean remained truncated at the top
CamL edge for its entire two-hit track, so only native area/geometry was
retained and `feature_enrichment_valid` was false. The 110-step/s case was
fully visible in CamL and retained masked colour and silhouette primitives.

## Iterations

The initial independent calibrated-crop worker maintained inference throughput
but measured only 68.5% of confirmed beans at 110 steps/s. The accepted design
made these changes:

- attached compact detector/refinement evidence to inference jobs;
- reused the exact materialized TensorRT BGR crops after successful delivery;
- replaced live Lab/HSV, percentiles and ellipse fitting with BGR aggregates
  and silhouette moments for offline derivation;
- removed histogram work and ran the two view kernels sequentially on the
  low-priority CPU;
- gave first observations priority and retained a geometry baseline on feature
  failure or primary queue saturation;
- cached one CamL candidate only for confirmed beans with no attachable stereo
  sample, and finalized it only after inference drained;
- made zero confirmed beans without statistics a benchmark failure criterion.

A controlled 601-frame 110-step/s comparison without statistics processed 601
frames at 60.00 FPS with zero skips. The final attached version typically
processed 600 at 59.90 FPS with one skip; its attachment averaged roughly
0.37 ms per frame and feature-kernel median was roughly 1.2 ms per stereo
observation. The full five-minute 110-step/s run had no inference loss.

Machine-readable reports and capture directories are under
`/home/doceave/Beano/diagnostics/inference-attached-statistics-20260830`.
These playback results authorize the planned attended live 80/90/100/110
steps/s matrix; they do not replace direct-camera validation.
