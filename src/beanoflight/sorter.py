"""Durable classification policy and virtual valve scheduling service."""

from __future__ import annotations

import math
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from itertools import pairwise

from .models import BeanEvent, BeanRef, GateProbability, TrackStatus
from .registry_models import (
    ActuationResult,
    BeanRecord,
    RunSession,
    RunState,
    SortingDecision,
    record_from_dict,
)
from .registry_service import DEFAULT_COMMAND_ENDPOINT, DEFAULT_EVENT_ENDPOINT
from .registry_zmq import ZeroMQRegistryClient, ZeroMQRegistrySubscriber


@dataclass(frozen=True, slots=True)
class SorterSettings:
    reject_categories: tuple[str, ...] = ("insect_damage", "mould", "broken")
    minimum_confidence: float = 0.75
    gate_probability_threshold: float = 0.35
    open_lead_ms: float = 8.0
    close_lag_ms: float = 12.0
    minimum_notice_ms: float = 4.0
    allow_adjacent_gate_pair: bool = True
    policy_version: str = "simulation-v2"

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
        timing = (self.open_lead_ms, self.close_lag_ms, self.minimum_notice_ms)
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


class SorterService:
    """Recoverable decision consumer with a separate virtual actuator loop."""

    def __init__(
        self,
        *,
        registry_endpoint: str = DEFAULT_COMMAND_ENDPOINT,
        event_endpoint: str = DEFAULT_EVENT_ENDPOINT,
        settings: SorterSettings | None = None,
        activity: Callable[[SorterActivity], None] | None = None,
    ) -> None:
        self.registry_endpoint = registry_endpoint
        self.event_endpoint = event_endpoint
        self.settings = settings or SorterSettings()
        self.settings.validate()
        self.activity = activity
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._pending: dict[str, _PendingActuation] = {}
        self._pending_lock = threading.RLock()
        self._awaiting_prediction: set[BeanRef] = set()
        self._gate_counts: dict[int, int] = {}
        self._cursor = 0
        self.decisions = 0
        self.actuations = 0
        self.errors = 0
        self.event_notifications = 0
        self.fallback_journal_queries = 0

    @property
    def gate_states(self) -> dict[int, bool]:
        with self._pending_lock:
            return {gate: count > 0 for gate, count in self._gate_counts.items()}

    def start(self) -> None:
        if self._threads:
            return
        for name, target in (
            ("beano-sorter-decisions", self._decision_loop),
            ("beano-virtual-actuator", self._actuator_loop),
        ):
            thread = threading.Thread(target=target, name=name, daemon=True)
            self._threads.append(thread)
            thread.start()

    def close(self) -> None:
        self._stop.set()
        for thread in self._threads:
            thread.join(2.0)
        self._threads.clear()

    def _decision_loop(self) -> None:
        registry = ZeroMQRegistryClient(self.registry_endpoint, timeout_ms=2_000)
        subscriber = ZeroMQRegistrySubscriber(self.event_endpoint)
        first = True
        last_fallback = time.monotonic()
        try:
            while not self._stop.is_set():
                try:
                    if first:
                        self._recover_current_state(registry)
                        first = False
                    event = subscriber.receive(timeout_ms=100)
                    if event is not None:
                        self.event_notifications += 1
                        received_ns = time.monotonic_ns()
                        if event.stream_sequence > self._cursor + 1:
                            self._catch_up(registry, received_ns)
                        elif event.stream_sequence > self._cursor:
                            self._process_events(
                                (event,),
                                registry,
                                received_monotonic_ns=received_ns,
                                use_embedded_state=True,
                            )
                        continue
                    # PUB/SUB is deliberately replaceable. The durable journal is
                    # a low-rate recovery path, not the normal decision trigger.
                    if time.monotonic() - last_fallback >= 0.25:
                        self._catch_up(registry, time.monotonic_ns())
                        last_fallback = time.monotonic()
                except Exception as exc:  # noqa: BLE001 - reconnect loop is intentional
                    self.errors += 1
                    self._emit("error", detail=str(exc))
                    registry.close()
                    subscriber.close()
                    self._stop.wait(0.25)
                    registry = ZeroMQRegistryClient(
                        self.registry_endpoint, timeout_ms=2_000
                    )
                    subscriber = ZeroMQRegistrySubscriber(self.event_endpoint)
                    first = True
        finally:
            subscriber.close()
            registry.close()

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
        for run_id in _recovery_run_ids(sessions):
            for record in registry.list_records(run_id=run_id):
                self._consider(record, registry)

    def _process_events(
        self,
        events: tuple[BeanEvent, ...],
        registry: ZeroMQRegistryClient,
        *,
        received_monotonic_ns: int | None = None,
        use_embedded_state: bool = False,
    ) -> None:
        # A classification is the normal decision trigger. Track updates only
        # matter for the rare classified bean which did not yet have a trajectory.
        selected_events = tuple(
            event
            for event in events
            if event.kind in {"inference.completed", "enrichment.added"}
            or (
                event.bean_ref in self._awaiting_prediction
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
            self._consider(
                record,
                registry,
                arrival_monotonic_ns=received_monotonic_ns,
            )
        self._cursor = events[-1].stream_sequence

    def _consider(
        self,
        record: BeanRecord,
        registry: ZeroMQRegistryClient,
        *,
        arrival_monotonic_ns: int | None = None,
    ) -> None:
        decision_started_ns = time.monotonic_ns()
        arrival_monotonic_ns = arrival_monotonic_ns or decision_started_ns
        if record.decision is not None:
            self._awaiting_prediction.discard(record.bean_ref)
            if record.actuation is None:
                self._schedule(record, registry)
            return
        classification = next(
            (
                item
                for item in reversed(record.enrichments)
                if item.kind == "classification"
            ),
            None,
        )
        if classification is None:
            return
        if record.status == TrackStatus.CANCELLED:
            self._record_cancelled_decision(
                record,
                classification.timestamp_ns,
                registry,
                arrival_monotonic_ns=arrival_monotonic_ns,
            )
            return
        prediction = record.prediction
        if prediction is None:
            self._awaiting_prediction.add(record.bean_ref)
            return
        self._awaiting_prediction.discard(record.bean_ref)
        value = classification.value
        category = (
            str(value.get("category", "")) if isinstance(value, dict) else str(value)
        )
        confidence = classification.confidence or 0.0
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
        if should_sort and not gates:
            reason = "no gate reached the probability threshold"
        elif should_sort and combined_probability is not None:
            reason = (
                f"reject category {category}; adjacent gates combined "
                f"probability {combined_probability:.3f}"
            )
        elif should_sort:
            reason = f"reject category {category}"
        session = None
        notice_ns = open_timestamp - decision_timestamp
        observed_source_ns = decision_timestamp
        if gates:
            # Inference completion is source-timestamped before its Registry
            # event reaches this process. Use the scheduler's actual arrival
            # time as the final safety gate and reuse the clock snapshot below.
            session = registry.get_session(record.bean_ref.run_id)
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
            },
        )
        planned = replace(record, decision=decision)
        if decision.gate_indices:
            # Valve timing is a local real-time responsibility. Persist the
            # immutable audit record immediately afterwards, but do not put an
            # SQLite commit between an approved plan and its scheduler.
            self._schedule(planned, registry, session=session)
        try:
            updated = registry.set_sorting_decision(
                record.bean_ref,
                decision,
                event_id=decision.decision_id,
            )
        except Exception:
            with self._pending_lock:
                pending = self._pending.pop(decision.decision_id, None)
            if pending is not None and pending.opened_source_ns is not None:
                self._set_gates(decision.gate_indices, False)
            raise
        self.decisions += 1
        self._emit(
            "decision",
            updated,
            category=category,
            confidence=confidence,
            detail=reason,
        )
        if not decision.gate_indices:
            registry.acknowledge_sorting_decision(
                updated.bean_ref,
                decision.decision_id,
                decision.timestamp_ns,
                event_id=f"ack:{decision.decision_id}",
            )

    def _record_cancelled_decision(
        self,
        record: BeanRecord,
        classification_timestamp_ns: int,
        registry: ZeroMQRegistryClient,
        *,
        arrival_monotonic_ns: int | None = None,
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
        updated = registry.set_sorting_decision(
            record.bean_ref,
            decision,
            event_id=decision.decision_id,
        )
        registry.acknowledge_sorting_decision(
            updated.bean_ref,
            decision.decision_id,
            decision.timestamp_ns,
            event_id=f"ack:{decision.decision_id}",
        )
        self.decisions += 1
        self._emit("decision", updated, detail=decision.reason)

    def _schedule(
        self,
        record: BeanRecord,
        registry: ZeroMQRegistryClient,
        *,
        session: RunSession | None = None,
    ) -> None:
        decision = record.decision
        if decision is None or not decision.gate_indices:
            return
        with self._pending_lock:
            if decision.decision_id in self._pending:
                return
        if session is None:
            session = registry.get_session(record.bean_ref.run_id)
        pending = _pending_actuation(record, session, time.monotonic_ns())
        with self._pending_lock:
            self._pending.setdefault(decision.decision_id, pending)

    def _actuator_loop(self) -> None:
        registry = ZeroMQRegistryClient(self.registry_endpoint, timeout_ms=2_000)
        try:
            while not self._stop.is_set():
                with self._pending_lock:
                    pending = tuple(self._pending.items())
                for decision_id, scheduled in pending:
                    record = scheduled.record
                    decision = record.decision
                    if decision is None:
                        continue
                    try:
                        if scheduled.open_monotonic_ns is None:
                            session = registry.get_session(record.bean_ref.run_id)
                            scheduled = _pending_actuation(
                                record, session, time.monotonic_ns()
                            )
                            with self._pending_lock:
                                self._pending[decision_id] = scheduled
                            if scheduled.open_monotonic_ns is None:
                                continue
                        now_monotonic_ns = time.monotonic_ns()
                        now_source = scheduled.session.monotonic_to_source_ns(
                            now_monotonic_ns
                        )
                        opened_at = scheduled.opened_source_ns
                        if (
                            opened_at is None
                            and now_monotonic_ns >= scheduled.open_monotonic_ns
                        ):
                            self._set_gates(decision.gate_indices, True)
                            opened_at = now_source
                            scheduled = replace(
                                scheduled, opened_source_ns=opened_at
                            )
                            with self._pending_lock:
                                self._pending[decision_id] = scheduled
                            self._emit("opened", record)
                        if (
                            opened_at is not None
                            and scheduled.close_monotonic_ns is not None
                            and now_monotonic_ns >= scheduled.close_monotonic_ns
                        ):
                            self._set_gates(decision.gate_indices, False)
                            success, detail = _actuation_timing_result(
                                decision, opened_at, now_source
                            )
                            result = ActuationResult(
                                decision_id=decision_id,
                                source="virtual-actuator",
                                actual_open_timestamp_ns=opened_at,
                                actual_close_timestamp_ns=now_source,
                                success=success,
                                detail=detail,
                            )
                            registry.record_actuation(
                                record.bean_ref,
                                result,
                                event_id=f"actuation:{decision_id}",
                            )
                            with self._pending_lock:
                                self._pending.pop(decision_id, None)
                            self.actuations += 1
                            self._emit("closed", record, detail=detail)
                    except Exception as exc:  # noqa: BLE001 - retain plan and retry
                        self.errors += 1
                        self._emit("error", record, detail=str(exc))
                        registry.close()
                self._stop.wait(0.002)
        finally:
            registry.close()

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
