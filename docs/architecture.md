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
observation histories. It never stores decoded full frames. Simulation has a
latest-only display queue; if Tk cannot display every image, stale preview
images are discarded without skipping tracking input. Crop transport has its
own bounded queue, so inference backpressure is explicit and does not retain an
unbounded collection of images.

## Coordinate and timing domains

All tracking and prediction state is in fall-plane millimetres. Pixel values
remain attached to observations for overlays and future crop extraction.
Optimized RAW detections retain distorted sensor pixels for bounding boxes and
crop extraction. Each centroid alone is undistorted with the frozen CamL lens
model before the undistorted PinkPlane homography converts it to millimetres;
no full-frame geometric remap is required on the tracking path.
All centroids from one frame are transformed in one vectorized OpenCV call.

FastCap's CamL kernel timestamp is authoritative. Only differences within that
timestamp domain are used. Plain videos without `pairs.csv` are supported for
diagnostics but are marked as using nominal FPS time.
Run creation/update fields remain in Unix wall-clock nanoseconds. Persisting a
source-domain observation never rewrites those session fields; source and wall
clocks meet only through the explicit run clock anchor.

CamL remains the only detection and tracking view. For inference, its distorted
sensor centroid is point-undistorted, mapped through PinkPlane's undistorted
CamL-to-CamR homography, and distorted into CamR sensor coordinates. A bounded
local CamR foreground search refines that projection to the observed bean. Both
complete Bayer ROIs are then demosaiced and transported together with their
frame indices, camera timestamps and both projected/refined centroids. No
full-frame CamR demosaic, undistortion or motion-detection pass is performed.
The localizer uses one native Bayer green sample and contour bounding boxes on
its normal path. An averaged dual-green retry is retained if that faster mask
finds no valid component. Precomposed 16-bit-to-detection lookup tables remove
per-frame RAW shifting without changing CamL detection values.
CamL and CamR Bayer crop materialization then run concurrently on the dispatch
stage before their atomic two-view transport.

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

`exited` ends image association, not flight prediction. The final propagated
state continues to predict the sorting line 30 mm below the FoV, so a result
arriving just after image exit can still produce a safe decision. Only a
tentative track that becomes `cancelled` has no downstream prediction.

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

Recorded-video Review mode currently uses a temporal median of up to three
human-confirmed empty frames. Candidate frames are random within evenly spaced
temporal strata. The first review pass presents one frame from each of three
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

`BeanRegistry` is now the authoritative materialized state for public bean
identities. `AnalysisEngine` accepts either an in-process registry or a
ZeroMQ-backed registry client and submits every current track/prediction with
an idempotent event ID. Inference workers complete registered crop jobs and add
versioned enrichments; a sorting worker adds an immutable decision, and a
virtual or physical actuator records its observed result. Neither can mutate
tracker state.

SQLite WAL stores run clocks, normalized observations, track states,
predictions, crop-job metadata, enrichments, decisions, actuation results and
an ordered event journal. ZeroMQ request/reply is
the acknowledged write/query path. PUB/SUB is deliberately limited to bounded
replaceable notifications. Each notification carries a persistent global
stream sequence, so a critical consumer uses `events_since(cursor)` to recover
any gap before proceeding.

The normal classification control path is a dedicated acknowledged ZeroMQ
REQ/REP socket from the inferencer to the single sorter. One bounded message represents the GPU
batch and carries inference-job metadata, class probabilities and logits only.
Before dispatching crops, BeanoFlight sends the current tracks, predictions and
replay-clock anchor over a second bounded PUSH/PULL socket. The sorter joins the
two compact messages from local caches and can plan a valve before the
inferencer's separate Registry commit finishes. Their independent sockets may
arrive in either order, so evidence waits briefly for its matching context;
Registry notifications remain the loss-recovery path. Routine Registry
lifecycle notifications are header-only to avoid duplicating full records at
frame rate. Inference jobs and sorting decisions persist their source-clock and
host-monotonic timing marks alongside crop provenance; no image data enters
either transport or ledger.

The direct evidence publisher uses up to three bounded 5 ms acknowledgement
attempts. A
dedicated ingress worker acknowledges only after validating the batch and
admitting it to a bounded in-process queue, before classification policy work.
Accepted batch IDs are cached so an acknowledgement lost in transit can be
retried without applying the evidence twice. A negative or missing
acknowledgement is retained in timing telemetry. A
Registry classification notification is held for a short
preference interval and then used as recovery if all direct attempts fail; the
durable journal remains the restart and sequence-gap path.

The sorter control worker performs no Registry or SQLite work. Registry
snapshots, event recovery and audits have separate workers. Four persistent
handoff workers prevent one acknowledged plan from head-of-line blocking other
beans in the same frame. Approved plans cross a second acknowledged IPC channel
to BeanoActuator, where acknowledgement means that a validated plan is in its
bounded deadline-ordered queue. That process synchronizes
the host monotonic clock with the ESP32-S2 and transfers absolute gate-open and
gate-close timestamps. A 1 MHz board GPTimer checks the bounded plan table every
100 us, so host scheduling jitter and SQLite latency are no longer in the final
edge-timing loop. Plan admission and native-USB I/O share one kernel-woken event
loop, avoiding an extra host scheduling hop. The actuator's lower-priority audit
worker persists the observed hardware result after the cycle through a compact
Registry acknowledgement.

The Performance launcher assigns general processes to the lower-numbered CPUs
and reserves the highest two available CPUs for Sorter and Actuator. It also
uses a shorter Python thread-switch interval in those latency-sensitive
processes. These are host jitter controls, not a hard real-time guarantee; the
firmware remains the sole owner of final output-edge timing.

Each inference is stored as immutable `classification_evidence` containing its
class order, complete probability vector, logits, ensemble ID and sample index.
When all requested samples arrive, BeanoSorter can append one local
`classification_pooled` result using mean probability and schedule immediately;
its audit worker persists the pool and decision afterwards. BeanRegistry also
materializes the identical pool atomically with the final completion, so either
arrival order is idempotent. BeanoSorter acts only on the pooled result. Its
deadline timer reserves the configured gate lead, minimum notice and an
additional processing margin; if the ensemble is still incomplete at that
cutoff, the sorter first drains evidence already queued on its direct socket,
then uses a one-sample pooled fallback if necessary. Every decision also stores
an immutable `classification_decision_basis` copy of the exact vector it used.
This is intentionally separate from Registry's canonical first-writer-wins
pool, so a concurrent complete pool cannot rewrite the historical explanation
for a fallback decision.

The 60 FPS frame transaction records undo information only for bean IDs and
bounded journal/idempotency entries changed by that frame; it never copies the
whole Registry cache. Track-update acknowledgements contain only bean ID and
revision, and recovery consumers read compact journal headers before requesting
a record they actually need. The durable hot-record cache is capped, bulk SQLite
queries bypass it, and no persisted bean data is discarded by cache eviction.

Recorded simulation uses a bounded sequential producer ahead of the analysis
thread, and the replay clock is anchored only after that initial buffer is
ready. In optimized stereo mode it memory-maps synchronized CamL/CamR RG10,
stores one compact CamL green plane per slot, and touches CamR only in bounded
candidate ROIs instead of expanding either frame to BGR. In fallback mode it
overlaps FFV1 decoding with analysis. The producer continues filling released
slots while analysis consumes frames; both capacity (0-120, where zero disables
it) and replay length (1-1,000 frames) are bounded and recorded in the session.

The original `EventBus` and `BeanStore` remain small same-process adapters for
simple workers and backwards compatibility. They are not the live
multi-process source of truth. See `bean-registry.md` for the process contract.

The current physical output is an ESP32-S2 bank of low-current indicator LEDs.
It demonstrates gate timing only; no valve power stage is implemented.
