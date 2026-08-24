# Review workflow

1. Open a FastCap recording folder or its `CamL-calibrated.mkv`.
2. Confirm that the status line says `exact FastCap timestamps`.
3. Confirm that the metric calibration has loaded and reports its RMS.
4. Enter three zero-based indices in **Background frames** and choose **Build
   entered frames**. The current default is `43,222,347`. Alternatively choose
   **Choose 3 empty frames for background**. Mark each candidate `Empty` or `Contains
   foreground`; only accepted frames enter the median. `U`/`Y` means use and
   `N` means do not use, in either upper or lower case.
5. Enable **Inspect frozen frame step-by-step**.
6. Move through the ten OpenCV stages. The caption records every relevant
   setting used for the displayed result.
7. Adjust one setting at a time and press **Apply settings**. Kernel Spinboxes
   move in odd increments so OpenCV always receives a centred morphology
   kernel.
8. Pay particular attention to the threshold mask and final accepted/rejected
   component view. A good mask covers the bean but does not join neighbouring
   beans or retain background texture.
9. Turn off the inspector and choose **Analyse clip**.
10. Step through the completed result. Check for false detections, missed
    beans, ID switches, implausible velocity and excessively broad gate
    probabilities.
11. Export the compact JSON analysis when the result should be compared or
    discussed.

## Side-margin review

The default left and right new-track margins are 50 pixels and are editable in
**Tracks & gates**. Their shaded red regions are display-only. A detection whose
first bounding box overlaps either region is labelled `EDGE-REJECTED` and is
recorded without a bean ID. This is preferable to calling it occluded: its
centroid, size, crop and appearance are already incomplete at first sight.

An existing track is allowed to enter a margin without changing ID. Review
those cases carefully because a partial edge measurement can still increase
trajectory uncertainty.

## Existing FastCap overlays

Current FastCap derivatives contain a 30-pixel information bar at the bottom.
Test-override media also contains a 38-pixel warning at the top. These are
static and therefore normally become part of the background, but bean pixels
behind the burned overlays cannot be recovered. Track prediction bridges the
small lower blind strip. A future clean machine-analysis derivative is
recommended.

## Interpreting probability

The thick interval on the sorting line is approximately the predicted 95%
horizontal interval of the bean centre. Each gate percentage is the Gaussian
probability that the centre crosses within that 5 mm interval. Gate
probabilities may sum to less than 100% when the distribution extends beyond
the displayed virtual gate bank.

These probabilities do not yet model bean width, nozzle plume width, measured
SX11F-LH valve latency or a validated category-specific sorting policy. The
ESP32 outputs are suitable for the current LED timing demonstration only; they
must not drive valves in version 0.1.

## Registry service check

The live registry can be started independently of the review GUI:

```bash
beano-registry --database ./beanoflight.db
```

Its default local endpoints are
`ipc:///tmp/beanoflight-registry-commands.ipc` and
`ipc:///tmp/beanoflight-registry-events.ipc`. Production systemd deployment
should place these sockets in a service-owned runtime directory and the SQLite
database on local persistent storage. Do not place a WAL database on a network
filesystem.

## Multi-process simulation check

Start `beano-simulation` for a convenience control panel, or run the registry,
monitor, actuator, sorter, inferencer and BeanoFlight in separate terminals.
Select
the same command and crop endpoints in each GUI. Start the registry first;
BeanoFlight refuses to begin Simulation if its ping is not acknowledged.
The launcher reuses a healthy existing registry only when it reports the
selected database. If the endpoint is serving a different database, or if the
endpoint or selected database is owned but unresponsive, **Start all** stops
immediately with a recovery message instead of creating a second database
writer.

The launcher labels an already-running Registry without current capability
metadata as **legacy; compatibility mode**. The inferencer then uses the
older atomic result-batch operation. Restarting the Registry enables the newer
compact acknowledgement, but version skew no longer turns classifications into
failed jobs.

When using `beano-simulation`, leave **Performance mode** selected for a
repeatable throughput run. It starts newly launched components with registry
event printing, Registry Monitor polling, crop and activity rendering, and
virtual-gate animation disabled. The processing services continue to run; only
their optional displays are paused. An already-running registry adopted by the
launcher keeps its original logging setting.

On machines with at least four available CPUs, Performance mode also reserves
the highest-numbered CPU for BeanoActuator and the next for BeanoSorter; replay,
Registry and inference use the remaining CPUs. On the six-core development
Jetson this is CPUs 0-3 for general work, CPU4 for Sorter and CPU5 for Actuator.

Disable preview for throughput checks. BeanoFlight reports source-read and
analysis time separately. For a complete recording bundle, keep the
memory-mapped RAW fast path enabled: it detects on the native green plane and
colour-processes only asynchronous inference crops. The FFV1 route remains a
useful fallback and review reference, but software decoding can be its limiting
stage. The default 60-frame prebuffer starts before the replay clock and then
overlaps RAW preparation or video decoding with analysis. Set the maximum replay
length between 1 and 1,000 frames. Inspect the registry monitor for a complete
chain of `inference.submitted`, `inference.completed`, `sorting.decision`, and,
where policy selects a gate, `sorting.actuated`. Crop receipt timing remains in
the completed job ledger without a separate hot-path `accepted` write.

The Actuation column reads `Awaiting`, `Not required`, `Scheduled`, `OK` or
`FAIL`; a blank result is no longer used for the normal no-gate case. Turn off
live playback, inference crop display, activity logs, registry live updates and
virtual-gate animation when measuring throughput. These controls affect GUI
work only and do not stop the corresponding service.

The headless `beano-system-test` requires exactly 3 explicit, visually
confirmed empty-frame indices and prints a JSON performance summary on
completion. Add `--optimized-raw` to use the same fast path as the GUI. Add
`--no-adaptive-edge-resize` for a controlled comparison in which inference is
deferred until the full requested-size crop fits within the frame. The
deadline-aware emergency microbatch path is enabled by default; add
`--no-emergency-microbatch` for a controlled A/B run which retains each busy
frame as one inference batch.

Every completed session stores its achieved FPS, source-read and analysis
stage distributions, Registry/SQLite operation timings, crop-dispatch queue
timings, process resource samples, prebuffer time, missed deadlines and crop
counts in the session `settings.performance` object. These values remain
available after the BeanoFlight window closes.

**Start all** launches BeanoActuator, then BeanoSorter, before Beano Inferencer so
the dedicated plan and inference-evidence receivers own their IPC endpoints
before the producers connect. Starting the components individually should follow
the same order. Evidence submission returns after a bounded in-memory enqueue;
a dedicated transport worker owns acknowledgement and retry, so a slow ACK does
not hold the inference result thread. Exhausted retries are not fatal:
BeanoFlight and the inferencer still commit authoritative state to BeanRegistry,
and the sorter recovers it from Registry notifications after a short preference
interval. Each crop also carries its exact track/prediction context through the
inferencer to the sorter, avoiding a cross-socket join on the critical path; the
standalone context stream remains available for later trajectory refinements.
When a busy frame's second-sample batch approaches its actuation deadline,
BeanoFlight sends the one or two earliest co-deadline beans first and then sends
the remainder. This is deliberately limited to frames containing at least five
beans so normal GPU batching is preserved. BeanoSorter also discards superseded
standalone context items while draining a burst; embedded evidence remains
authoritative for the time-critical decision.
Performance mode applies CPU affinity to every extant native thread, including
threads created during CUDA, OpenCV and ZeroMQ import—not only the process
leader. Direct inference evidence uses dedicated ZeroMQ I/O contexts so bulk
Registry, crop and trajectory traffic cannot block its native I/O queue.
BeanoSorter also defers cyclic garbage collection while its latency-critical
service is running; normal reference counting remains active and cyclic
collection is restored when the service stops.
The Beano Inferencer and BeanoSorter status panels show direct sent/received and
context cache counts; the timing ledger labels trajectory delivery as
`embedded-evidence`, `direct`, or `registry`.

The Beano Inferencer also displays pending Registry audits and retry counts.
Closing its window or choosing **Stop all** drains accepted inference results
before the process exits. Transient Registry transport interruptions and delayed
job registration are retried off the actuation-critical path. When the launcher
owns BeanRegistry, it keeps Registry running until the worker processes finish
their orderly shutdown.

For real-time runs, inspect `source_timeline_fps`, `frames_skipped`, and
`frame_age_ms` together. Processed FPS intentionally falls when an old replay
frame is discarded; source-timeline FPS shows whether the input clock was kept,
while frame-age telemetry proves that latency remained bounded.

An isolated full-pipeline benchmark also reports `outcome.timing_ledger`.
Inspect `late_by_ms`, `equivalent_line_extension_mm`,
`shadow_recovered_with_extra_notice`, and the bounded `per_bean` entries before
changing the physical sorting-line offset or valve timing assumptions.
The same ledger reports `actuator_open_lateness_ms` and
`actuator_close_lateness_ms`; these measure host scheduler behaviour separately
from a classification decision that was already too late.

For a repeatable acceptance matrix which does not depend on manually launched
GUIs, run:

```bash
beano-performance-benchmark /path/to/recording-bundle \
  --background-frames 43,222,347 \
  --scenarios core,full --repeats 5 \
  --target-fps 60 --maximum-frames 601 \
  --prebuffer-frames 60 --crops-per-bean 2 \
  --inference-backend tensorrt \
  --output ./performance-report.json
```

Add `--esp32-actuator` to include the connected ESP32-S2 in an isolated
hardware-backed run. The benchmark then starts its own BeanoActuator service,
uses the stable USB path, and requires every reported cycle to complete. Use
`--esp32-port` if the board is connected at a different path.

The benchmark owns isolated endpoints and keeps its Registry alive for every
repetition. Confirm `passed` for both scenarios; this includes the FPS
tolerance, zero dropped/failed jobs and complete expected decision counts. With
`--esp32-actuator`, any recorded failed hardware cycle also makes the scenario
fail.
Then compare individual stage timings.

After upgrading code that changes the registry command contract, stop all
components and restart BeanRegistry before restarting its clients. For the
current performance reference, confirm that the selected source is
`20260816T134132.801241Z-beans`; the launcher remembers only the path supplied
when it was started or subsequently chosen in BeanoFlight.

## Registry ownership recovery

BeanRegistry holds advisory locks beside the database and IPC socket paths for
its entire lifetime. Lock files persist harmlessly after exit; ownership is the
live OS lock, not the presence of the file. A second service exits with status
2 and reports the owning PID metadata.

When upgrading from a version without these locks, BeanoFlight also identifies
local registry processes whose command line selects the database and which
actually hold that SQLite file open. If the launcher says the registry is
occupied but unresponsive, close the dependent component GUIs, terminate every
old `beanoflight.registry_service` process gracefully, verify that none remain,
and then start the registry once. Do not delete the SQLite, WAL or SHM files as
a way of resolving process ownership.
