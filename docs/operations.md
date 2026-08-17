# Review workflow

1. Open a FastCap recording folder or its `CamL-calibrated.mkv`.
2. Confirm that the status line says `exact FastCap timestamps`.
3. Confirm that the metric calibration has loaded and reports its RMS.
4. Find a frame without beans and choose **Use current frame as background**.
   For a more representative model, choose **Choose 3 empty frames for
   background**. Mark each stratified candidate `Empty` or `Contains
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

These probabilities do not yet model bean width, nozzle plume width, valve
latency or category-specific sorting policy. They must not be used to actuate
real hardware in version 0.1.

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
monitor, mock inferencer, sorter and BeanoFlight in separate terminals. Select
the same command and crop endpoints in each GUI. Start the registry first;
BeanoFlight refuses to begin Simulation if its ping is not acknowledged.
The launcher reuses a healthy existing registry only when it reports the
selected database. If the endpoint is serving a different database, or if the
endpoint or selected database is owned but unresponsive, **Start all** stops
immediately with a recovery message instead of creating a second database
writer.

When using `beano-simulation`, leave **Performance mode** selected for a
repeatable throughput run. It starts newly launched components with registry
event printing, Registry Monitor polling, crop and activity rendering, and
virtual-gate animation disabled. The processing services continue to run; only
their optional displays are paused. An already-running registry adopted by the
launcher keeps its original logging setting.

Disable preview for throughput checks. BeanoFlight reports source-read and
analysis time separately. For a complete recording bundle, keep the
memory-mapped RAW fast path enabled: it detects on the native green plane and
colour-processes only asynchronous inference crops. The FFV1 route remains a
useful fallback and review reference, but software decoding can be its limiting
stage. The default 60-frame prebuffer starts before the replay clock and then
overlaps RAW preparation or video decoding with analysis. Set the maximum replay
length between 1 and 1,000 frames. Inspect the registry monitor for a complete
chain of `inference.submitted`, `inference.accepted`, `inference.completed`,
`sorting.decision`, and, where policy selects a gate, `sorting.actuated`.

The Actuation column reads `Awaiting`, `Not required`, `Scheduled`, `OK` or
`FAIL`; a blank result is no longer used for the normal no-gate case. Turn off
live playback, mock crop display, activity logs, registry live updates and
virtual-gate animation when measuring throughput. These controls affect GUI
work only and do not stop the corresponding service.

The headless `beano-system-test` requires exactly 3 explicit, visually
confirmed empty-frame indices and prints a JSON performance summary on
completion. Add `--optimized-raw` to use the same fast path as the GUI.

Every completed session stores its achieved FPS, source-read and analysis
means/maxima, prebuffer time, missed deadlines and crop counts in the session
`settings.performance` object. These values remain available after the
BeanoFlight window closes.

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
