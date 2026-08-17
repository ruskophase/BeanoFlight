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

Disable preview for throughput checks. BeanoFlight reports source-read and
analysis time separately: on an FFV1 recording, software decoding may be the
limiting stage even when the future live-camera detector path is within its
16.67 ms budget. Inspect the registry monitor for a complete chain of
`inference.submitted`, `inference.accepted`, `inference.completed`,
`sorting.decision`, and, where policy selects a gate, `sorting.actuated`.

The headless `beano-system-test` requires exactly 3 explicit, visually
confirmed empty-frame indices and prints a JSON performance summary on
completion. It uses the same replay core as the GUI.
