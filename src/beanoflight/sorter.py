"""Durable classification policy and virtual valve scheduling service."""

from __future__ import annotations

import math
import queue
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from itertools import pairwise

import zmq

from .actuation_transport import (
    ActuationPlan,
    ZeroMQActuationPlanPublisher,
)
from .classification import (
    CLASSIFICATION_POOLED,
    LEGACY_CLASSIFICATION,
    classification_decision_basis,
    classification_ensemble_id,
    evidence_for_ensemble,
    expected_evidence_count,
    pool_classification_evidence,
    pooled_for_ensemble,
)
from .classification_transport import (
    DEFAULT_DIRECT_EVIDENCE_ENDPOINT,
    DirectEvidenceBatch,
    ZeroMQDirectEvidenceReceiver,
)
from .models import BeanEvent, BeanRef, GateProbability, TrackStatus
from .registry_models import (
    ActuationResult,
    BeanRecord,
    Enrichment,
    RunSession,
    RunState,
    SortingDecision,
    record_from_dict,
)
from .registry_service import DEFAULT_COMMAND_ENDPOINT, DEFAULT_EVENT_ENDPOINT
from .registry_zmq import (
    RegistryRemoteError,
    ZeroMQRegistryClient,
    ZeroMQRegistrySubscriber,
)
from .runtime_priority import lower_current_thread_priority
from .sorting_context_transport import (
    DEFAULT_SORTING_CONTEXT_ENDPOINT,
    SortingContextBatch,
    ZeroMQSortingContextReceiver,
)


@dataclass(frozen=True, slots=True)
class SorterSettings:
    reject_categories: tuple[str, ...] = ("insect_damage", "mould", "broken")
    minimum_confidence: float = 0.75
    gate_probability_threshold: float = 0.35
    open_lead_ms: float = 8.0
    close_lag_ms: float = 12.0
    minimum_notice_ms: float = 4.0
    ensemble_deadline_reserve_ms: float = 10.0
    allow_adjacent_gate_pair: bool = True
    policy_version: str = "simulation-v3-ensemble"

    def validate(self) -> None:
        if any(not value.strip() for value in self.reject_categories):
            raise ValueError("sort categories cannot be blank")
        if not math.isfinite(self.minimum_confidence) or not (
            0 <= self.minimum_confidence <= 1
        ):
            raise ValueError("minimum confidence must be between zero and one")
        if not math.isfinite(self.gate_probability_threshold) or not (
            0 <= self.gate_probability_threshold <= 1
        ):
            raise ValueError("gate probability threshold must be between zero and one")
        timing = (
            self.open_lead_ms,
            self.close_lag_ms,
            self.minimum_notice_ms,
            self.ensemble_deadline_reserve_ms,
        )
        if any(not math.isfinite(value) or value < 0 for value in timing):
            raise ValueError("sort timing values must be finite and non-negative")


@dataclass(frozen=True, slots=True)
class SorterActivity:
    kind: str
    bean_id: str = ""
    decision_id: str = ""
    gate_indices: tuple[int, ...] = ()
    category: str = ""
    confidence: float | None = None
    detail: str = ""


@dataclass(frozen=True, slots=True)
class _PendingActuation:
    record: BeanRecord
    session: RunSession
    open_monotonic_ns: int | None
    close_monotonic_ns: int | None
    opened_source_ns: int | None = None


@dataclass(frozen=True, slots=True)
class _DecisionAudit:
    record: BeanRecord
    category: str = ""
    confidence: float | None = None
    pooled_enrichment: Enrichment | None = None
    classification_basis: Enrichment | None = None
    attempts: int = 0


@dataclass(frozen=True, slots=True)
class _ActuationAudit:
    bean_ref: BeanRef
    record: BeanRecord
    result: ActuationResult
    attempts: int = 0


@dataclass(frozen=True, slots=True)
class _PendingEnsemble:
    record: BeanRecord
    deadline_monotonic_ns: int
    direct_path: bool = False
    direct_sent_monotonic_ns: int | None = None
    direct_received_monotonic_ns: int | None = None
    context_path: bool = False
    context_received_monotonic_ns: int | None = None


@dataclass(frozen=True, slots=True)
class _PendingRegistryRecovery:
    record: BeanRecord
    due_monotonic_ns: int


@dataclass(frozen=True, slots=True)
class _CachedSortingContext:
    record: BeanRecord
    received_monotonic_ns: int
    frame_index: int


@dataclass(frozen=True, slots=True)
class _RecoveredRecord:
    record: BeanRecord
    received_monotonic_ns: int
    defer_classification: bool = True


@dataclass(frozen=True, slots=True)
class _ReceivedDirectBatch:
    batch: DirectEvidenceBatch
    received_monotonic_ns: int


@dataclass(frozen=True, slots=True)
class _ExternalActuation:
    record: BeanRecord
    session: RunSession
    plan: ActuationPlan


class SorterService:
    """Recoverable decision service with an isolated actuation-plan output."""

    EXTERNAL_ACTUATION_WORKERS = 4

    def __init__(
        self,
        *,
        registry_endpoint: str = DEFAULT_COMMAND_ENDPOINT,
        event_endpoint: str = DEFAULT_EVENT_ENDPOINT,
        classification_endpoint: str = DEFAULT_DIRECT_EVIDENCE_ENDPOINT,
        sorting_context_endpoint: str = DEFAULT_SORTING_CONTEXT_ENDPOINT,
        actuation_endpoint: str = "",
        settings: SorterSettings | None = None,
        activity: Callable[[SorterActivity], None] | None = None,
    ) -> None:
        self.registry_endpoint = registry_endpoint
        self.event_endpoint = event_endpoint
        self.classification_endpoint = classification_endpoint
        self.sorting_context_endpoint = sorting_context_endpoint
        self.actuation_endpoint = actuation_endpoint
        self.settings = settings or SorterSettings()
        self.settings.validate()
        self.activity = activity
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._pending: dict[str, _PendingActuation] = {}
        self._pending_lock = threading.RLock()
        self._actuator_condition = threading.Condition(self._pending_lock)
        self._awaiting_prediction: set[BeanRef] = set()
        self._awaiting_ensemble: dict[BeanRef, _PendingEnsemble] = {}
        self._pending_registry_recovery: dict[
            BeanRef, _PendingRegistryRecovery
        ] = {}
        self._planned: set[BeanRef] = set()
        self._audit_queue: queue.Queue[_DecisionAudit] = queue.Queue()
        self._actuation_audit_queue: queue.Queue[_ActuationAudit] = queue.Queue()
        self._sessions: dict[str, RunSession] = {}
        self._direct_evidence: dict[BeanRef, dict[str, Enrichment]] = {}
        self._direct_timing: dict[str, tuple[int, int]] = {}
        self._direct_ingress: queue.Queue[_ReceivedDirectBatch] = queue.Queue(
            maxsize=1_024
        )
        self._admitted_direct_batches: OrderedDict[str, None] = OrderedDict()
        self._direct_ingress_ready = threading.Event()
        if not self.classification_endpoint:
            self._direct_ingress_ready.set()
        self._sorting_contexts: dict[BeanRef, _CachedSortingContext] = {}
        self._recovery_queue: queue.Queue[RunSession | _RecoveredRecord] = (
            queue.Queue(maxsize=2_048)
        )
        self._recovery_watch: set[BeanRef] = set()
        self._recovery_watch_lock = threading.Lock()
        self._external_actuation_queue: queue.Queue[_ExternalActuation] = (
            queue.Queue(maxsize=256)
        )
        self._externally_scheduled: set[str] = set()
        self._gate_counts: dict[int, int] = {}
        self._cursor = 0
        self.decisions = 0
        self.actuations = 0
        self.errors = 0
        self.event_notifications = 0
        self.fallback_journal_queries = 0
        self.low_confidence_defects = 0
        self.pooled_classifications = 0
        self.deadline_fallbacks = 0
        self.direct_batches_received = 0
        self.direct_evidence_received = 0
        self.direct_batches_rejected = 0
        self.direct_registry_reads = 0
        self.registry_recovery_decisions = 0
        self.context_batches_received = 0
        self.contexts_received = 0
        self.context_cache_hits = 0
        self.context_cache_misses = 0
        self.external_plans_accepted = 0
        self.external_plans_rejected = 0
        self.ready = threading.Event()
        self.startup_error = ""

    @property
    def gate_states(self) -> dict[int, bool]:
        with self._pending_lock:
            return {gate: count > 0 for gate, count in self._gate_counts.items()}

    def start(self) -> None:
        if self._threads:
            return
        workers = []
        if self.classification_endpoint:
            workers.append(
                (
                    "beano-sorter-evidence-ingress",
                    self._direct_evidence_ingress_loop,
                )
            )
        workers.extend([
            ("beano-sorter-decisions", self._decision_loop),
            ("beano-sorter-recovery", self._recovery_loop),
            ("beano-sorter-audit", self._audit_loop),
            ("beano-actuation-audit", self._actuation_audit_loop),
        ])
        if self.actuation_endpoint:
            workers.extend(
                (
                    f"beano-external-actuator-plans-{index + 1}",
                    self._external_actuator_loop,
                )
                for index in range(self.EXTERNAL_ACTUATION_WORKERS)
            )
        else:
            workers.append(("beano-virtual-actuator", self._actuator_loop))
        for name, target in workers:
            thread = threading.Thread(target=target, name=name, daemon=True)
            self._threads.append(thread)
            thread.start()

    def close(self) -> None:
        self._stop.set()
        with self._actuator_condition:
            self._actuator_condition.notify_all()
        for thread in self._threads:
            thread.join(2.0)
        self._threads.clear()

    def _decision_loop(self) -> None:
        contexts = None
        try:
            if self.sorting_context_endpoint:
                contexts = ZeroMQSortingContextReceiver(
                    self.sorting_context_endpoint
                )
                self.sorting_context_endpoint = contexts.endpoint
        except Exception as exc:  # noqa: BLE001 - surfaced to GUI/controller
            self.startup_error = str(exc)
            self._emit("error", detail=self.startup_error)
            self.ready.set()
            if contexts is not None:
                contexts.close()
            return
        if not self._direct_ingress_ready.wait(2.0):
            self.startup_error = "direct evidence ingress did not become ready"
            self._emit("error", detail=self.startup_error)
            self.ready.set()
            if contexts is not None:
                contexts.close()
            return
        if self.startup_error:
            self.ready.set()
            if contexts is not None:
                contexts.close()
            return
        poller = _control_poller(None, contexts)
        self.ready.set()
        try:
            while not self._stop.is_set():
                try:
                    readable = dict(
                        poller.poll(
                            0
                            if not self._direct_ingress.empty()
                            else min(1, self._ensemble_receive_timeout_ms())
                        )
                    )
                    if contexts is not None and (
                        contexts.socket in readable
                        or contexts.socket.poll(0, zmq.POLLIN)
                    ):
                        # Context is cheaper than a Registry lookup and should be
                        # current before inference evidence is considered.
                        for _index in range(256):
                            if not contexts.socket.poll(0, zmq.POLLIN):
                                break
                            self._process_sorting_context(
                                contexts.receive_batch(),
                                None,
                                received_monotonic_ns=time.monotonic_ns(),
                            )
                    # The ingress worker has already decoded, admitted and ACKed
                    # these batches. Context wins when both become ready together,
                    # then evidence is drained without more socket work.
                    self._drain_direct_ingress(limit=256)
                    self._drain_recovery_queue(limit=256)
                    # A result may have arrived while a context or Registry
                    # recovery item was handled. Poll once more at zero wait so
                    # queued evidence wins over an expiring fallback.
                    self._drain_direct_ingress(limit=256)
                    # Only expire after every evidence message which was already
                    # readable at the deadline has had a chance to complete a pool.
                    self._release_due_ensemble_fallbacks(None)
                    self._release_due_registry_recoveries(None)
                except Exception as exc:  # noqa: BLE001 - control loop stays alive
                    self.errors += 1
                    self._emit("error", detail=str(exc))
                    self._stop.wait(0.005)
        finally:
            if contexts is not None:
                contexts.close()

    def _direct_evidence_ingress_loop(self) -> None:
        try:
            receiver = ZeroMQDirectEvidenceReceiver(self.classification_endpoint)
        except Exception as exc:  # noqa: BLE001 - surfaced to GUI/controller
            self.startup_error = str(exc)
            self._emit("error", detail=self.startup_error)
            self._direct_ingress_ready.set()
            return
        self.classification_endpoint = receiver.endpoint
        self._direct_ingress_ready.set()
        try:
            while not self._stop.is_set():
                try:
                    receiver.receive_batch(
                        timeout_ms=100,
                        accept=self._admit_direct_evidence,
                    )
                except Exception as exc:  # noqa: BLE001 - recovery remains live
                    self.errors += 1
                    self._emit("error", detail=f"direct evidence ingress: {exc}")
                    self._stop.wait(0.001)
        finally:
            receiver.close()

    def _admit_direct_evidence(
        self,
        batch: DirectEvidenceBatch,
        received_monotonic_ns: int,
    ) -> bool:
        # A missing ACK makes the publisher reset its REQ socket and resend the
        # identical batch. Acknowledge that retry without consuming ingress
        # capacity or processing its evidence twice.
        if batch.batch_id in self._admitted_direct_batches:
            self._admitted_direct_batches.move_to_end(batch.batch_id)
            return True
        try:
            self._direct_ingress.put_nowait(
                _ReceivedDirectBatch(batch, received_monotonic_ns)
            )
        except queue.Full:
            self.direct_batches_rejected += 1
            return False
        self._admitted_direct_batches[batch.batch_id] = None
        while len(self._admitted_direct_batches) > 4_096:
            self._admitted_direct_batches.popitem(last=False)
        return True

    def _drain_direct_ingress(self, *, limit: int) -> int:
        drained = 0
        for _index in range(limit):
            try:
                received = self._direct_ingress.get_nowait()
            except queue.Empty:
                break
            try:
                self._process_direct_evidence(
                    received.batch,
                    None,
                    received_monotonic_ns=received.received_monotonic_ns,
                )
                drained += 1
            finally:
                self._direct_ingress.task_done()
        return drained

    def _recovery_loop(self) -> None:
        """Own all Registry reads and notifications away from control timing."""

        registry = ZeroMQRegistryClient(self.registry_endpoint, timeout_ms=2_000)
        subscriber = ZeroMQRegistrySubscriber(self.event_endpoint)
        initialized = False
        last_catch_up = 0.0
        try:
            while not self._stop.is_set():
                try:
                    if not initialized:
                        self._cursor = registry.event_cursor()
                        sessions = registry.list_sessions()
                        for session in sessions:
                            self._put_recovery(session)
                        for run_id in _recovery_run_ids(sessions):
                            for record in registry.list_records(run_id=run_id):
                                self._put_recovery(
                                    _RecoveredRecord(
                                        record,
                                        time.monotonic_ns(),
                                        defer_classification=False,
                                    )
                                )
                        initialized = True
                        last_catch_up = time.monotonic()
                    events = subscriber.receive_many(timeout_ms=100)
                    if events:
                        self.event_notifications += len(events)
                        received_ns = time.monotonic_ns()
                        if events[0].stream_sequence > self._cursor + 1:
                            self._recover_events_since(registry, received_ns)
                        fresh = tuple(
                            event
                            for event in events
                            if event.stream_sequence > self._cursor
                        )
                        if fresh:
                            self._queue_recovery_events(
                                fresh,
                                registry,
                                received_monotonic_ns=received_ns,
                                use_embedded_state=True,
                            )
                        last_catch_up = time.monotonic()
                    elif time.monotonic() - last_catch_up >= 0.25:
                        self._recover_events_since(registry, time.monotonic_ns())
                        last_catch_up = time.monotonic()
                except Exception as exc:  # noqa: BLE001 - durable recovery reconnect
                    self.errors += 1
                    self._emit("error", detail=f"Registry recovery: {exc}")
                    registry.close()
                    subscriber.close()
                    if self._stop.wait(0.25):
                        break
                    registry = ZeroMQRegistryClient(
                        self.registry_endpoint, timeout_ms=2_000
                    )
                    subscriber = ZeroMQRegistrySubscriber(self.event_endpoint)
                    initialized = False
        finally:
            subscriber.close()
            registry.close()

    def _put_recovery(self, item: RunSession | _RecoveredRecord) -> None:
        while not self._stop.is_set():
            try:
                self._recovery_queue.put(item, timeout=0.05)
                return
            except queue.Full:
                continue

    def _recover_events_since(
        self,
        registry: ZeroMQRegistryClient,
        received_monotonic_ns: int,
    ) -> None:
        events = registry.events_since_compact(self._cursor, limit=500)
        self.fallback_journal_queries += 1
        if events:
            self._queue_recovery_events(
                events,
                registry,
                received_monotonic_ns=received_monotonic_ns,
            )

    def _queue_recovery_events(
        self,
        events: tuple[BeanEvent, ...],
        registry: ZeroMQRegistryClient,
        *,
        received_monotonic_ns: int,
        use_embedded_state: bool = False,
    ) -> None:
        with self._recovery_watch_lock:
            watched = frozenset(self._recovery_watch)
        selected = tuple(
            event
            for event in events
            if event.kind in {"inference.completed", "enrichment.added"}
            or (
                event.bean_ref in watched
                and event.kind
                in {
                    "bean.created",
                    "bean.confirmed",
                    "bean.cancelled",
                    "track.updated",
                    "bean.exited",
                }
            )
        )
        by_ref = {event.bean_ref: event for event in selected}
        for bean_ref, event in by_ref.items():
            record = _embedded_record(event) if use_embedded_state else None
            if record is None:
                record = registry.get(bean_ref, include_history=False)
            self._put_recovery(
                _RecoveredRecord(
                    record,
                    received_monotonic_ns,
                    defer_classification=bool(self.classification_endpoint)
                    and event.kind
                    in {"inference.completed", "enrichment.added"},
                )
            )
        if events:
            self._cursor = events[-1].stream_sequence

    def _drain_recovery_queue(self, *, limit: int) -> None:
        for _index in range(limit):
            try:
                item = self._recovery_queue.get_nowait()
            except queue.Empty:
                break
            try:
                if isinstance(item, RunSession):
                    self._sessions[item.run_id] = item
                else:
                    self._accept_recovered_record(item)
            finally:
                self._recovery_queue.task_done()

    def _accept_recovered_record(self, recovered: _RecoveredRecord) -> None:
        record = recovered.record
        bean_ref = record.bean_ref
        if recovered.defer_classification and bean_ref not in self._planned:
            previous = self._pending_registry_recovery.get(bean_ref)
            self._pending_registry_recovery[bean_ref] = _PendingRegistryRecovery(
                record=(
                    record
                    if previous is None or record.revision >= previous.record.revision
                    else previous.record
                ),
                due_monotonic_ns=recovered.received_monotonic_ns + 5_000_000,
            )
            return
        record = self._with_direct_evidence(record)
        sent_ns, direct_received_ns = self._direct_delivery_timing(record)
        cached_context = self._sorting_contexts.get(bean_ref)
        if cached_context is not None:
            record = _merge_sorting_context(cached_context.record, record)
        self._consider(
            record,
            None,
            arrival_monotonic_ns=recovered.received_monotonic_ns,
            direct_path=direct_received_ns is not None,
            direct_sent_monotonic_ns=sent_ns,
            direct_received_monotonic_ns=direct_received_ns,
            context_path=cached_context is not None,
            context_received_monotonic_ns=(
                None
                if cached_context is None
                else cached_context.received_monotonic_ns
            ),
        )

    def _catch_up(
        self,
        registry: ZeroMQRegistryClient,
        received_monotonic_ns: int,
    ) -> None:
        events = registry.events_since_compact(self._cursor, limit=500)
        self.fallback_journal_queries += 1
        if events:
            self._process_events(
                events,
                registry,
                received_monotonic_ns=received_monotonic_ns,
            )

    def _recover_current_state(self, registry: ZeroMQRegistryClient) -> None:
        # Take the cursor before the snapshot. Events racing with the snapshot are
        # replayed once afterwards; immutable decisions make that harmless.
        self._cursor = registry.event_cursor()
        sessions = registry.list_sessions()
        self._sessions.update({session.run_id: session for session in sessions})
        for run_id in _recovery_run_ids(sessions):
            for record in registry.list_records(run_id=run_id):
                self._consider(record, registry)

    def _process_direct_evidence(
        self,
        batch: DirectEvidenceBatch,
        registry: ZeroMQRegistryClient | None,
        *,
        received_monotonic_ns: int,
    ) -> None:
        self.direct_batches_received += 1
        self.direct_evidence_received += len(batch.items)
        references = []
        for item in batch.items:
            bean_ref = item.job.bean_ref
            if bean_ref in self._planned:
                continue
            cached = self._direct_evidence.setdefault(bean_ref, {})
            cached.setdefault(item.enrichment.result_id, item.enrichment)
            self._direct_timing.setdefault(
                item.enrichment.result_id,
                (batch.sent_monotonic_ns, received_monotonic_ns),
            )
            if bean_ref not in references:
                references.append(bean_ref)
            self._pending_registry_recovery.pop(bean_ref, None)
        if not references:
            return
        records = []
        context_received_by_ref: dict[BeanRef, int] = {}
        for bean_ref in references:
            cached_context = self._sorting_contexts.get(bean_ref)
            if cached_context is None:
                self.context_cache_misses += 1
                # The trajectory publisher precedes crop dispatch, but the two
                # independent sockets can be observed in either order. Retain
                # the evidence and let the next context message trigger the
                # decision. If that best-effort message was lost, the durable
                # Registry completion notification remains the recovery path.
                self._awaiting_prediction.add(bean_ref)
                with self._recovery_watch_lock:
                    self._recovery_watch.add(bean_ref)
                continue
            records.append(cached_context.record)
            context_received_by_ref[bean_ref] = (
                cached_context.received_monotonic_ns
            )
            self.context_cache_hits += 1
        for record in records:
            record = self._with_direct_evidence(record)
            sent_ns, first_received_ns = self._direct_delivery_timing(record)
            context_received_ns = context_received_by_ref.get(record.bean_ref)
            self._consider(
                record,
                registry,
                arrival_monotonic_ns=time.monotonic_ns(),
                direct_path=True,
                direct_sent_monotonic_ns=sent_ns,
                direct_received_monotonic_ns=first_received_ns,
                context_path=context_received_ns is not None,
                context_received_monotonic_ns=context_received_ns,
            )

    def _process_sorting_context(
        self,
        batch: SortingContextBatch,
        registry: ZeroMQRegistryClient | None,
        *,
        received_monotonic_ns: int,
    ) -> None:
        self.context_batches_received += 1
        self.contexts_received += len(batch.items)
        self._sessions[batch.run_id] = RunSession(
            run_id=batch.run_id,
            revision=0,
            state=RunState.RUNNING,
            source_path="/direct-sorting-context",
            source_kind="direct",
            frame_count=max(1, batch.frame_index + 1),
            source_fps=batch.source_fps,
            target_fps=batch.target_fps,
            source_start_timestamp_ns=batch.clock_source_timestamp_ns,
            clock_source_timestamp_ns=batch.clock_source_timestamp_ns,
            clock_monotonic_ns=batch.clock_monotonic_ns,
            preview_enabled=False,
            created_timestamp_ns=0,
            updated_timestamp_ns=0,
            settings={},
        )
        for item in batch.items:
            track = item.track
            bean_ref = track.bean_ref
            previous = self._sorting_contexts.get(bean_ref)
            if (
                previous is not None
                and previous.record.track.timestamp_ns > track.timestamp_ns
            ):
                continue
            context_record = BeanRecord(
                bean_ref=bean_ref,
                revision=0,
                status=track.status,
                created_timestamp_ns=track.timestamp_ns,
                updated_timestamp_ns=track.timestamp_ns,
                track=track,
                prediction=item.prediction,
            )
            self._sorting_contexts[bean_ref] = _CachedSortingContext(
                context_record,
                received_monotonic_ns,
                batch.frame_index,
            )
            pending = self._awaiting_ensemble.get(bean_ref)
            if pending is not None:
                updated_record = _merge_sorting_context(
                    context_record,
                    pending.record,
                )
                self._awaiting_ensemble[bean_ref] = replace(
                    pending,
                    record=updated_record,
                    # A pending two-sample pool is tied to the latest physical
                    # crossing estimate, not to the first trajectory that happened
                    # to be available when evidence arrived.  In particular, an
                    # earlier revised crossing must advance the fallback deadline.
                    deadline_monotonic_ns=self._ensemble_deadline_monotonic_ns(
                        updated_record,
                        registry,
                    ),
                    context_path=True,
                    context_received_monotonic_ns=received_monotonic_ns,
                )
            if bean_ref not in self._awaiting_prediction:
                continue
            record = self._with_direct_evidence(context_record)
            sent_ns, direct_received_ns = self._direct_delivery_timing(record)
            if direct_received_ns is None:
                continue
            self._consider(
                record,
                registry,
                arrival_monotonic_ns=time.monotonic_ns(),
                direct_path=True,
                direct_sent_monotonic_ns=sent_ns,
                direct_received_monotonic_ns=direct_received_ns,
                context_path=True,
                context_received_monotonic_ns=received_monotonic_ns,
            )
        self._evict_stale_sorting_contexts(batch.run_id, batch.frame_index)

    def _evict_stale_sorting_contexts(
        self, run_id: str, current_frame_index: int
    ) -> None:
        oldest_frame = current_frame_index - 120
        stale = tuple(
            bean_ref
            for bean_ref, cached in self._sorting_contexts.items()
            if bean_ref.run_id == run_id
            and cached.frame_index < oldest_frame
            and bean_ref not in self._direct_evidence
            and bean_ref not in self._awaiting_ensemble
            and bean_ref not in self._awaiting_prediction
        )
        for bean_ref in stale:
            self._sorting_contexts.pop(bean_ref, None)

    def _with_direct_evidence(self, record: BeanRecord) -> BeanRecord:
        cached = self._direct_evidence.get(record.bean_ref)
        if not cached:
            return record
        known = {item.result_id for item in record.enrichments}
        additions = tuple(
            item for result_id, item in cached.items() if result_id not in known
        )
        return (
            record
            if not additions
            else replace(record, enrichments=(*record.enrichments, *additions))
        )

    def _direct_delivery_timing(
        self, record: BeanRecord
    ) -> tuple[int | None, int | None]:
        evidence = evidence_for_ensemble(record.enrichments)
        timings = tuple(
            self._direct_timing[item.result_id]
            for item in evidence
            if item.result_id in self._direct_timing
        )
        return timings[-1] if timings else (None, None)

    def _clear_direct_evidence(self, bean_ref: BeanRef) -> None:
        cached = self._direct_evidence.pop(bean_ref, {})
        for result_id in cached:
            self._direct_timing.pop(result_id, None)

    def _process_events(
        self,
        events: tuple[BeanEvent, ...],
        registry: ZeroMQRegistryClient,
        *,
        received_monotonic_ns: int | None = None,
        use_embedded_state: bool = False,
        defer_classifications: bool = False,
    ) -> None:
        # A classification is the normal decision trigger. Track updates only
        # matter for the rare classified bean which did not yet have a trajectory.
        selected_events = tuple(
            event
            for event in events
            if event.kind in {"inference.completed", "enrichment.added"}
            or (
                (
                    event.bean_ref in self._awaiting_prediction
                    or event.bean_ref in self._awaiting_ensemble
                )
                and event.kind
                in {
                    "bean.created",
                    "bean.confirmed",
                    "bean.cancelled",
                    "track.updated",
                    "bean.exited",
                }
            )
        )
        by_ref = {event.bean_ref: event for event in selected_events}
        for bean_ref, event in by_ref.items():
            record = _embedded_record(event) if use_embedded_state else None
            if record is None:
                record = registry.get(bean_ref, include_history=False)
            if (
                defer_classifications
                and event.kind in {"inference.completed", "enrichment.added"}
                and bean_ref not in self._planned
            ):
                previous = self._pending_registry_recovery.get(bean_ref)
                self._pending_registry_recovery[bean_ref] = (
                    _PendingRegistryRecovery(
                        record=(
                            record
                            if previous is None
                            or record.revision >= previous.record.revision
                            else previous.record
                        ),
                        # Give the already-sent acknowledged local message one
                        # scheduler quantum to arrive. This delay applies only
                        # when its bounded delivery retries were exhausted;
                        # normal direct decisions do not wait for it.
                        due_monotonic_ns=(
                            received_monotonic_ns or time.monotonic_ns()
                        )
                        + 5_000_000,
                    )
                )
                continue
            record = self._with_direct_evidence(record)
            sent_ns, direct_received_ns = self._direct_delivery_timing(record)
            direct_path = direct_received_ns is not None
            cached_context = self._sorting_contexts.get(bean_ref)
            context_path = cached_context is not None
            context_received_ns = (
                None
                if cached_context is None
                else cached_context.received_monotonic_ns
            )
            if cached_context is not None:
                record = _merge_sorting_context(
                    cached_context.record,
                    record,
                )
            self._consider(
                record,
                registry,
                arrival_monotonic_ns=received_monotonic_ns,
                direct_path=direct_path,
                direct_sent_monotonic_ns=sent_ns,
                direct_received_monotonic_ns=direct_received_ns,
                context_path=context_path,
                context_received_monotonic_ns=context_received_ns,
            )
        self._cursor = events[-1].stream_sequence

    def _consider(
        self,
        record: BeanRecord,
        registry: ZeroMQRegistryClient | None,
        *,
        arrival_monotonic_ns: int | None = None,
        force_deadline_fallback: bool = False,
        direct_path: bool = False,
        direct_sent_monotonic_ns: int | None = None,
        direct_received_monotonic_ns: int | None = None,
        context_path: bool = False,
        context_received_monotonic_ns: int | None = None,
    ) -> None:
        decision_started_ns = time.monotonic_ns()
        arrival_monotonic_ns = arrival_monotonic_ns or decision_started_ns
        if record.decision is not None:
            self._planned.add(record.bean_ref)
            self._awaiting_prediction.discard(record.bean_ref)
            self._awaiting_ensemble.pop(record.bean_ref, None)
            self._pending_registry_recovery.pop(record.bean_ref, None)
            self._clear_direct_evidence(record.bean_ref)
            self._sorting_contexts.pop(record.bean_ref, None)
            with self._recovery_watch_lock:
                self._recovery_watch.discard(record.bean_ref)
            if record.actuation is None and record.decision.gate_indices:
                self._schedule(
                    record,
                    registry,
                    session=self._session(record.bean_ref.run_id, registry),
                )
            return
        if record.bean_ref in self._planned:
            return
        pooled_to_persist = None
        classification = next(
            (
                item
                for item in reversed(record.enrichments)
                if item.kind in {CLASSIFICATION_POOLED, LEGACY_CLASSIFICATION}
            ),
            None,
        )
        if classification is None:
            evidence = evidence_for_ensemble(record.enrichments)
            if not evidence:
                return
            ensemble_id = classification_ensemble_id(evidence[0])
            expected_samples = expected_evidence_count(evidence)
            if len(evidence) >= expected_samples:
                candidate = pool_classification_evidence(
                    evidence[:expected_samples], deadline_fallback=False
                )
                if direct_path or registry is None:
                    record = replace(
                        record,
                        enrichments=(*record.enrichments, candidate),
                    )
                    classification = candidate
                    pooled_to_persist = candidate
                else:
                    assert registry is not None
                    record = registry.add_enrichment(
                        record.bean_ref,
                        candidate,
                        event_id=candidate.result_id,
                    )
                    arrival_monotonic_ns = time.monotonic_ns()
                    classification = pooled_for_ensemble(
                        record.enrichments, ensemble_id
                    )
            elif record.status == TrackStatus.CANCELLED:
                force_deadline_fallback = True
            elif record.prediction is None:
                self._awaiting_prediction.add(record.bean_ref)
                with self._recovery_watch_lock:
                    self._recovery_watch.add(record.bean_ref)
                return
            elif not force_deadline_fallback:
                deadline_ns = self._ensemble_deadline_monotonic_ns(
                    record,
                    registry,
                )
                if direct_path or arrival_monotonic_ns < deadline_ns:
                    # Direct evidence is always finalized by the post-drain
                    # deadline phase, even if its deadline has just passed.
                    # A second result which is already queued can therefore be
                    # merged before the fallback becomes immutable.
                    self._awaiting_ensemble[record.bean_ref] = _PendingEnsemble(
                        record,
                        deadline_ns,
                        direct_path,
                        direct_sent_monotonic_ns,
                        direct_received_monotonic_ns,
                        context_path,
                        context_received_monotonic_ns,
                    )
                    return
                force_deadline_fallback = True

            if classification is None and force_deadline_fallback:
                session = self._session(record.bean_ref.run_id, registry)
                fallback = pool_classification_evidence(
                    evidence,
                    deadline_fallback=True,
                    timestamp_ns=session.monotonic_to_source_ns(
                        arrival_monotonic_ns
                    ),
                )
                if direct_path or registry is None:
                    record = replace(
                        record,
                        enrichments=(*record.enrichments, fallback),
                    )
                    classification = fallback
                    pooled_to_persist = fallback
                else:
                    assert registry is not None
                    record = registry.add_enrichment(
                        record.bean_ref,
                        fallback,
                        event_id=fallback.result_id,
                    )
                    # Registry finalization is part of the recovery-path cost.
                    arrival_monotonic_ns = time.monotonic_ns()
                    classification = pooled_for_ensemble(
                        record.enrichments, ensemble_id
                    )
                if classification is not None and _is_deadline_fallback(
                    classification
                ):
                    self.deadline_fallbacks += 1
        if classification is None:
            return
        classification_basis = (
            classification_decision_basis(classification)
            if classification.kind == CLASSIFICATION_POOLED
            else None
        )
        self._awaiting_ensemble.pop(record.bean_ref, None)
        if classification.kind == CLASSIFICATION_POOLED:
            self.pooled_classifications += 1
        if record.status == TrackStatus.CANCELLED:
            self._record_cancelled_decision(
                record,
                classification.timestamp_ns,
                registry,
                arrival_monotonic_ns=arrival_monotonic_ns,
                pooled_to_persist=pooled_to_persist,
                classification_basis=classification_basis,
            )
            return
        prediction = record.prediction
        if prediction is None:
            self._awaiting_prediction.add(record.bean_ref)
            with self._recovery_watch_lock:
                self._recovery_watch.add(record.bean_ref)
            return
        self._awaiting_prediction.discard(record.bean_ref)
        with self._recovery_watch_lock:
            self._recovery_watch.discard(record.bean_ref)
        value = classification.value
        category = (
            str(value.get("category", "")) if isinstance(value, dict) else str(value)
        )
        confidence = classification.confidence or 0.0
        sample_count, expected_samples, deadline_fallback = (
            _classification_pool_details(classification)
        )
        should_sort = (
            category in self.settings.reject_categories
            and confidence >= self.settings.minimum_confidence
        )
        gates, combined_probability = (
            _select_gate_indices(
                prediction.gates,
                self.settings.gate_probability_threshold,
                allow_adjacent_pair=self.settings.allow_adjacent_gate_pair,
            )
            if should_sort
            else ((), None)
        )
        decision_timestamp = max(record.track.timestamp_ns, classification.timestamp_ns)
        open_timestamp = prediction.crossing_timestamp_ns - round(
            self.settings.open_lead_ms * 1_000_000
        )
        required_open_timestamp = open_timestamp
        close_timestamp = prediction.crossing_timestamp_ns + round(
            self.settings.close_lag_ms * 1_000_000
        )
        reason = "category accepted"
        low_confidence_defect = (
            category in self.settings.reject_categories
            and confidence < self.settings.minimum_confidence
        )
        if low_confidence_defect:
            reason = (
                f"reject category {category} below confidence threshold "
                f"({confidence:.3f} < {self.settings.minimum_confidence:.3f})"
            )
        if should_sort and not gates:
            reason = "no gate reached the probability threshold"
        elif should_sort and combined_probability is not None:
            reason = (
                f"reject category {category}; adjacent gates combined "
                f"probability {combined_probability:.3f}"
            )
        elif should_sort:
            reason = f"reject category {category}"
        if classification.kind == CLASSIFICATION_POOLED:
            reason += (
                f"; classification {'deadline fallback' if deadline_fallback else 'pooled'} "
                f"{sample_count}/{expected_samples}"
            )
        session = None
        notice_ns = open_timestamp - decision_timestamp
        observed_source_ns = decision_timestamp
        if gates:
            # Inference completion is source-timestamped before its Registry
            # event reaches this process. Use the scheduler's actual arrival
            # time as the final safety gate and reuse the clock snapshot below.
            session = self._session(record.bean_ref.run_id, registry)
            observed_source_ns = session.monotonic_to_source_ns(
                arrival_monotonic_ns
            )
            notice_ns = open_timestamp - observed_source_ns
        minimum_notice_ns = round(self.settings.minimum_notice_ms * 1_000_000)
        additional_notice_ns = max(0, minimum_notice_ns - notice_ns)
        if gates and notice_ns < round(self.settings.minimum_notice_ms * 1_000_000):
            gates = ()
            reason = "classification arrived too late for safe actuation"
            open_timestamp = decision_timestamp
            close_timestamp = decision_timestamp
        decision = SortingDecision(
            decision_id=f"sort:{record.bean_ref.run_id}:{record.bean_ref.sequence}",
            source="beano-sorter",
            timestamp_ns=decision_timestamp,
            actuation_timestamp_ns=max(decision_timestamp, open_timestamp),
            gate_indices=gates,
            policy_version=self.settings.policy_version,
            reason=reason,
            close_timestamp_ns=max(decision_timestamp, close_timestamp),
            crossing_timestamp_ns=prediction.crossing_timestamp_ns,
            based_on_revision=record.revision,
            timing_marks_ns={
                "sorter_event_received_monotonic_ns": arrival_monotonic_ns,
                "sorter_decision_started_monotonic_ns": decision_started_ns,
                "sorter_plan_ready_monotonic_ns": time.monotonic_ns(),
                "sorter_decision_request_monotonic_ns": time.monotonic_ns(),
                "sorter_observed_source_ns": observed_source_ns,
                "required_gate_open_source_ns": required_open_timestamp,
                "predicted_crossing_source_ns": prediction.crossing_timestamp_ns,
                "available_notice_ns": notice_ns,
                "minimum_notice_ns": minimum_notice_ns,
                "additional_notice_required_ns": additional_notice_ns,
                "classification_sample_count": sample_count,
                "classification_expected_samples": expected_samples,
                "classification_deadline_fallback": int(deadline_fallback),
                "classification_pooled_source_ns": classification.timestamp_ns,
                "classification_direct_path": int(direct_path),
                "direct_result_send_monotonic_ns": int(
                    direct_sent_monotonic_ns or 0
                ),
                "sorter_direct_received_monotonic_ns": int(
                    direct_received_monotonic_ns or 0
                ),
                "sorting_context_direct_path": int(context_path),
                "sorter_context_received_monotonic_ns": int(
                    context_received_monotonic_ns or 0
                ),
            },
        )
        planned = replace(record, decision=decision)
        self._planned.add(record.bean_ref)
        self._pending_registry_recovery.pop(record.bean_ref, None)
        self._clear_direct_evidence(record.bean_ref)
        self._sorting_contexts.pop(record.bean_ref, None)
        with self._recovery_watch_lock:
            self._recovery_watch.discard(record.bean_ref)
        if low_confidence_defect:
            self.low_confidence_defects += 1
        if decision.gate_indices:
            # Valve timing is a local real-time responsibility. Persist the
            # immutable audit record immediately afterwards, but do not put an
            # SQLite commit between an approved plan and its scheduler.
            self._schedule(planned, registry, session=session)
        self._queue_audit(
            _DecisionAudit(
                planned,
                category,
                confidence,
                pooled_enrichment=pooled_to_persist,
                classification_basis=classification_basis,
            ),
            registry,
        )

    def _ensemble_deadline_monotonic_ns(
        self,
        record: BeanRecord,
        registry: ZeroMQRegistryClient | None,
    ) -> int:
        prediction = record.prediction
        if prediction is None:
            return time.monotonic_ns()
        reserve_ms = (
            self.settings.open_lead_ms
            + self.settings.minimum_notice_ms
            + self.settings.ensemble_deadline_reserve_ms
        )
        deadline_source_ns = prediction.crossing_timestamp_ns - round(
            reserve_ms * 1_000_000
        )
        session = self._session(record.bean_ref.run_id, registry)
        mapped = session.source_to_monotonic_ns(deadline_source_ns)
        return time.monotonic_ns() if mapped is None else mapped

    def _ensemble_receive_timeout_ms(self) -> int:
        deadlines = [
            item.deadline_monotonic_ns
            for item in self._awaiting_ensemble.values()
        ]
        deadlines.extend(
            item.due_monotonic_ns
            for item in self._pending_registry_recovery.values()
        )
        if not deadlines:
            return 100
        now_ns = time.monotonic_ns()
        remaining_ns = min(deadlines) - now_ns
        return max(0, min(100, math.ceil(remaining_ns / 1_000_000)))

    def _release_due_registry_recoveries(
        self, registry: ZeroMQRegistryClient | None
    ) -> None:
        now_ns = time.monotonic_ns()
        due = tuple(
            (bean_ref, pending)
            for bean_ref, pending in self._pending_registry_recovery.items()
            if pending.due_monotonic_ns <= now_ns
        )
        for bean_ref, pending in due:
            self._pending_registry_recovery.pop(bean_ref, None)
            if bean_ref in self._planned:
                continue
            self.registry_recovery_decisions += 1
            self._consider(
                pending.record,
                registry,
                arrival_monotonic_ns=now_ns,
            )

    def _release_due_ensemble_fallbacks(
        self, registry: ZeroMQRegistryClient | None
    ) -> None:
        now_ns = time.monotonic_ns()
        due = tuple(
            (bean_ref, pending)
            for bean_ref, pending in self._awaiting_ensemble.items()
            if pending.deadline_monotonic_ns <= now_ns
        )
        additions = []
        for bean_ref, pending in due:
            self._awaiting_ensemble.pop(bean_ref, None)
            if pending.direct_path or registry is None:
                self._consider(
                    pending.record,
                    registry,
                    arrival_monotonic_ns=now_ns,
                    force_deadline_fallback=True,
                    direct_path=pending.direct_path,
                    direct_sent_monotonic_ns=(
                        pending.direct_sent_monotonic_ns
                    ),
                    direct_received_monotonic_ns=(
                        pending.direct_received_monotonic_ns
                    ),
                    context_path=pending.context_path,
                    context_received_monotonic_ns=(
                        pending.context_received_monotonic_ns
                    ),
                )
                continue
            evidence = evidence_for_ensemble(pending.record.enrichments)
            if not evidence:
                continue
            session = self._session(bean_ref.run_id, registry)
            fallback = pool_classification_evidence(
                evidence,
                deadline_fallback=True,
                timestamp_ns=session.monotonic_to_source_ns(now_ns),
            )
            additions.append((bean_ref, fallback, fallback.result_id))
        if not additions:
            return
        assert registry is not None
        try:
            records = registry.add_enrichments(tuple(additions))
        except RegistryRemoteError as exc:
            if not (
                exc.error_type == "ValueError"
                and "unknown registry operation: add_enrichments"
                in exc.remote_message
            ):
                raise
            records = tuple(
                registry.add_enrichment(
                    bean_ref,
                    enrichment,
                    event_id=event_id,
                )
                for bean_ref, enrichment, event_id in additions
            )
        finalized_ns = time.monotonic_ns()
        for record in records:
            classification = pooled_for_ensemble(record.enrichments)
            if classification is not None and _is_deadline_fallback(classification):
                self.deadline_fallbacks += 1
            self._consider(
                record,
                registry,
                arrival_monotonic_ns=finalized_ns,
                force_deadline_fallback=True,
            )

    def _record_cancelled_decision(
        self,
        record: BeanRecord,
        classification_timestamp_ns: int,
        registry: ZeroMQRegistryClient | None,
        *,
        arrival_monotonic_ns: int | None = None,
        pooled_to_persist: Enrichment | None = None,
        classification_basis: Enrichment | None = None,
    ) -> None:
        self._awaiting_prediction.discard(record.bean_ref)
        decision_timestamp = max(
            record.track.timestamp_ns, classification_timestamp_ns
        )
        decision = SortingDecision(
            decision_id=f"sort:{record.bean_ref.run_id}:{record.bean_ref.sequence}",
            source="beano-sorter",
            timestamp_ns=decision_timestamp,
            actuation_timestamp_ns=decision_timestamp,
            gate_indices=(),
            policy_version=self.settings.policy_version,
            reason="tentative track cancelled before trajectory confirmation",
            close_timestamp_ns=decision_timestamp,
            crossing_timestamp_ns=None,
            based_on_revision=record.revision,
            timing_marks_ns={
                "sorter_event_received_monotonic_ns": (
                    arrival_monotonic_ns or time.monotonic_ns()
                ),
                "sorter_decision_request_monotonic_ns": time.monotonic_ns(),
            },
        )
        self._planned.add(record.bean_ref)
        self._clear_direct_evidence(record.bean_ref)
        self._sorting_contexts.pop(record.bean_ref, None)
        with self._recovery_watch_lock:
            self._recovery_watch.discard(record.bean_ref)
        self._queue_audit(
            _DecisionAudit(
                replace(record, decision=decision),
                pooled_enrichment=pooled_to_persist,
                classification_basis=classification_basis,
            ),
            registry,
        )

    def _queue_audit(
        self,
        audit: _DecisionAudit,
        registry: ZeroMQRegistryClient | None,
    ) -> None:
        if self._threads:
            self._audit_queue.put(audit)
        else:
            if registry is None:
                raise RuntimeError("a Registry client is required for synchronous audit")
            self._persist_audit(audit, registry)

    def _audit_loop(self) -> None:
        lower_current_thread_priority()
        registry = ZeroMQRegistryClient(self.registry_endpoint, timeout_ms=2_000)
        try:
            while not self._stop.is_set() or not self._audit_queue.empty():
                try:
                    audit = self._audit_queue.get(timeout=0.05)
                except queue.Empty:
                    continue
                record = audit.record
                try:
                    self._persist_audit(audit, registry)
                except Exception as exc:  # noqa: BLE001 - bounded durable retry
                    self.errors += 1
                    decision = record.decision
                    if audit.attempts < 5 and not self._stop.is_set():
                        self._stop.wait(0.02 * (audit.attempts + 1))
                        self._audit_queue.put(
                            replace(audit, attempts=audit.attempts + 1)
                        )
                    else:
                        self._planned.discard(record.bean_ref)
                        with self._pending_lock:
                            pending = (
                                None
                                if decision is None
                                else self._pending.pop(decision.decision_id, None)
                            )
                        if (
                            pending is not None
                            and pending.opened_source_ns is not None
                            and decision is not None
                        ):
                            self._set_gates(decision.gate_indices, False)
                    self._emit("error", record, detail=str(exc))
                finally:
                    self._audit_queue.task_done()
        finally:
            registry.close()

    def _persist_audit(
        self, audit: _DecisionAudit, registry: ZeroMQRegistryClient
    ) -> None:
        record = audit.record
        decision = record.decision
        if decision is None:
            return
        if audit.classification_basis is not None:
            registry.add_enrichment(
                record.bean_ref,
                audit.classification_basis,
                event_id=audit.classification_basis.result_id,
            )
        if audit.pooled_enrichment is not None:
            pool = audit.pooled_enrichment
            sample_count, _expected, fallback = _classification_pool_details(pool)
            # Keep the finalization attempt idempotent without reusing the
            # canonical pool's event ID. If Registry has already finalized a
            # different pool in the second-result/deadline race, its
            # first-writer-wins rule can now return that pool without turning
            # this decision audit into a conflict.
            registry.add_enrichment(
                record.bean_ref,
                pool,
                event_id=(
                    f"sorter-finalize:{pool.result_id}:"
                    f"{sample_count}:{int(fallback)}"
                ),
            )
        decision = replace(
            decision,
            timing_marks_ns={
                **decision.timing_marks_ns,
                "sorter_decision_request_monotonic_ns": time.monotonic_ns(),
            },
        )
        updated = registry.set_sorting_decision(
            record.bean_ref,
            decision,
            event_id=decision.decision_id,
        )
        if not decision.gate_indices:
            updated = registry.acknowledge_sorting_decision(
                updated.bean_ref,
                decision.decision_id,
                decision.timestamp_ns,
                event_id=f"ack:{decision.decision_id}",
            )
        self.decisions += 1
        self._emit(
            "decision",
            updated,
            category=audit.category,
            confidence=audit.confidence,
            detail=decision.reason,
        )

    def _actuation_audit_loop(self) -> None:
        lower_current_thread_priority()
        registry = ZeroMQRegistryClient(self.registry_endpoint, timeout_ms=2_000)
        try:
            while not self._stop.is_set() or not self._actuation_audit_queue.empty():
                try:
                    audit = self._actuation_audit_queue.get(timeout=0.05)
                except queue.Empty:
                    continue
                try:
                    registry.record_actuation_ack(
                        audit.bean_ref,
                        audit.result,
                        event_id=f"actuation:{audit.result.decision_id}",
                    )
                    self.actuations += 1
                    self._emit(
                        "closed",
                        audit.record,
                        detail=audit.result.detail,
                    )
                except Exception as exc:  # noqa: BLE001 - bounded durable retry
                    self.errors += 1
                    if audit.attempts < 5:
                        self._stop.wait(0.02 * (audit.attempts + 1))
                        self._actuation_audit_queue.put(
                            replace(audit, attempts=audit.attempts + 1)
                        )
                    else:
                        self._emit("error", audit.record, detail=str(exc))
                    registry.close()
                finally:
                    self._actuation_audit_queue.task_done()
        finally:
            registry.close()

    def _session(
        self,
        run_id: str,
        registry: ZeroMQRegistryClient | None,
    ) -> RunSession:
        session = self._sessions.get(run_id)
        if session is None:
            if registry is None:
                raise RuntimeError(f"run clock {run_id!r} is not available")
            session = registry.get_session(run_id)
            self._sessions[run_id] = session
        return session

    def _schedule(
        self,
        record: BeanRecord,
        registry: ZeroMQRegistryClient | None,
        *,
        session: RunSession | None = None,
    ) -> None:
        decision = record.decision
        if decision is None or not decision.gate_indices:
            return
        if self.actuation_endpoint:
            if decision.decision_id in self._externally_scheduled:
                return
            if session is None:
                session = self._session(record.bean_ref.run_id, registry)
            pending = _pending_actuation(record, session, time.monotonic_ns())
            plan = _external_actuation_plan(pending)
            self._externally_scheduled.add(decision.decision_id)
            try:
                self._external_actuation_queue.put_nowait(
                    _ExternalActuation(record, session, plan)
                )
            except queue.Full:
                self._externally_scheduled.discard(decision.decision_id)
                self.external_plans_rejected += 1
                self._queue_external_failure(
                    record,
                    session,
                    "external actuator plan queue is full",
                )
            return
        with self._pending_lock:
            if decision.decision_id in self._pending:
                return
        if session is None:
            session = self._session(record.bean_ref.run_id, registry)
        pending = _pending_actuation(record, session, time.monotonic_ns())
        with self._actuator_condition:
            self._pending.setdefault(decision.decision_id, pending)
            self._actuator_condition.notify()

    def _external_actuator_loop(self) -> None:
        publisher = ZeroMQActuationPlanPublisher(self.actuation_endpoint)
        try:
            while not self._stop.is_set() or not self._external_actuation_queue.empty():
                try:
                    pending = self._external_actuation_queue.get(timeout=0.05)
                except queue.Empty:
                    continue
                try:
                    receipt = publisher.submit(pending.plan)
                    if receipt.accepted:
                        self.external_plans_accepted += 1
                        self._emit(
                            "scheduled",
                            pending.record,
                            detail=(
                                "ESP32 actuator accepted plan"
                                + (f" · {receipt.detail}" if receipt.detail else "")
                            ),
                        )
                    else:
                        self.external_plans_rejected += 1
                        self._queue_external_failure(
                            pending.record,
                            pending.session,
                            receipt.detail or "ESP32 actuator rejected plan",
                        )
                except Exception as exc:  # noqa: BLE001 - fail safe and auditable
                    self.external_plans_rejected += 1
                    self._queue_external_failure(
                        pending.record,
                        pending.session,
                        str(exc),
                    )
                    publisher.close()
                    publisher = ZeroMQActuationPlanPublisher(
                        self.actuation_endpoint
                    )
                finally:
                    self._external_actuation_queue.task_done()
        finally:
            publisher.close()

    def _queue_external_failure(
        self,
        record: BeanRecord,
        session: RunSession,
        detail: str,
    ) -> None:
        decision = record.decision
        if decision is None:
            return
        now_source = session.monotonic_to_source_ns(time.monotonic_ns())
        result = ActuationResult(
            decision_id=decision.decision_id,
            source="beano-actuator-transport",
            actual_open_timestamp_ns=now_source,
            actual_close_timestamp_ns=now_source,
            success=False,
            detail=f"external actuator plan failed: {detail}",
        )
        self._actuation_audit_queue.put(
            _ActuationAudit(record.bean_ref, record, result)
        )
        self._emit("error", record, detail=result.detail)

    def _actuator_loop(self) -> None:
        while not self._stop.is_set():
            opened_records: list[BeanRecord] = []
            audits: list[_ActuationAudit] = []
            with self._actuator_condition:
                while not self._stop.is_set():
                    now_monotonic_ns = time.monotonic_ns()
                    next_deadline_ns = None
                    for decision_id, original in tuple(self._pending.items()):
                        scheduled = original
                        record = scheduled.record
                        decision = record.decision
                        if decision is None:
                            self._pending.pop(decision_id, None)
                            continue
                        if scheduled.open_monotonic_ns is None:
                            scheduled = _pending_actuation(
                                record,
                                scheduled.session,
                                now_monotonic_ns,
                            )
                        opened_at = scheduled.opened_source_ns
                        if (
                            opened_at is None
                            and scheduled.open_monotonic_ns is not None
                            and now_monotonic_ns >= scheduled.open_monotonic_ns
                        ):
                            self._set_gates(decision.gate_indices, True)
                            opened_at = scheduled.session.monotonic_to_source_ns(
                                now_monotonic_ns
                            )
                            scheduled = replace(
                                scheduled,
                                opened_source_ns=opened_at,
                            )
                            opened_records.append(record)
                        if (
                            opened_at is not None
                            and scheduled.close_monotonic_ns is not None
                            and now_monotonic_ns >= scheduled.close_monotonic_ns
                        ):
                            now_source = scheduled.session.monotonic_to_source_ns(
                                now_monotonic_ns
                            )
                            self._set_gates(decision.gate_indices, False)
                            success, detail = _actuation_timing_result(
                                decision,
                                opened_at,
                                now_source,
                            )
                            open_lateness_ms = (
                                opened_at - decision.actuation_timestamp_ns
                            ) / 1_000_000.0
                            close_target = (
                                decision.close_timestamp_ns
                                if decision.close_timestamp_ns is not None
                                else decision.actuation_timestamp_ns
                            )
                            close_lateness_ms = (
                                now_source - close_target
                            ) / 1_000_000.0
                            detail += (
                                f"; scheduler lateness open "
                                f"{open_lateness_ms:.3f} ms, close "
                                f"{close_lateness_ms:.3f} ms"
                            )
                            result = ActuationResult(
                                decision_id=decision_id,
                                source="virtual-actuator-deadline",
                                actual_open_timestamp_ns=opened_at,
                                actual_close_timestamp_ns=now_source,
                                success=success,
                                detail=detail,
                            )
                            self._pending.pop(decision_id, None)
                            audits.append(
                                _ActuationAudit(record.bean_ref, record, result)
                            )
                            continue
                        self._pending[decision_id] = scheduled
                        candidate = (
                            scheduled.open_monotonic_ns
                            if opened_at is None
                            else scheduled.close_monotonic_ns
                        )
                        if candidate is not None:
                            next_deadline_ns = (
                                candidate
                                if next_deadline_ns is None
                                else min(next_deadline_ns, candidate)
                            )
                    if opened_records or audits:
                        break
                    timeout = (
                        None
                        if next_deadline_ns is None
                        else max(
                            0.0,
                            (next_deadline_ns - time.monotonic_ns())
                            / 1_000_000_000.0,
                        )
                    )
                    self._actuator_condition.wait(timeout=timeout)
            for record in opened_records:
                self._emit("opened", record)
            for audit in audits:
                self._actuation_audit_queue.put(audit)

    def _set_gates(self, gates: tuple[int, ...], active: bool) -> None:
        with self._pending_lock:
            for gate in gates:
                current = self._gate_counts.get(gate, 0)
                self._gate_counts[gate] = current + 1 if active else max(0, current - 1)

    def _emit(
        self,
        kind: str,
        record: BeanRecord | None = None,
        *,
        category: str = "",
        confidence: float | None = None,
        detail: str = "",
    ) -> None:
        if self.activity is None:
            return
        decision = None if record is None else record.decision
        self.activity(
            SorterActivity(
                kind=kind,
                bean_id="" if record is None else str(record.bean_ref),
                decision_id="" if decision is None else decision.decision_id,
                gate_indices=() if decision is None else decision.gate_indices,
                category=category,
                confidence=confidence,
                detail=detail,
            )
        )


def _embedded_record(event: BeanEvent) -> BeanRecord | None:
    value = event.payload.get("record")
    if not isinstance(value, Mapping):
        return None
    try:
        return record_from_dict(value)
    except (KeyError, TypeError, ValueError):
        return None


def _sorter_poller(
    subscriber: ZeroMQRegistrySubscriber,
    direct: ZeroMQDirectEvidenceReceiver | None,
    contexts: ZeroMQSortingContextReceiver | None,
) -> zmq.Poller:
    poller = zmq.Poller()
    poller.register(subscriber.socket, zmq.POLLIN)
    if direct is not None:
        poller.register(direct.socket, zmq.POLLIN)
    if contexts is not None:
        poller.register(contexts.socket, zmq.POLLIN)
    return poller


def _control_poller(
    direct: ZeroMQDirectEvidenceReceiver | None,
    contexts: ZeroMQSortingContextReceiver | None,
) -> zmq.Poller:
    poller = zmq.Poller()
    if direct is not None:
        poller.register(direct.socket, zmq.POLLIN)
    if contexts is not None:
        poller.register(contexts.socket, zmq.POLLIN)
    return poller


def _merge_sorting_context(
    context: BeanRecord,
    state: BeanRecord,
) -> BeanRecord:
    """Use the newest track/prediction without losing local inference state."""

    return replace(
        context,
        revision=max(context.revision, state.revision),
        created_timestamp_ns=min(
            context.created_timestamp_ns,
            state.created_timestamp_ns,
        ),
        updated_timestamp_ns=max(
            context.updated_timestamp_ns,
            state.updated_timestamp_ns,
        ),
        enrichments=state.enrichments,
        decision=state.decision,
        inference_jobs=state.inference_jobs,
        actuation=state.actuation,
    )


def _classification_pool_details(
    classification: Enrichment,
) -> tuple[int, int, bool]:
    if classification.kind != CLASSIFICATION_POOLED:
        return 1, 1, False
    value = classification.value
    if not isinstance(value, Mapping):
        return 1, 1, False
    ensemble = value.get("ensemble")
    if not isinstance(ensemble, Mapping):
        return 1, 1, False
    return (
        int(ensemble.get("sample_count", 1)),
        int(ensemble.get("expected_samples", 1)),
        bool(ensemble.get("deadline_fallback", False)),
    )


def _is_deadline_fallback(classification: Enrichment) -> bool:
    return _classification_pool_details(classification)[2]


def _select_gate_indices(
    gates: tuple[GateProbability, ...],
    threshold: float,
    *,
    allow_adjacent_pair: bool,
) -> tuple[tuple[int, ...], float | None]:
    individual = tuple(
        item.gate.index for item in gates if item.probability >= threshold
    )
    if individual or not allow_adjacent_pair or len(gates) < 2:
        return individual, None
    candidates = tuple(
        (left.probability + right.probability, left.gate.index, right.gate.index)
        for left, right in pairwise(gates)
        if right.gate.index == left.gate.index + 1
    )
    if not candidates:
        return (), None
    probability, left_index, right_index = max(candidates)
    if probability < threshold:
        return (), None
    return (left_index, right_index), probability


def _pending_actuation(
    record: BeanRecord,
    session: RunSession,
    now_monotonic_ns: int,
) -> _PendingActuation:
    decision = record.decision
    if decision is None:
        raise ValueError("pending actuation requires a sorting decision")
    close_source_ns = (
        decision.close_timestamp_ns
        if decision.close_timestamp_ns is not None
        else decision.actuation_timestamp_ns
    )
    if session.target_fps <= 0:
        open_monotonic_ns = now_monotonic_ns
        close_monotonic_ns = now_monotonic_ns
    else:
        open_monotonic_ns = session.source_to_monotonic_ns(
            decision.actuation_timestamp_ns
        )
        close_monotonic_ns = session.source_to_monotonic_ns(close_source_ns)
    return _PendingActuation(
        record,
        session,
        open_monotonic_ns,
        close_monotonic_ns,
    )


def _external_actuation_plan(pending: _PendingActuation) -> ActuationPlan:
    decision = pending.record.decision
    if decision is None or decision.crossing_timestamp_ns is None:
        raise ValueError("external actuation requires a predicted crossing")
    if pending.open_monotonic_ns is None or pending.close_monotonic_ns is None:
        raise ValueError("external actuation requires a running replay clock")
    crossing_monotonic_ns = pending.session.source_to_monotonic_ns(
        decision.crossing_timestamp_ns
    )
    if crossing_monotonic_ns is None:
        raise ValueError("external actuation crossing cannot be mapped to host time")
    close_source_ns = (
        decision.close_timestamp_ns
        if decision.close_timestamp_ns is not None
        else decision.actuation_timestamp_ns
    )
    plan = ActuationPlan(
        decision_id=decision.decision_id,
        bean_ref=pending.record.bean_ref,
        gate_indices=decision.gate_indices,
        open_monotonic_ns=pending.open_monotonic_ns,
        close_monotonic_ns=pending.close_monotonic_ns,
        crossing_monotonic_ns=crossing_monotonic_ns,
        open_source_ns=decision.actuation_timestamp_ns,
        close_source_ns=close_source_ns,
        crossing_source_ns=decision.crossing_timestamp_ns,
        run_clock_source_ns=pending.session.clock_source_timestamp_ns,
        run_clock_monotonic_ns=pending.session.clock_monotonic_ns,
        run_clock_scale_ppb=round(
            pending.session.playback_scale * 1_000_000_000
        ),
    )
    plan.validate()
    return plan


def _actuation_timing_result(
    decision: SortingDecision,
    actual_open_timestamp_ns: int,
    actual_close_timestamp_ns: int,
) -> tuple[bool, str]:
    crossing = decision.crossing_timestamp_ns
    if crossing is None:
        return False, "timing miss: decision has no predicted crossing"
    if actual_open_timestamp_ns <= crossing <= actual_close_timestamp_ns:
        return True, "simulated gate was active at the predicted crossing"
    if actual_open_timestamp_ns > crossing:
        late_ms = (actual_open_timestamp_ns - crossing) / 1_000_000.0
        return False, f"timing miss: gate opened {late_ms:.3f} ms after crossing"
    early_ms = (crossing - actual_close_timestamp_ns) / 1_000_000.0
    return False, f"timing miss: gate closed {early_ms:.3f} ms before crossing"


def _recovery_run_ids(sessions: tuple[RunSession, ...]) -> tuple[str, ...]:
    if not sessions:
        return ()
    latest = max(
        sessions,
        key=lambda session: (
            session.updated_timestamp_ns,
            session.created_timestamp_ns,
            session.run_id,
        ),
    )
    live_states = {RunState.CREATED, RunState.RUNNING, RunState.PAUSED}
    selected = {session.run_id for session in sessions if session.state in live_states}
    selected.add(latest.run_id)
    return tuple(
        session.run_id
        for session in sorted(
            sessions,
            key=lambda session: (
                session.updated_timestamp_ns,
                session.created_timestamp_ns,
                session.run_id,
            ),
        )
        if session.run_id in selected
    )
