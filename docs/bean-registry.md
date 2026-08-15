# BeanRegistry process contract

`BeanRegistry` is the single writer and authoritative current-state owner for
the live sorter. Other processes send commands or queries; they do not mutate
Python objects or write SQLite tables directly.

```text
Capture/shared frames -> Detector/Tracker -> BeanRegistry <- ResNet result
                                               |
                                               +-> sorting decision/ack
                                               +-> monitoring events
                                               +-> SQLite WAL history
```

## Identity, ownership and revisions

The public key is the full `BeanRef(run_id, sequence)`. The display may shorten
the run UUID, but IPC and SQLite never do. Internal negative edge-suppression
references are not submitted to the registry.

The tracker exclusively owns lifecycle and kinematic state. ResNet and other
workers may only append an `Enrichment`. The sorter may set a decision and
acknowledge it; an acknowledged decision cannot be replaced. Each accepted
change increments the bean's registry revision.

Every mutating command carries a caller-stable event ID. Repeating the same
command is idempotent, including after a registry restart. Reusing an event ID,
enrichment result ID or decision ID for different content is rejected.

## Durable schema

`SQLiteBeanRepository` enables WAL mode and schema version 1. It owns these
normalized tables:

- `sessions` and `beans` for identity and current lifecycle;
- `observations` and `track_states` for measurements and Kalman state;
- `predictions` for sorting-line distributions and gate probabilities;
- `enrichments` for versioned ML or property results;
- `sorting_decisions` for proposed actions and acknowledgements;
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

## Memory and image copying

Only the latest observation crosses ZeroMQ on a track update. The registry
merges it into the short in-memory history, while SQLite inserts only the new
observation. Query and event responses omit observation history unless a full
`get` is explicitly requested.

Camera frames and image crops belong in a bounded shared-memory pool. A future
crop request should carry a frame-slot generation plus bounding box; the
registry stores the resulting properties, not image bytes.
