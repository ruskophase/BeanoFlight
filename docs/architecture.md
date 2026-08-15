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

## Asynchronous extension points

`EventBus` provides bounded non-blocking subscriber queues. Events currently
include creation, confirmation, exit and cancellation. A later cropper can
subscribe to creation/update events, submit an image tensor to ResNet, and add
the result to `BeanStore`. A sorting decision process can combine the newest
track prediction with those enrichments without sharing mutable objects.

No hardware output is present in version 0.1. Gate selection is diagnostic.
