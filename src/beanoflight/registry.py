"""Authoritative, revisioned hot-state registry for live bean processing."""

from __future__ import annotations

import hashlib
import json
import threading
import uuid
from collections import OrderedDict, deque
from collections.abc import Sequence
from contextlib import AbstractContextManager, nullcontext
from dataclasses import replace
from typing import Protocol

from .events import EventBus
from .models import BeanEvent, BeanRef, CrossingPrediction, TrackSnapshot, TrackStatus
from .registry_models import (
    BeanRecord,
    Enrichment,
    SortingDecision,
    decision_to_dict,
    enrichment_to_dict,
    observation_to_dict,
    prediction_to_dict,
    record_to_dict,
    track_to_dict,
)


class RegistryError(RuntimeError):
    pass


class BeanNotFoundError(RegistryError):
    pass


class StaleRegistryUpdateError(RegistryError):
    pass


class RegistryConflictError(RegistryError):
    pass


class RegistryHistoryGapError(RegistryError):
    pass


class RegistryRepository(Protocol):
    def save(self, record: BeanRecord, event: BeanEvent) -> int: ...

    def load(self, bean_ref: BeanRef) -> BeanRecord | None: ...

    def list_records(
        self,
        *,
        run_id: str | None = None,
        statuses: Sequence[TrackStatus] | None = None,
    ) -> tuple[BeanRecord, ...]: ...

    def event_identity(self, event_id: str) -> tuple[BeanRef, str] | None: ...

    def events_since(
        self, after_sequence: int, *, limit: int = 1_000
    ) -> tuple[BeanEvent, ...]: ...

    def batch(self) -> AbstractContextManager[None]: ...


class BeanRegistry:
    """Single-writer materialized state with validated worker enrichments."""

    def __init__(
        self,
        repository: RegistryRepository | None = None,
        *,
        events: EventBus | None = None,
        idempotency_capacity: int = 8_192,
        event_history_capacity: int = 8_192,
    ) -> None:
        self.repository = repository
        self.events = events or EventBus()
        self._lock = threading.RLock()
        self._records: dict[BeanRef, BeanRecord] = {}
        self._processed: OrderedDict[str, tuple[BeanRef, str]] = OrderedDict()
        self._idempotency_capacity = max(128, int(idempotency_capacity))
        self._journal: deque[BeanEvent] = deque(
            maxlen=max(128, int(event_history_capacity))
        )
        self._stream_sequence = 0

    def subscribe(self, *, capacity: int = 1_024):
        return self.events.subscribe(capacity=capacity)

    def update_track(
        self,
        track: TrackSnapshot,
        prediction: CrossingPrediction | None = None,
        *,
        event_id: str | None = None,
    ) -> BeanRecord:
        with self._lock:
            record, event = self._update_track_locked(
                track, prediction, event_id=event_id
            )
        if event is not None:
            self.events.publish(event)
        return record

    def update_tracks(
        self,
        updates: Sequence[
            tuple[TrackSnapshot, CrossingPrediction | None, str | None]
        ],
    ) -> tuple[BeanRecord, ...]:
        """Commit one camera frame's track updates in a shared transaction."""

        if not updates:
            return ()
        published: list[BeanEvent] = []
        records: list[BeanRecord] = []
        with self._lock:
            previous_records = self._records.copy()
            previous_processed = self._processed.copy()
            previous_journal = deque(self._journal, maxlen=self._journal.maxlen)
            previous_sequence = self._stream_sequence
            transaction = (
                nullcontext()
                if self.repository is None
                else self.repository.batch()
            )
            try:
                with transaction:
                    for track, prediction, event_id in updates:
                        record, event = self._update_track_locked(
                            track, prediction, event_id=event_id
                        )
                        records.append(record)
                        if event is not None:
                            published.append(event)
            except Exception:
                self._records = previous_records
                self._processed = previous_processed
                self._journal = previous_journal
                self._stream_sequence = previous_sequence
                raise
        for event in published:
            self.events.publish(event)
        return tuple(records)

    def _update_track_locked(
        self,
        track: TrackSnapshot,
        prediction: CrossingPrediction | None,
        *,
        event_id: str | None,
    ) -> tuple[BeanRecord, BeanEvent | None]:
        if not track.bean_ref.run_id.strip() or track.bean_ref.sequence <= 0:
            raise RegistryConflictError(
                "registry tracks require a public run ID and positive bean sequence"
            )
        if prediction is not None and prediction.bean_ref != track.bean_ref:
            raise RegistryConflictError("prediction belongs to a different bean")
        identifier = event_id or uuid.uuid4().hex
        fingerprint = _fingerprint(
            "update_track",
            {
                "track": _track_command_dict(track),
                "prediction": (
                    None if prediction is None else prediction_to_dict(prediction)
                ),
            },
        )
        duplicate = self._duplicate(identifier, track.bean_ref, fingerprint)
        if duplicate is not None:
            return duplicate, None
        previous = self._get_locked(track.bean_ref)
        if previous is not None and track.timestamp_ns < previous.track.timestamp_ns:
            raise StaleRegistryUpdateError(
                "track timestamp is older than the current registry state"
            )
        if previous is not None:
            _validate_status_transition(previous.status, track.status)
        if previous is not None:
            track = _merge_track_history(previous.track, track)
        revision = 1 if previous is None else previous.revision + 1
        created_timestamp = (
            _track_created_timestamp(track)
            if previous is None
            else previous.created_timestamp_ns
        )
        record = BeanRecord(
            bean_ref=track.bean_ref,
            revision=revision,
            status=track.status,
            created_timestamp_ns=created_timestamp,
            updated_timestamp_ns=(
                track.timestamp_ns
                if previous is None
                else max(previous.updated_timestamp_ns, track.timestamp_ns)
            ),
            track=track,
            prediction=prediction,
            enrichments=() if previous is None else previous.enrichments,
            decision=None if previous is None else previous.decision,
        )
        kind = _track_event_kind(previous, track.status)
        event = self._event(
            identifier, kind, record, track.timestamp_ns, fingerprint
        )
        return record, self._commit(record, event)

    def add_enrichment(
        self,
        bean_ref: BeanRef,
        enrichment: Enrichment,
        *,
        event_id: str | None = None,
    ) -> BeanRecord:
        _validate_enrichment(enrichment)
        result_id = enrichment.result_id or uuid.uuid4().hex
        enrichment = replace(enrichment, result_id=result_id)
        identifier = event_id or result_id
        fingerprint = _fingerprint(
            "add_enrichment", enrichment_to_dict(enrichment)
        )
        with self._lock:
            duplicate = self._duplicate(identifier, bean_ref, fingerprint)
            if duplicate is not None:
                return duplicate
            previous = self._require_locked(bean_ref)
            matching = tuple(
                item for item in previous.enrichments if item.result_id == result_id
            )
            if matching:
                if matching[0] != enrichment:
                    raise RegistryConflictError(
                        "an enrichment result ID was reused with different content"
                    )
                return previous
            record = replace(
                previous,
                revision=previous.revision + 1,
                updated_timestamp_ns=max(
                    previous.updated_timestamp_ns, enrichment.timestamp_ns
                ),
                enrichments=(*previous.enrichments, enrichment),
            )
            event = self._event(
                identifier,
                "enrichment.added",
                record,
                enrichment.timestamp_ns,
                fingerprint,
            )
            event = self._commit(record, event)
        self.events.publish(event)
        return record

    def set_sorting_decision(
        self,
        bean_ref: BeanRef,
        decision: SortingDecision,
        *,
        event_id: str | None = None,
    ) -> BeanRecord:
        _validate_decision(decision)
        identifier = event_id or decision.decision_id or uuid.uuid4().hex
        if not decision.decision_id:
            decision = replace(decision, decision_id=identifier)
        fingerprint = _fingerprint(
            "set_sorting_decision", decision_to_dict(decision)
        )
        with self._lock:
            duplicate = self._duplicate(identifier, bean_ref, fingerprint)
            if duplicate is not None:
                return duplicate
            previous = self._require_locked(bean_ref)
            if previous.decision is not None:
                if previous.decision.decision_id == decision.decision_id:
                    if previous.decision != decision:
                        raise RegistryConflictError(
                            "a decision ID was reused with different content"
                        )
                    return previous
                if previous.decision.acknowledged_timestamp_ns is not None:
                    raise RegistryConflictError(
                        "an acknowledged sorting decision cannot be replaced"
                    )
            record = replace(
                previous,
                revision=previous.revision + 1,
                updated_timestamp_ns=max(
                    previous.updated_timestamp_ns, decision.timestamp_ns
                ),
                decision=decision,
            )
            event = self._event(
                identifier,
                "sorting.decision",
                record,
                decision.timestamp_ns,
                fingerprint,
            )
            event = self._commit(record, event)
        self.events.publish(event)
        return record

    def acknowledge_sorting_decision(
        self,
        bean_ref: BeanRef,
        decision_id: str,
        timestamp_ns: int,
        *,
        event_id: str | None = None,
    ) -> BeanRecord:
        identifier = event_id or uuid.uuid4().hex
        fingerprint = _fingerprint(
            "acknowledge_sorting_decision",
            {"decision_id": decision_id, "timestamp_ns": int(timestamp_ns)},
        )
        with self._lock:
            duplicate = self._duplicate(identifier, bean_ref, fingerprint)
            if duplicate is not None:
                return duplicate
            previous = self._require_locked(bean_ref)
            decision = previous.decision
            if decision is None or decision.decision_id != decision_id:
                raise RegistryConflictError("sorting decision does not exist")
            if timestamp_ns < decision.timestamp_ns:
                raise StaleRegistryUpdateError(
                    "decision acknowledgement predates the decision"
                )
            if decision.acknowledged_timestamp_ns is not None:
                if decision.acknowledged_timestamp_ns != timestamp_ns:
                    raise RegistryConflictError(
                        "sorting decision was already acknowledged at another time"
                    )
                return previous
            acknowledged = replace(
                decision, acknowledged_timestamp_ns=int(timestamp_ns)
            )
            record = replace(
                previous,
                revision=previous.revision + 1,
                updated_timestamp_ns=max(previous.updated_timestamp_ns, timestamp_ns),
                decision=acknowledged,
            )
            event = self._event(
                identifier,
                "sorting.acknowledged",
                record,
                timestamp_ns,
                fingerprint,
            )
            event = self._commit(record, event)
        self.events.publish(event)
        return record

    def get(self, bean_ref: BeanRef) -> BeanRecord:
        with self._lock:
            return self._require_locked(bean_ref)

    def list_records(
        self,
        *,
        run_id: str | None = None,
        statuses: Sequence[TrackStatus] | None = None,
    ) -> tuple[BeanRecord, ...]:
        with self._lock:
            if self.repository is not None:
                for record in self.repository.list_records(
                    run_id=run_id, statuses=statuses
                ):
                    self._records.setdefault(record.bean_ref, record)
            allowed = None if statuses is None else frozenset(statuses)
            records = (
                record
                for record in self._records.values()
                if (run_id is None or record.bean_ref.run_id == run_id)
                and (allowed is None or record.status in allowed)
            )
            return tuple(sorted(records, key=lambda item: item.bean_ref))

    def list_active(self, *, run_id: str | None = None) -> tuple[BeanRecord, ...]:
        return self.list_records(
            run_id=run_id,
            statuses=(
                TrackStatus.TENTATIVE,
                TrackStatus.CONFIRMED,
                TrackStatus.OCCLUDED,
            ),
        )

    def events_since(
        self, after_sequence: int, *, limit: int = 1_000
    ) -> tuple[BeanEvent, ...]:
        if after_sequence < 0:
            raise ValueError("event cursor cannot be negative")
        if limit <= 0 or limit > 10_000:
            raise ValueError("event query limit must be between 1 and 10000")
        with self._lock:
            if self.repository is not None:
                return self.repository.events_since(after_sequence, limit=limit)
            if self._journal and after_sequence < self._journal[0].stream_sequence - 1:
                raise RegistryHistoryGapError(
                    "event cursor is older than the in-memory registry journal"
                )
            return tuple(
                event
                for event in self._journal
                if event.stream_sequence > after_sequence
            )[:limit]

    def evict_completed(self, *, before_timestamp_ns: int) -> int:
        terminal = {TrackStatus.EXITED, TrackStatus.CANCELLED}
        with self._lock:
            targets = tuple(
                bean_ref
                for bean_ref, record in self._records.items()
                if record.status in terminal
                and record.updated_timestamp_ns < before_timestamp_ns
                and (
                    record.decision is None
                    or record.decision.acknowledged_timestamp_ns is not None
                )
            )
            for bean_ref in targets:
                self._records.pop(bean_ref, None)
            return len(targets)

    def _get_locked(self, bean_ref: BeanRef) -> BeanRecord | None:
        record = self._records.get(bean_ref)
        if record is None and self.repository is not None:
            record = self.repository.load(bean_ref)
            if record is not None:
                self._records[bean_ref] = record
        return record

    def _require_locked(self, bean_ref: BeanRef) -> BeanRecord:
        record = self._get_locked(bean_ref)
        if record is None:
            raise BeanNotFoundError(f"unknown bean {bean_ref}")
        return record

    def _duplicate(
        self, event_id: str, bean_ref: BeanRef, fingerprint: str
    ) -> BeanRecord | None:
        remembered = self._processed.get(event_id)
        if remembered is not None:
            owner, previous_fingerprint = remembered
            if owner != bean_ref or previous_fingerprint != fingerprint:
                raise RegistryConflictError(
                    "event ID was reused for a different registry command"
                )
            return self._require_locked(bean_ref)
        identity = (
            None
            if self.repository is None
            else self.repository.event_identity(event_id)
        )
        if identity is not None:
            owner, previous_fingerprint = identity
            if owner != bean_ref or previous_fingerprint != fingerprint:
                raise RegistryConflictError(
                    "event ID was reused for a different registry command"
                )
            record = self._require_locked(owner)
            self._remember(event_id, bean_ref, fingerprint)
            return record
        return None

    def _event(
        self,
        event_id: str,
        kind: str,
        record: BeanRecord,
        timestamp_ns: int,
        fingerprint: str,
    ) -> BeanEvent:
        # Per-frame history is deliberately omitted from fan-out messages. Consumers
        # can query it when required without growing the 60 FPS hot-path payload.
        return BeanEvent(
            kind=kind,
            bean_ref=record.bean_ref,
            timestamp_ns=int(timestamp_ns),
            payload={
                "command_fingerprint": fingerprint,
                "record": record_to_dict(record, include_history=False),
            },
            revision=record.revision,
            event_id=event_id,
        )

    def _commit(self, record: BeanRecord, event: BeanEvent) -> BeanEvent:
        # This also proves every enrichment value is safe for SQLite and JSON IPC.
        try:
            json.dumps(event.payload, allow_nan=False, separators=(",", ":"))
        except (TypeError, ValueError) as exc:
            raise RegistryConflictError(
                f"registry values must be finite JSON data: {exc}"
            ) from exc
        if self.repository is not None:
            stream_sequence = self.repository.save(record, event)
            self._stream_sequence = max(self._stream_sequence, stream_sequence)
        else:
            self._stream_sequence += 1
            stream_sequence = self._stream_sequence
        event = replace(event, stream_sequence=stream_sequence)
        self._records[record.bean_ref] = record
        self._journal.append(event)
        self._remember(
            event.event_id,
            record.bean_ref,
            str(event.payload["command_fingerprint"]),
        )
        return event

    def _remember(
        self, event_id: str, bean_ref: BeanRef, fingerprint: str
    ) -> None:
        self._processed[event_id] = (bean_ref, fingerprint)
        self._processed.move_to_end(event_id)
        while len(self._processed) > self._idempotency_capacity:
            self._processed.popitem(last=False)


def _track_created_timestamp(track: TrackSnapshot) -> int:
    if track.history:
        return track.history[0].timestamp_ns
    return track.timestamp_ns


def _merge_track_history(
    previous: TrackSnapshot, incoming: TrackSnapshot
) -> TrackSnapshot:
    if previous.bean_ref != incoming.bean_ref:
        raise RegistryConflictError("cannot merge histories from different beans")
    known = {
        (observation.frame_index, observation.timestamp_ns)
        for observation in previous.history
    }
    additions = tuple(
        observation
        for observation in incoming.history
        if (observation.frame_index, observation.timestamp_ns) not in known
    )
    return replace(incoming, history=(*previous.history, *additions))


def _track_event_kind(
    previous: BeanRecord | None, status: TrackStatus
) -> str:
    if previous is None:
        return "bean.created"
    if status == TrackStatus.EXITED:
        return "bean.exited"
    if status == TrackStatus.CANCELLED:
        return "bean.cancelled"
    if previous.status == TrackStatus.TENTATIVE and status == TrackStatus.CONFIRMED:
        return "bean.confirmed"
    return "track.updated"


def _validate_enrichment(enrichment: Enrichment) -> None:
    if not enrichment.source.strip() or not enrichment.kind.strip():
        raise RegistryConflictError("enrichment source and kind are required")
    if enrichment.timestamp_ns < 0:
        raise RegistryConflictError("enrichment timestamp cannot be negative")
    if enrichment.confidence is not None and not 0.0 <= enrichment.confidence <= 1.0:
        raise RegistryConflictError("enrichment confidence must be between zero and one")


def _validate_status_transition(previous: TrackStatus, incoming: TrackStatus) -> None:
    allowed = {
        TrackStatus.TENTATIVE: {
            TrackStatus.TENTATIVE,
            TrackStatus.CONFIRMED,
            TrackStatus.CANCELLED,
        },
        TrackStatus.CONFIRMED: {
            TrackStatus.CONFIRMED,
            TrackStatus.OCCLUDED,
            TrackStatus.EXITED,
        },
        TrackStatus.OCCLUDED: {
            TrackStatus.OCCLUDED,
            TrackStatus.CONFIRMED,
            TrackStatus.EXITED,
        },
        TrackStatus.EXITED: set(),
        TrackStatus.CANCELLED: set(),
    }
    if incoming not in allowed[previous]:
        raise RegistryConflictError(
            f"invalid bean lifecycle transition {previous.value} -> {incoming.value}"
        )


def _validate_decision(decision: SortingDecision) -> None:
    if not decision.source.strip():
        raise RegistryConflictError("sorting decision source is required")
    if decision.timestamp_ns < 0:
        raise RegistryConflictError("sorting decision timestamp cannot be negative")
    if decision.actuation_timestamp_ns < decision.timestamp_ns:
        raise RegistryConflictError("actuation cannot predate its sorting decision")
    if len(set(decision.gate_indices)) != len(decision.gate_indices):
        raise RegistryConflictError("sorting decision gate indices must be unique")


def _fingerprint(operation: str, value: object) -> str:
    try:
        encoded = json.dumps(
            {"operation": operation, "value": value},
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise RegistryConflictError(
            f"registry values must be finite JSON data: {exc}"
        ) from exc
    return hashlib.sha256(encoded).hexdigest()


def _track_command_dict(track: TrackSnapshot) -> dict[str, object]:
    value = track_to_dict(track, include_history=False)
    if track.history:
        value["history"] = [observation_to_dict(track.history[-1])]
    return value
