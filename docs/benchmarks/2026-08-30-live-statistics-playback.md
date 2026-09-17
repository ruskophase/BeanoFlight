# Live Statistics Playback Pressure Test — 2026-08-30

The two-sample live collector was exercised through the complete TensorRT,
Registry, Sorter and virtual-actuator pipeline at a camera-paced 60 FPS. All
available recordings were tested. The three primary pressure recordings ran
for five minutes each; the post-release-point recording ran for one minute and
the original development clip for ten seconds.

Validated settings were a 160 px crop, one low-priority worker, queue capacity
24, first-sample reserve 8, one preparation per frame and a 10 ms admission
budget.

| Recording | Frames | Timeline FPS | Skipped | Inference drops | Analysis mean / p95 (ms) | Confirmed beans | Two samples | One sample | No sample | Max stats queue |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 80 steps/s | 18,001 | 60.000 | 0 | 0 | 6.41 / 12.91 | 5,159 | 2,608 | 1,811 | 740 | 2 |
| 90 steps/s | 18,001 | 60.000 | 0 | 0 | 6.61 / 13.14 | 5,386 | 2,664 | 1,900 | 822 | 2 |
| 110 steps/s | 17,999 | 60.000 | 2 | 0 | 8.32 / 15.52 | 7,260 | 2,074 | 2,901 | 2,285 | 2 |
| Post-shift 80 steps/s | 3,601 | 59.999 | 0 | 0 | 4.69 / 12.83 | 558 | 182 | 188 | 188 | 1 |
| Original 10 s rerun | 601 | 59.997 | 0 | 0 | 4.88 / 9.43 | 156 | 112 | 31 | 13 | 6 |

All inference jobs completed, every run settled, statistics reported no fatal
error and no statistics queue overflow occurred. The 80 and 90 steps/s runs
obtained at least one measurement for 85.7% and 84.7% of confirmed beans. At
110 steps/s that coverage was 68.5%, demonstrating the intended priority
tradeoff: the admission gate discarded optional measurement opportunities
rather than increasing primary-pipeline loss.

The two isolated stale-frame skips in the five-minute 110 steps/s run are a
live-test watch item. They were not accompanied by inference drops, a growing
statistics queue or loss of the 60 FPS source timeline. Short repeated 110
steps/s tests with the final defaults produced zero skips, so this does not
look like sustained overload, but direct-camera validation remains necessary.

## Iteration summary

The first implementation used 320 px crops and two workers. It processed only
1,304 of 1,800 frames in the highest-pressure replay and was rejected. The
following changes were tested incrementally:

- reduced calibrated work to a 160 px crop;
- reused existing CamL/CamR component masks;
- replaced full-array colour operations with selected-pixel conversion and
  lookup tables;
- restricted the default to one background worker and one preparation per
  frame;
- reserved queue capacity for first samples and admitted work only after a
  10 ms sorting-critical budget check;
- pinned optional work to the reserved actuator CPU at lower scheduler
  priority.

A final 1,800-frame 110 steps/s tuning run processed every frame with no crop
drop. The no-statistics comparison averaged 6.47 ms analysis; the selected
collector configuration averaged 8.34 ms in its zero-skip repeat. Increasing
the budget to 12–14 ms or enabling a second worker improved sample coverage but
caused more stale-frame skips, so those settings were rejected.

The first long 90 steps/s validation also exposed a benchmark-only settlement
problem after playback: outcome polling repeatedly deserialized the entire
durable run while Registry writes were settling. Registry now exposes indexed
`run_outcome_counts` queries, the benchmark polls those bounded counters and
performs one detailed sweep only after settlement. The capture from the
interrupted attempt was preserved, and the clean rerun above settled normally.

Machine-readable reports and numerical capture directories are under
`/home/doceave/Beano/diagnostics/live-statistics-playback-20260830`. These are
playback results, not substitutes for the planned live 80/90/100/110 steps/s
acceptance matrix.
