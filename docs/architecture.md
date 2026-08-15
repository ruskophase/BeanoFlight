# Architecture

BeanoFlight keeps the recorded-video tool and eventual live sorter on the same
core pipeline:

```text
FrameSource -> BeanDetector -> Observation -> TrackManager -> TrajectoryPredictor
                                             |                    |
                                             +-> BeanEvent        +-> gate probabilities
```

## Ownership and copying

The tracking thread is the sole writer of kinematic state. It publishes
immutable snapshots and events; inference and sorting workers cannot mutate a
track. `BeanStore` accepts versioned enrichments such as a ResNet category,
confidence or crop property under the stable `BeanRef`.

A `BeanRef` contains a random run UUID plus a monotonic integer sequence. The
integer is convenient inside a process, while the composite value remains
unique when results from multiple recordings are combined.

Review analysis stores only compact detections, states, covariance and short
observation histories. It never stores decoded full frames. Free-run has a
two-item display queue; if Tk cannot display every image, stale display images
are discarded without skipping tracking input.

## Coordinate and timing domains

All tracking and prediction state is in fall-plane millimetres. Pixel values
remain attached to observations for overlays and future crop extraction.

FastCap's CamL kernel timestamp is authoritative. Only differences within that
timestamp domain are used. Plain videos without `pairs.csv` are supported for
diagnostics but are marked as using nominal FPS time.

## Assignment and lifecycle

Before assignment, every active Kalman state is propagated to the incoming
frame timestamp. Candidate matches outside the Mahalanobis gate are rejected.
Remaining costs combine predicted physical distance and area continuity.

At most ten beans are expected. BeanoFlight uses an exact bitmask dynamic
program for this small assignment rather than importing a large optimization
library. It provides globally optimal one-to-one matching with bounded cost at
this scale.

New IDs normally originate within the first 26 mm below the calibrated top of
the FoV. This accommodates the large inter-frame movement of a freely falling
bean. A new ID is tentative until its second observation. Missing confirmed
tracks become occluded and are propagated briefly; a track becomes exited when
its prediction passes the calibrated lower boundary plus margin.

The left and right birth margins are a separate acceptance rule. Detection is
still performed and displayed inside those regions, but an unmatched first
observation is rejected if any part of its bounding box enters a margin. It is
not called occluded because no trustworthy complete track has existed yet. If
an already-valid track later enters a side margin, normal association keeps its
ID; this avoids renaming a bean as it drifts laterally.

An edge rejection creates a short-lived internal suppression trajectory with
no public `BeanRef`. It consumes later observations from the same edge-entering
bean so that moving fully out of the margin cannot cause a delayed new ID. The
suppression expires after the same bounded miss/exit rules as a normal track;
it is never published to inference or sorting consumers.

## Background models

Recorded-video Review mode uses a temporal median of up to twenty
human-confirmed empty frames. Candidate frames are random within evenly spaced
temporal strata. The first review pass presents one frame from each of twenty
full-video time bands; three further passes provide replacements for rejected
frames. This gives coverage across the recording without silently including
moving beans. The accepted indices and selection seed are analysis provenance.

The later live source should begin with an explicit background-acquisition
period while bean feed is stopped. Continuous adaptation can then use a slow
per-pixel model only where a dilated foreground mask and every active or
recently missed track agree that the scene is clear. Updates should freeze
during a busy scene, exposure/illumination change, or camera movement. This
prevents a stationary or slow bean from being learned into the background and
keeps live adaptation separable from the version 0.1 recorded-video model.

## Asynchronous extension points

`EventBus` provides bounded non-blocking subscriber queues. Events currently
include creation, confirmation, exit and cancellation. A later cropper can
subscribe to creation/update events, submit an image tensor to ResNet, and add
the result to `BeanStore`. A sorting decision process can combine the newest
track prediction with those enrichments without sharing mutable objects.

No hardware output is present in version 0.1. Gate selection is diagnostic.
