"""Durable classification policy and virtual valve scheduling service."""

from __future__ import annotations

import math
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from .registry_models import (
    ActuationResult,
    BeanRecord,
    RunState,
    SortingDecision,
)
from .registry_service import DEFAULT_COMMAND_ENDPOINT
from .registry_zmq import ZeroMQRegistryClient


@dataclass(frozen=True, slots=True)
class SorterSettings:
    reject_categories: tuple[str, ...] = ("insect_damage", "mould", "broken")
    minimum_confidence: float = 0.75
    gate_probability_threshold: float = 0.35
    open_lead_ms: float = 8.0
    close_lag_ms: float = 12.0
    minimum_notice_ms: float = 4.0
    policy_version: str = "simulation-v1"

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


class SorterService:
    """Recoverable decision consumer with a separate virtual actuator loop."""

    def __init__(
        self,
        *,
        registry_endpoint: str = DEFAULT_COMMAND_ENDPOINT,
        settings: SorterSettings | None = None,
        activity: Callable[[SorterActivity], None] | None = None,
    ) -> None:
        self.registry_endpoint = registry_endpoint
        self.settings = settings or SorterSettings()
        self.settings.validate()
        self.activity = activity
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._pending: dict[str, tuple[BeanRecord, int | None]] = {}
        self._pending_lock = threading.RLock()
        self._gate_counts: dict[int, int] = {}
        self._cursor = 0
        self.decisions = 0
        self.actuations = 0
        self.errors = 0

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
        first = True
        try:
            while not self._stop.is_set():
                try:
                    if first:
                        for record in registry.list_records():
                            self._consider(record, registry)
                        first = False
                    events = registry.events_since(self._cursor, limit=500)
                    if not events:
                        self._stop.wait(0.025)
                        continue
                    for event in events:
                        self._cursor = event.stream_sequence
                        self._consider(registry.get(event.bean_ref), registry)
                except Exception as exc:  # noqa: BLE001 - reconnect loop is intentional
                    self.errors += 1
                    self._emit("error", detail=str(exc))
                    registry.close()
                    self._stop.wait(0.25)
        finally:
            registry.close()

    def _consider(self, record: BeanRecord, registry: ZeroMQRegistryClient) -> None:
        if record.decision is not None:
            if record.actuation is None:
                self._schedule(record)
            return
        classification = next(
            (
                item
                for item in reversed(record.enrichments)
                if item.kind == "classification"
            ),
            None,
        )
        prediction = record.prediction
        if classification is None or prediction is None:
            return
        value = classification.value
        category = (
            str(value.get("category", "")) if isinstance(value, dict) else str(value)
        )
        confidence = classification.confidence or 0.0
        should_sort = (
            category in self.settings.reject_categories
            and confidence >= self.settings.minimum_confidence
        )
        gates = (
            tuple(
                item.gate.index
                for item in prediction.gates
                if item.probability >= self.settings.gate_probability_threshold
            )
            if should_sort
            else ()
        )
        decision_timestamp = max(record.track.timestamp_ns, classification.timestamp_ns)
        open_timestamp = prediction.crossing_timestamp_ns - round(
            self.settings.open_lead_ms * 1_000_000
        )
        close_timestamp = prediction.crossing_timestamp_ns + round(
            self.settings.close_lag_ms * 1_000_000
        )
        reason = "category accepted"
        if should_sort and not gates:
            reason = "no gate reached the probability threshold"
        elif should_sort:
            reason = f"reject category {category}"
        notice_ns = open_timestamp - decision_timestamp
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
        )
        updated = registry.set_sorting_decision(
            record.bean_ref,
            decision,
            event_id=decision.decision_id,
        )
        self.decisions += 1
        self._emit(
            "decision",
            updated,
            category=category,
            confidence=confidence,
            detail=reason,
        )
        if decision.gate_indices:
            self._schedule(updated)
        else:
            registry.acknowledge_sorting_decision(
                updated.bean_ref,
                decision.decision_id,
                decision.timestamp_ns,
                event_id=f"ack:{decision.decision_id}",
            )

    def _schedule(self, record: BeanRecord) -> None:
        decision = record.decision
        if decision is None or not decision.gate_indices:
            return
        with self._pending_lock:
            self._pending.setdefault(decision.decision_id, (record, None))

    def _actuator_loop(self) -> None:
        registry = ZeroMQRegistryClient(self.registry_endpoint, timeout_ms=2_000)
        try:
            while not self._stop.is_set():
                with self._pending_lock:
                    pending = tuple(self._pending.items())
                for decision_id, (record, opened_at) in pending:
                    decision = record.decision
                    if decision is None:
                        continue
                    try:
                        session = registry.get_session(record.bean_ref.run_id)
                        if session.state == RunState.PAUSED:
                            continue
                        now_source = (
                            decision.close_timestamp_ns
                            if session.target_fps <= 0
                            else session.monotonic_to_source_ns(time.monotonic_ns())
                        )
                        if (
                            opened_at is None
                            and now_source >= decision.actuation_timestamp_ns
                        ):
                            self._set_gates(decision.gate_indices, True)
                            opened_at = now_source
                            with self._pending_lock:
                                self._pending[decision_id] = (record, opened_at)
                            self._emit("opened", record)
                        close_timestamp = (
                            decision.close_timestamp_ns
                            if decision.close_timestamp_ns is not None
                            else decision.actuation_timestamp_ns
                        )
                        if opened_at is not None and now_source >= close_timestamp:
                            self._set_gates(decision.gate_indices, False)
                            result = ActuationResult(
                                decision_id=decision_id,
                                source="virtual-actuator",
                                actual_open_timestamp_ns=opened_at,
                                actual_close_timestamp_ns=now_source,
                                success=True,
                                detail="simulated gate cycle",
                            )
                            registry.record_actuation(
                                record.bean_ref,
                                result,
                                event_id=f"actuation:{decision_id}",
                            )
                            with self._pending_lock:
                                self._pending.pop(decision_id, None)
                            self.actuations += 1
                            self._emit("closed", record)
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
