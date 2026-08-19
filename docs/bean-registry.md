# BeanRegistry process contract

`BeanRegistry` is the single writer and authoritative current-state owner for
the live sorter. Other processes send commands or queries; they do not mutate
Python objects or write SQLite tables directly.

At startup the service takes exclusive advisory locks for the resolved
database and both IPC endpoints. It also refuses to bind over a listener left
by an older unlocked release. The launcher probes for a healthy existing
service and adopts it rather than spawning another writer.

```text
Capture/shared frames -> Detector/Tracker -> BeanRegistry <- inference result
       |                                       |
       +-- bounded image crop -> inferencer    +-> sorting decision/result
                                               +-> monitoring events
                                               +-> SQLite WAL history
```

## Identity, ownership and revisions

The public key is the full `BeanRef(run_id, sequence)`. The display may shorten
the run UUID, but IPC and SQLite never do. Internal negative edge-suppression
references are not submitted to the registry.

The tracker exclusively owns lifecycle and kinematic state. Inference and other
workers may only complete registered jobs and append an `Enrichment`. The
sorter may set one decision; an actuator may acknowledge it and record one
result. Completed outcomes cannot be replaced. Each accepted change increments
the bean's registry revision.

Every mutating command carries a caller-stable event ID. Repeating the same
command is idempotent, including after a registry restart. Reusing an event ID,
enrichment result ID or decision ID for different content is rejected.

## Durable schema

`SQLiteBeanRepository` enables WAL mode and schema version 2. Version 1
databases are migrated in place. It owns these normalized tables:

- `sessions` and `beans` for run clocks, identity and current lifecycle;
- `observations` and `track_states` for measurements and Kalman state;
- `predictions` for sorting-line distributions and gate probabilities;
- `inference_jobs` for crop provenance and delivery/completion status;
- `enrichments` for versioned ML or property results;
- `sorting_decisions` for proposed actions and acknowledgements;
- `actuation_results` for observed virtual or physical gate cycles;
- `registry_events` for the ordered, idempotent event journal.

The state change and its event journal entry commit in one transaction. Full
frames and crops are never stored. The hot-path database has one writer; other
tools may open read-only connections for audit queries.

All tracks produced by one camera frame use one ZeroMQ request and one SQLite
transaction. The individual bean revisions and idempotency keys remain
independent, while a failure rolls the entire frame batch back in memory and on
disk before any subscriber is notified.

## ZeroMQ endpoints

The `beano-registry` service binds two local endpoints:

- request/reply `commands` for acknowledged mutations, snapshots, active-bean
  lists and journal recovery;
- publish/subscribe `events` for low-latency replaceable notification.

Messages are finite JSON using schema `beanoflight-registry/v1`; pickle is not
accepted. A `ZeroMQRegistryClient` is thread-affine, so create one in each
worker thread or process.

The first implementation deliberately provides no network authentication or
encryption. Use Unix-domain `ipc://` endpoints with service-owned filesystem
permissions. Do not expose the command endpoint over TCP to an untrusted
network.

Typical inference result:

```python
from beanoflight.models import BeanRef
from beanoflight.registry_models import Enrichment
from beanoflight.registry_zmq import ZeroMQRegistryClient

with ZeroMQRegistryClient(
    "ipc:///tmp/beanoflight-registry-commands.ipc"
) as registry:
    registry.add_enrichment(
        BeanRef("FULL-RUN-UUID", 17),
        Enrichment(
            source="resnet",
            kind="defect",
            value="insect_damage",
            timestamp_ns=1234567890,
            version="beans-v7",
            result_id="resnet-job-abc123",
            confidence=0.94,
        ),
    )
```

## Reliable consumption

PUB/SUB is a wake-up and monitoring path; a slow subscriber may miss an event.
Every published event therefore contains both a per-bean `revision` and a
global persistent `stream_sequence`.

A critical consumer maintains its last committed sequence. On startup, after
a reconnect, or whenever it observes a gap, it calls:

```python
events = registry.events_since(last_sequence)
for event in events:
    process_idempotently(event)
    last_sequence = event.stream_sequence
```

The consumer stores its cursor only after its own effect is safely committed.
This makes the SQLite journal the recovery path while keeping routine latency
on local IPC.

A snapshot-based consumer must not replay the complete journal on every
process start. It first reads `event_cursor()`, then takes the relevant current
state snapshot, and finally consumes events after that cursor. An update racing
with the snapshot can be seen twice, so effects remain idempotent, but no update
is missed. BeanoSorter applies this pattern to live sessions and the most
recent session, and coalesces each event page to one current-state lookup per
bean. During normal live operation BeanoSorter uses the compact materialized
record already embedded in each notification and performs no record query.
Compact journal pages remain the recovery path after a sequence gap.

For frame-rate traffic, use `update_track_revisions()` when the caller only
needs acknowledged revisions and `events_since_compact()` when a consumer only
needs event identity, kind and cursor data. Full record and event payload
queries remain available for diagnostics. Crop submission similarly uses a
revision-only acknowledgement while preserving the durable-before-enqueue
contract. `service_metrics()` exposes bounded operation, SQLite transaction and
hot-cache measurements used by the replay benchmark.

Inference-job rows retain crop source/output dimensions, resize provenance and
critical-path timing marks. Sorting-decision rows retain sorter receipt, notice
margin and required-open timing marks. These fields form the durable per-bean
timing ledger without storing crop images.

## Memory and image copying

Only the latest observation crosses ZeroMQ on a track update. The registry
merges it into the short in-memory history, while SQLite inserts only the new
observation. Query and event responses omit observation history unless a full
`get` is explicitly requested.

The optimized recorded-source simulator copies one bounded Bayer ROI while its
source mapping is live, then calibrates it asynchronously into a contiguous BGR
crop and sends it over a separate ZeroMQ socket, never through the registry.
The live implementation
should replace that copy with a bounded shared-memory pool whose request carries
a frame-slot generation plus bounding box. In both cases the registry stores
job provenance and resulting properties, not image bytes.
