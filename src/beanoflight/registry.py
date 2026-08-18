"""Authoritative, revisioned hot-state registry for live bean processing."""

from __future__ import annotations

import hashlib
import json
import math
import threading
import uuid
from collections import OrderedDict, deque
from collections.abc import Sequence
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass, replace
from typing import Protocol

from .events import EventBus
from .models import BeanEvent, BeanRef, CrossingPrediction, TrackSnapshot, TrackStatus
from .registry_models import (
    ActuationResult,
    BeanRecord,
    Enrichment,
    InferenceJob,
    InferenceStatus,
    RunSession,
    RunState,
    SortingDecision,
    actuation_to_dict,
    decision_to_dict,
    enrichment_to_dict,
    inference_job_to_dict,
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


_MISSING = object()


@dataclass(slots=True)
class _BatchUndo:
    """Changes made to hot state while one durable frame transaction is open."""

    stream_sequence: int
    records: dict[BeanRef, BeanRecord | object]
    processed_added: list[str]
    processed_evicted: list[tuple[str, tuple[BeanRef, str]]]
    journal_initial_length: int
    journal_added: int
    journal_evicted: list[BeanEvent]


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

    def event_cursor(self) -> int: ...

    def batch(self) -> AbstractContextManager[None]: ...

    def save_session(self, session: RunSession) -> None: ...

    def load_session(self, run_id: str) -> RunSession | None: ...

    def list_sessions(self) -> tuple[RunSession, ...]: ...


class BeanRegistry:
    """Single-writer materialized state with validated worker enrichments."""

    def __init__(
        self,
        repository: RegistryRepository | None = None,
        *,
        events: EventBus | None = None,
        idempotency_capacity: int = 8_192,
        event_history_capacity: int = 8_192,
        record_cache_capacity: int = 256,
    ) -> None:
        self.repository = repository
        self.events = events or EventBus()
        self._lock = threading.RLock()
        self._records: dict[BeanRef, BeanRecord] = {}
        self._record_cache_capacity = max(16, int(record_cache_capacity))
        self._sessions: dict[str, RunSession] = {}
        self._processed: OrderedDict[str, tuple[BeanRef, str]] = OrderedDict()
        self._idempotency_capacity = max(128, int(idempotency_capacity))
        self._journal: deque[BeanEvent] = deque(
            maxlen=max(128, int(event_history_capacity))
        )
        self._stream_sequence = 0 if repository is None else repository.event_cursor()
        self._batch_undo: _BatchUndo | None = None

    def subscribe(self, *, capacity: int = 1_024):
        return self.events.subscribe(capacity=capacity)

    def put_session(
        self, session: RunSession, *, expected_revision: int | None = None
    ) -> RunSession:
        """Create or update a replay/live session with optimistic revision checks."""

        _validate_session(session)
        with self._lock:
            previous = self._get_session_locked(session.run_id)
            current_revision = 0 if previous is None else previous.revision
            if expected_revision is not None and expected_revision != current_revision:
                raise StaleRegistryUpdateError(
                    f"run revision is {current_revision}, expected {expected_revision}"
                )
            if previous is not None:
                _validate_run_transition(previous.state, session.state)
                immutable_previous = (
                    previous.source_path,
                    previous.source_kind,
                    previous.frame_count,
                    previous.source_fps,
                    previous.source_start_timestamp_ns,
                )
                immutable_incoming = (
                    session.source_path,
                    session.source_kind,
                    session.frame_count,
                    session.source_fps,
                    session.source_start_timestamp_ns,
                )
                if immutable_incoming != immutable_previous:
                    raise RegistryConflictError(
                        "run source identity and timing cannot change after creation"
                    )
            stored = replace(
                session,
                revision=current_revision + 1,
                created_timestamp_ns=(
                    session.created_timestamp_ns
                    if previous is None
                    else previous.created_timestamp_ns
                ),
            )
            # Validate settings before changing either persistence or hot state.
            try:
                json.dumps(stored.settings, allow_nan=False, separators=(",", ":"))
            except (TypeError, ValueError) as exc:
                raise RegistryConflictError(
                    f"run settings must be finite JSON data: {exc}"
                ) from exc
            if self.repository is not None:
                self.repository.save_session(stored)
            self._sessions[stored.run_id] = stored
            self._trim_record_cache()
            return stored

    def get_session(self, run_id: str) -> RunSession:
        with self._lock:
            session = self._get_session_locked(run_id)
            if session is None:
                raise BeanNotFoundError(f"unknown run {run_id!r}")
            return session

    def list_sessions(self) -> tuple[RunSession, ...]:
        with self._lock:
            if self.repository is not None:
                for session in self.repository.list_sessions():
                    self._sessions.setdefault(session.run_id, session)
            return tuple(
                sorted(
                    self._sessions.values(), key=lambda item: item.created_timestamp_ns
                )
            )

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
        updates: Sequence[tuple[TrackSnapshot, CrossingPrediction | None, str | None]],
    ) -> tuple[BeanRecord, ...]:
        """Commit one camera frame's track updates in a shared transaction."""

        if not updates:
            return ()
        published: list[BeanEvent] = []
        records: list[BeanRecord] = []
        with self._lock:
            if self._batch_undo is not None:
                raise RegistryConflictError("nested registry frame batches are invalid")
            undo = _BatchUndo(
                self._stream_sequence,
                {},
                [],
                [],
                len(self._journal),
                0,
                [],
            )
            self._batch_undo = undo
            transaction = (
                nullcontext() if self.repository is None else self.repository.batch()
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
                self._rollback_batch(undo)
                raise
            finally:
                self._batch_undo = None
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
            inference_jobs=() if previous is None else previous.inference_jobs,
            actuation=None if previous is None else previous.actuation,
        )
        kind = _track_event_kind(previous, track.status)
        event = self._event(identifier, kind, record, track.timestamp_ns, fingerprint)
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
        fingerprint = _fingerprint("add_enrichment", enrichment_to_dict(enrichment))
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

    def submit_inference_job(
        self, job: InferenceJob, *, event_id: str | None = None
    ) -> BeanRecord:
        _validate_inference_job(job)
        if job.status != InferenceStatus.SUBMITTED:
            raise RegistryConflictError("a new inference job must be submitted")
        identifier = event_id or job.job_id
        fingerprint = _fingerprint("submit_inference_job", inference_job_to_dict(job))
        with self._lock:
            duplicate = self._duplicate(identifier, job.bean_ref, fingerprint)
            if duplicate is not None:
                return duplicate
            previous = self._require_locked(job.bean_ref)
            matching = tuple(
                item for item in previous.inference_jobs if item.job_id == job.job_id
            )
            if matching:
                if matching[0] != job:
                    raise RegistryConflictError(
                        "an inference job ID was reused with different content"
                    )
                return previous
            if job.source_registry_revision > previous.revision:
                raise RegistryConflictError(
                    "inference job refers to a future bean revision"
                )
            record = replace(
                previous,
                revision=previous.revision + 1,
                updated_timestamp_ns=max(
                    previous.updated_timestamp_ns, job.updated_timestamp_ns
                ),
                inference_jobs=(*previous.inference_jobs, job),
            )
            event = self._event(
                identifier,
                "inference.submitted",
                record,
                job.updated_timestamp_ns,
                fingerprint,
            )
            event = self._commit(record, event)
        self.events.publish(event)
        return record

    def update_inference_job(
        self,
        bean_ref: BeanRef,
        job_id: str,
        status: InferenceStatus,
        timestamp_ns: int,
        *,
        detail: str = "",
        event_id: str | None = None,
    ) -> BeanRecord:
        identifier = event_id or uuid.uuid4().hex
        fingerprint = _fingerprint(
            "update_inference_job",
            {
                "job_id": job_id,
                "status": status.value,
                "timestamp_ns": int(timestamp_ns),
                "detail": detail,
            },
        )
        with self._lock:
            duplicate = self._duplicate(identifier, bean_ref, fingerprint)
            if duplicate is not None:
                return duplicate
            previous = self._require_locked(bean_ref)
            jobs = list(previous.inference_jobs)
            index = next(
                (index for index, item in enumerate(jobs) if item.job_id == job_id),
                None,
            )
            if index is None:
                raise RegistryConflictError("inference job does not exist")
            job = jobs[index]
            _validate_inference_transition(job.status, status)
            if timestamp_ns < job.updated_timestamp_ns:
                raise StaleRegistryUpdateError(
                    "inference update predates current job state"
                )
            updated = replace(
                job,
                status=status,
                updated_timestamp_ns=int(timestamp_ns),
                detail=detail,
            )
            if updated == job:
                return previous
            jobs[index] = updated
            record = replace(
                previous,
                revision=previous.revision + 1,
                updated_timestamp_ns=max(previous.updated_timestamp_ns, timestamp_ns),
                inference_jobs=tuple(jobs),
            )
            event = self._event(
                identifier,
                f"inference.{status.value}",
                record,
                timestamp_ns,
                fingerprint,
            )
            event = self._commit(record, event)
        self.events.publish(event)
        return record

    def complete_inference_job(
        self,
        bean_ref: BeanRef,
        job_id: str,
        enrichment: Enrichment,
        *,
        event_id: str | None = None,
    ) -> BeanRecord:
        """Atomically complete one job and append its classification result."""

        _validate_enrichment(enrichment)
        if not enrichment.result_id:
            enrichment = replace(enrichment, result_id=job_id)
        identifier = event_id or f"complete:{job_id}"
        fingerprint = _fingerprint(
            "complete_inference_job",
            {"job_id": job_id, "enrichment": enrichment_to_dict(enrichment)},
        )
        with self._lock:
            duplicate = self._duplicate(identifier, bean_ref, fingerprint)
            if duplicate is not None:
                return duplicate
            previous = self._require_locked(bean_ref)
            jobs = list(previous.inference_jobs)
            index = next(
                (index for index, item in enumerate(jobs) if item.job_id == job_id),
                None,
            )
            if index is None:
                raise RegistryConflictError("inference job does not exist")
            job = jobs[index]
            _validate_inference_transition(job.status, InferenceStatus.COMPLETED)
            if enrichment.timestamp_ns < job.updated_timestamp_ns:
                raise StaleRegistryUpdateError(
                    "inference completion predates the current job state"
                )
            matching = tuple(
                item
                for item in previous.enrichments
                if item.result_id == enrichment.result_id
            )
            if matching and matching[0] != enrichment:
                raise RegistryConflictError(
                    "an enrichment result ID was reused with different content"
                )
            jobs[index] = replace(
                job,
                status=InferenceStatus.COMPLETED,
                updated_timestamp_ns=enrichment.timestamp_ns,
            )
            enrichments = (
                previous.enrichments
                if matching
                else (*previous.enrichments, enrichment)
            )
            record = replace(
                previous,
                revision=previous.revision + 1,
                updated_timestamp_ns=max(
                    previous.updated_timestamp_ns, enrichment.timestamp_ns
                ),
                inference_jobs=tuple(jobs),
                enrichments=enrichments,
            )
            event = self._event(
                identifier,
                "inference.completed",
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
        fingerprint = _fingerprint("set_sorting_decision", decision_to_dict(decision))
        with self._lock:
            duplicate = self._duplicate(identifier, bean_ref, fingerprint)
            if duplicate is not None:
                return duplicate
            previous = self._require_locked(bean_ref)
            if decision.based_on_revision > previous.revision:
                raise RegistryConflictError(
                    "sorting decision refers to a future bean revision"
                )
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

    def record_actuation(
        self,
        bean_ref: BeanRef,
        result: ActuationResult,
        *,
        event_id: str | None = None,
    ) -> BeanRecord:
        _validate_actuation(result)
        identifier = event_id or f"actuation:{result.decision_id}"
        fingerprint = _fingerprint("record_actuation", actuation_to_dict(result))
        with self._lock:
            duplicate = self._duplicate(identifier, bean_ref, fingerprint)
            if duplicate is not None:
                return duplicate
            previous = self._require_locked(bean_ref)
            if (
                previous.decision is None
                or previous.decision.decision_id != result.decision_id
            ):
                raise RegistryConflictError(
                    "actuation does not match the sorting decision"
                )
            if previous.actuation is not None:
                if previous.actuation != result:
                    raise RegistryConflictError(
                        "an actuation result was already recorded with different content"
                    )
                return previous
            record = replace(
                previous,
                revision=previous.revision + 1,
                updated_timestamp_ns=max(
                    previous.updated_timestamp_ns,
                    result.actual_close_timestamp_ns,
                ),
                actuation=result,
            )
            event = self._event(
                identifier,
                "sorting.actuated" if result.success else "sorting.failed",
                record,
                result.actual_close_timestamp_ns,
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
                # Large monitor/recovery queries must not turn the hot cache into
                # an unbounded mirror of SQLite. The durable snapshot is already
                # complete and ordered, so return it directly.
                return self.repository.list_records(run_id=run_id, statuses=statuses)
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
            if after_sequence >= self._stream_sequence:
                return ()
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

    def event_cursor(self) -> int:
        """Return the newest durable journal sequence without replaying history."""

        with self._lock:
            return self._stream_sequence

    def evict_completed(
        self, *, before_timestamp_ns: int, run_id: str | None = None
    ) -> int:
        terminal = {TrackStatus.EXITED, TrackStatus.CANCELLED}
        with self._lock:
            targets = tuple(
                bean_ref
                for bean_ref, record in self._records.items()
                if (run_id is None or bean_ref.run_id == run_id)
                and record.status in terminal
                and record.updated_timestamp_ns < before_timestamp_ns
                and (
                    record.decision is None
                    or record.actuation is not None
                    or record.decision.acknowledged_timestamp_ns is not None
                )
            )
            for bean_ref in targets:
                self._records.pop(bean_ref, None)
            return len(targets)

    def _get_session_locked(self, run_id: str) -> RunSession | None:
        session = self._sessions.get(run_id)
        if session is None and self.repository is not None:
            session = self.repository.load_session(run_id)
            if session is not None:
                self._sessions[run_id] = session
        return session

    def _get_locked(self, bean_ref: BeanRef) -> BeanRecord | None:
        record = self._records.get(bean_ref)
        if record is None and self.repository is not None:
            record = self.repository.load(bean_ref)
            if record is not None:
                self._set_hot_record(record)
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
        self._set_hot_record(record)
        self._append_journal(event)
        self._remember(
            event.event_id,
            record.bean_ref,
            str(event.payload["command_fingerprint"]),
        )
        return event

    def hot_state_metrics(self) -> dict[str, int]:
        with self._lock:
            return {
                "records": len(self._records),
                "record_capacity": self._record_cache_capacity,
                "sessions": len(self._sessions),
                "idempotency_entries": len(self._processed),
                "journal_events": len(self._journal),
            }

    def _set_hot_record(self, record: BeanRecord) -> None:
        undo = self._batch_undo
        if undo is not None and record.bean_ref not in undo.records:
            undo.records[record.bean_ref] = self._records.get(record.bean_ref, _MISSING)
        self._records[record.bean_ref] = record
        self._trim_record_cache()

    def _trim_record_cache(self) -> None:
        """Bound durable hot state while retaining beans in an active run."""

        if self.repository is None:
            # An in-memory registry has no durable copy from which to rehydrate.
            return

        while len(self._records) > self._record_cache_capacity:
            candidate = next(
                (
                    bean_ref
                    for bean_ref, cached in self._records.items()
                    if cached.status in {TrackStatus.EXITED, TrackStatus.CANCELLED}
                    or (
                        (session := self._sessions.get(bean_ref.run_id)) is not None
                        and session.state in {RunState.COMPLETED, RunState.FAILED}
                    )
                ),
                None,
            )
            if candidate is None:
                # Active beans from a running session are never evicted; their
                # population is bounded by what is simultaneously in flight.
                return
            undo = self._batch_undo
            if undo is not None and candidate not in undo.records:
                undo.records[candidate] = self._records[candidate]
            self._records.pop(candidate)

    def _append_journal(self, event: BeanEvent) -> None:
        undo = self._batch_undo
        if undo is not None:
            if (
                len(self._journal) == self._journal.maxlen
                and len(undo.journal_evicted) < undo.journal_initial_length
            ):
                undo.journal_evicted.append(self._journal[0])
            undo.journal_added += 1
        self._journal.append(event)

    def _remember(self, event_id: str, bean_ref: BeanRef, fingerprint: str) -> None:
        undo = self._batch_undo
        if undo is not None and event_id not in self._processed:
            undo.processed_added.append(event_id)
        self._processed[event_id] = (bean_ref, fingerprint)
        self._processed.move_to_end(event_id)
        while len(self._processed) > self._idempotency_capacity:
            evicted = self._processed.popitem(last=False)
            if undo is not None and evicted[0] not in undo.processed_added:
                undo.processed_evicted.append(evicted)

    def _rollback_batch(self, undo: _BatchUndo) -> None:
        self._stream_sequence = undo.stream_sequence
        for bean_ref, previous in undo.records.items():
            if previous is _MISSING:
                self._records.pop(bean_ref, None)
            else:
                self._records[bean_ref] = previous  # type: ignore[assignment]
        for event_id in undo.processed_added:
            self._processed.pop(event_id, None)
        for event_id, value in reversed(undo.processed_evicted):
            self._processed[event_id] = value
            self._processed.move_to_end(event_id, last=False)
        for _ in range(min(undo.journal_added, len(self._journal))):
            self._journal.pop()
        for event in reversed(undo.journal_evicted):
            self._journal.appendleft(event)


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


def _track_event_kind(previous: BeanRecord | None, status: TrackStatus) -> str:
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
        raise RegistryConflictError(
            "enrichment confidence must be between zero and one"
        )


def _validate_session(session: RunSession) -> None:
    if not session.run_id.strip():
        raise RegistryConflictError("run ID is required")
    if not session.source_path.strip() or not session.source_kind.strip():
        raise RegistryConflictError("run source path and kind are required")
    if session.revision < 0:
        raise RegistryConflictError("run revision cannot be negative")
    if session.frame_count <= 0 or session.source_fps <= 0:
        raise RegistryConflictError("run frame count and source FPS must be positive")
    if not math.isfinite(session.source_fps) or not math.isfinite(session.target_fps):
        raise RegistryConflictError("run FPS values must be finite")
    if session.target_fps < 0:
        raise RegistryConflictError("target FPS cannot be negative")
    if (
        min(
            session.source_start_timestamp_ns,
            session.clock_source_timestamp_ns,
            session.clock_monotonic_ns,
            session.created_timestamp_ns,
            session.updated_timestamp_ns,
        )
        < 0
    ):
        raise RegistryConflictError("run timestamps cannot be negative")
    if session.updated_timestamp_ns < session.created_timestamp_ns:
        raise RegistryConflictError("run update cannot predate run creation")


def _validate_run_transition(previous: RunState, incoming: RunState) -> None:
    allowed = {
        RunState.CREATED: {RunState.CREATED, RunState.RUNNING, RunState.FAILED},
        RunState.RUNNING: {
            RunState.RUNNING,
            RunState.PAUSED,
            RunState.COMPLETED,
            RunState.FAILED,
        },
        RunState.PAUSED: {
            RunState.PAUSED,
            RunState.RUNNING,
            RunState.COMPLETED,
            RunState.FAILED,
        },
        RunState.COMPLETED: set(),
        RunState.FAILED: set(),
    }
    if incoming not in allowed[previous]:
        raise RegistryConflictError(
            f"invalid run transition {previous.value} -> {incoming.value}"
        )


def _validate_inference_job(job: InferenceJob) -> None:
    if not job.job_id.strip() or not job.camera_id.strip():
        raise RegistryConflictError("inference job and camera IDs are required")
    if not job.bean_ref.run_id.strip() or job.bean_ref.sequence <= 0:
        raise RegistryConflictError("inference job requires a public bean reference")
    if job.frame_index < 0 or job.source_registry_revision <= 0:
        raise RegistryConflictError(
            "inference frame and bean revision must be positive"
        )
    if job.crop_width_px <= 0 or job.crop_height_px <= 0:
        raise RegistryConflictError("inference crop dimensions must be positive")
    if (
        min(
            job.capture_timestamp_ns,
            job.submitted_timestamp_ns,
            job.updated_timestamp_ns,
        )
        < 0
    ):
        raise RegistryConflictError("inference timestamps cannot be negative")


def _validate_inference_transition(
    previous: InferenceStatus, incoming: InferenceStatus
) -> None:
    allowed = {
        InferenceStatus.SUBMITTED: {
            InferenceStatus.SUBMITTED,
            InferenceStatus.ACCEPTED,
            InferenceStatus.COMPLETED,
            InferenceStatus.FAILED,
            InferenceStatus.DROPPED,
        },
        InferenceStatus.ACCEPTED: {
            InferenceStatus.ACCEPTED,
            InferenceStatus.COMPLETED,
            InferenceStatus.FAILED,
            InferenceStatus.DROPPED,
        },
        InferenceStatus.COMPLETED: {InferenceStatus.COMPLETED},
        InferenceStatus.FAILED: {InferenceStatus.FAILED},
        InferenceStatus.DROPPED: {InferenceStatus.DROPPED},
    }
    if incoming not in allowed[previous]:
        raise RegistryConflictError(
            f"invalid inference transition {previous.value} -> {incoming.value}"
        )


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
    if (
        decision.close_timestamp_ns is not None
        and decision.close_timestamp_ns < decision.actuation_timestamp_ns
    ):
        raise RegistryConflictError("valve close cannot predate valve open")
    if decision.based_on_revision < 0:
        raise RegistryConflictError("decision bean revision cannot be negative")
    if len(set(decision.gate_indices)) != len(decision.gate_indices):
        raise RegistryConflictError("sorting decision gate indices must be unique")


def _validate_actuation(result: ActuationResult) -> None:
    if not result.decision_id.strip() or not result.source.strip():
        raise RegistryConflictError("actuation decision ID and source are required")
    if result.actual_open_timestamp_ns < 0:
        raise RegistryConflictError("actuation open timestamp cannot be negative")
    if result.actual_close_timestamp_ns < result.actual_open_timestamp_ns:
        raise RegistryConflictError("actuation close cannot predate open")


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
