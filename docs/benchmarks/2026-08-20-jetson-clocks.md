# Jetson clock-lock benchmark — 2026-08-20

This test isolates the effect of `jetson_clocks` while keeping the Jetson
headless in both profiles. Both tests used `MAXN_SUPER`, commit `b21053c`, the
August 16 recording, background frames 43/222/347, the warmed FP16 shared-layer1
stereo ResNet18 TensorRT engine, two temporal crops per bean, and three complete
601-frame repetitions at a 60 FPS source clock.

The control boot was **Beano Headless** (selector option 3): CPU, GPU and EMC
used their normal dynamic frequency ranges. The treatment boot was **Beano
Headless Performance** (selector option 4): CPUs were fixed at 1.728 GHz, GPU at
1.020 GHz, and EMC at 3.199 GHz.

## Aggregate results

| Metric | Headless dynamic | Headless locked | Locked reduction |
|---|---:|---:|---:|
| Mean frame analysis | 5.847 ms | 3.625 ms | 38.0% |
| Mean of per-run maximum frame analysis | 19.170 ms | 8.900 ms | 53.6% |
| TensorRT service p50 | 7.076 ms | 2.787 ms | 60.6% |
| TensorRT service p95 | 12.329 ms | 4.821 ms | 60.9% |
| Mean of per-run TensorRT maximum | 16.731 ms | 6.731 ms | 59.8% |
| First detection to classification p50 | 46.055 ms | 32.201 ms | 30.1% |
| First detection to classification p95 | 65.491 ms | 48.688 ms | 25.7% |
| Crop capture to classification p50 | 21.828 ms | 12.164 ms | 44.3% |
| Crop capture to classification p95 | 42.155 ms | 19.849 ms | 52.9% |
| Direct acknowledgement p95 | 2.414 ms | 1.043 ms | 56.8% |

Both profiles maintained 59.999 FPS, skipped no frames, dropped no crops,
completed all 939 inference jobs, acknowledged all 471 bean-level direct
deliveries, and produced no late reject decisions. Dynamic clocks produced
1, 7 and 8 sorter deadline fallbacks; locked clocks produced exactly one in
each run. The remaining fallback is structural: that bean has only one valid
crop in this recording. Locked-clock post-run junction temperature was 48.6 C,
versus 46.1 C after the dynamic-clock control, with no observed throttling.

## Reproduction command

Run this once after booting each profile, changing only the output/database
names:

```bash
PYTHONPATH=src .venv/bin/python -m beanoflight.performance_benchmark \
  /home/doceave/Beano/20260816T134132.801241Z-beans \
  --background-frames 43,222,347 \
  --scenarios full --repeats 3 --target-fps 60 \
  --maximum-frames 601 --prebuffer-frames 60 \
  --crops-per-bean 2 --crop-size 224 --crop-processing ml-fast \
  --inference-backend tensorrt \
  --inference-engine \
    artifacts/mock-resnet18/model/mock-stereo-resnet18-fp16.engine \
  --database artifacts/tensorrt-profile.db \
  --output artifacts/tensorrt-profile.json
```

The full per-bean reports remain local ignored artifacts. This summary records
the repeatable comparison without treating generated timing data as source.
