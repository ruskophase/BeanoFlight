"""Versioned JSON-over-ZeroMQ IPC for BeanRegistry."""

from __future__ import annotations

import json
import os
import queue
import threading
import time
import uuid
from collections import defaultdict, deque
from collections.abc import Callable, Mapping
from pathlib import Path

import zmq

from .models import BeanEvent, BeanRef, TrackStatus
from .registry import BeanRegistry
from .registry_models import (
    REGISTRY_SCHEMA,
    ActuationResult,
    BeanRecord,
    Enrichment,
    InferenceJob,
    InferenceStatus,
    RunSession,
    SortingDecision,
    actuation_from_dict,
    actuation_to_dict,
    bean_ref_from_dict,
    bean_ref_to_dict,
    decision_from_dict,
    decision_to_dict,
    enrichment_from_dict,
    enrichment_to_dict,
    event_from_dict,
    event_to_dict,
    inference_job_from_dict,
    inference_job_to_dict,
    observation_to_dict,
    prediction_from_dict,
    prediction_to_dict,
    record_from_dict,
    record_to_dict,
    run_session_from_dict,
    run_session_to_dict,
    track_from_dict,
    track_to_dict,
)
from .telemetry import TimingAccumulator

MAX_MESSAGE_BYTES = 4 * 1024 * 1024
REGISTRY_API_VERSION = 2
CAPABILITY_COMPLETE_INFERENCE_JOBS_ACK = "complete_inference_jobs_ack"
CAPABILITY_ADD_ENRICHMENTS = "add_enrichments"
CAPABILITY_RECORD_ACTUATION_ACK = "record_actuation_ack"
REGISTRY_CAPABILITIES = (
    CAPABILITY_COMPLETE_INFERENCE_JOBS_ACK,
    CAPABILITY_ADD_ENRICHMENTS,
    CAPABILITY_RECORD_ACTUATION_ACK,
    "event_batches",
    "events_since_compact",
)


class RegistryTransportError(RuntimeError):
    pass


class RegistryRemoteError(RegistryTransportError):
    def __init__(self, error_type: str, message: str) -> None:
        super().__init__(f"{error_type}: {message}")
        self.error_type = error_type
        self.remote_message = message


class ZeroMQRegistryServer:
    """Serve registry commands and publish replaceable state notifications."""

    def __init__(
        self,
        registry: BeanRegistry,
        *,
        command_endpoint: str,
        event_endpoint: str,
        context: zmq.Context | None = None,
        event_capacity: int = 4_096,
        event_observer: Callable[[BeanEvent], None] | None = None,
    ) -> None:
        self.registry = registry
        self.command_endpoint = command_endpoint
        self.event_endpoint = event_endpoint
        self.context = context or zmq.Context.instance()
        self._event_queue = registry.subscribe(capacity=event_capacity)
        self.event_observer = event_observer
        self._operation_timings: defaultdict[str, TimingAccumulator] = defaultdict(
            TimingAccumulator
        )

    def serve_forever(
        self,
        stop: threading.Event,
        *,
        ready: threading.Event | None = None,
        poll_interval_ms: int = 10,
    ) -> None:
        commands = self.context.socket(zmq.REP)
        events = self.context.socket(zmq.PUB)
        for socket in (commands, events):
            socket.setsockopt(zmq.LINGER, 0)
            socket.setsockopt(zmq.MAXMSGSIZE, MAX_MESSAGE_BYTES)
        commands.setsockopt(zmq.RCVHWM, 1_024)
        commands.setsockopt(zmq.SNDHWM, 1_024)
        events.setsockopt(zmq.SNDHWM, 4_096)
        commands.bind(self.command_endpoint)
        events.bind(self.event_endpoint)
        self.command_endpoint = commands.getsockopt_string(zmq.LAST_ENDPOINT)
        self.event_endpoint = events.getsockopt_string(zmq.LAST_ENDPOINT)
        poller = zmq.Poller()
        poller.register(commands, zmq.POLLIN)
        if ready is not None:
            ready.set()
        try:
            while not stop.is_set():
                readable = dict(poller.poll(max(1, poll_interval_ms)))
                if commands in readable:
                    request = commands.recv()
                    commands.send(self._handle(request))
                self._publish_waiting(events)
            self._publish_waiting(events)
        finally:
            poller.unregister(commands)
            commands.close(0)
            events.close(0)

    def _handle(self, encoded: bytes) -> bytes:
        started = time.perf_counter_ns()
        request_id = ""
        operation = "invalid"
        try:
            if len(encoded) > MAX_MESSAGE_BYTES:
                raise ValueError("registry request exceeds the message limit")
            request = _object(json.loads(encoded.decode("utf-8")))
            request_id = str(request.get("request_id", ""))
            if request.get("schema") != REGISTRY_SCHEMA:
                raise ValueError("unsupported or missing registry schema")
            if not request_id:
                raise ValueError("request_id is required")
            operation = str(request.get("operation", ""))
            payload = _object(request.get("payload", {}))
            result = self._dispatch(operation, payload, request_id)
            response = {
                "schema": REGISTRY_SCHEMA,
                "request_id": request_id,
                "ok": True,
                "result": result,
            }
        # The REP socket must answer malformed and failed commands as well as
        # successful ones, or the client/server state machine deadlocks.
        except Exception as exc:  # noqa: BLE001
            response = {
                "schema": REGISTRY_SCHEMA,
                "request_id": request_id,
                "ok": False,
                "error": {"type": type(exc).__name__, "message": str(exc)},
            }
        result = _encode(response)
        if operation not in {"service_metrics", "reset_service_metrics"}:
            self._operation_timings[operation].add(
                (time.perf_counter_ns() - started) / 1_000_000.0
            )
        return result

    def _dispatch(
        self, operation: str, payload: Mapping[str, object], request_id: str
    ) -> object:
        if operation == "ping":
            database = getattr(self.registry.repository, "path", None)
            return {
                "service": "BeanRegistry",
                "schema": REGISTRY_SCHEMA,
                "api_version": REGISTRY_API_VERSION,
                "capabilities": list(REGISTRY_CAPABILITIES),
                "pid": os.getpid(),
                "database": ("" if database is None else str(Path(database).resolve())),
            }
        if operation == "put_session":
            expected_value = payload.get("expected_revision")
            session = self.registry.put_session(
                run_session_from_dict(_object(payload["session"])),
                expected_revision=(
                    None if expected_value is None else int(expected_value)
                ),
            )
            return run_session_to_dict(session)
        if operation == "get_session":
            return run_session_to_dict(
                self.registry.get_session(str(payload["run_id"]))
            )
        if operation == "list_sessions":
            return [
                run_session_to_dict(session)
                for session in self.registry.list_sessions()
            ]
        if operation == "get":
            record = self.registry.get(bean_ref_from_dict(_object(payload["bean_ref"])))
            return record_to_dict(
                record, include_history=bool(payload.get("include_history", True))
            )
        if operation == "get_many":
            include_history = bool(payload.get("include_history", False))
            return [
                record_to_dict(
                    self.registry.get(bean_ref_from_dict(_object(raw_ref))),
                    include_history=include_history,
                )
                for raw_ref in _array(payload.get("bean_refs", []))
            ]
        if operation in {"list", "list_active"}:
            run_id_value = payload.get("run_id")
            run_id = None if run_id_value is None else str(run_id_value)
            if operation == "list_active":
                records = self.registry.list_active(run_id=run_id)
            else:
                statuses_value = payload.get("statuses")
                statuses = (
                    None
                    if statuses_value is None
                    else tuple(
                        TrackStatus(str(item)) for item in _array(statuses_value)
                    )
                )
                records = self.registry.list_records(run_id=run_id, statuses=statuses)
            return [record_to_dict(record, include_history=False) for record in records]
        if operation == "events_since":
            return [
                event_to_dict(event)
                for event in self.registry.events_since(
                    int(payload.get("after_sequence", 0)),
                    limit=int(payload.get("limit", 1_000)),
                )
            ]
        if operation == "events_since_compact":
            return [
                {
                    "event_id": event.event_id,
                    "kind": event.kind,
                    "bean_ref": bean_ref_to_dict(event.bean_ref),
                    "timestamp_ns": event.timestamp_ns,
                    "revision": event.revision,
                    "stream_sequence": event.stream_sequence,
                }
                for event in self.registry.events_since(
                    int(payload.get("after_sequence", 0)),
                    limit=int(payload.get("limit", 1_000)),
                )
            ]
        if operation == "event_cursor":
            return self.registry.event_cursor()
        if operation == "hot_state_metrics":
            return self.registry.hot_state_metrics()
        if operation == "service_metrics":
            repository = self.registry.repository
            repository_metrics = getattr(repository, "performance_metrics", None)
            return {
                "hot_state": self.registry.hot_state_metrics(),
                "operations_ms": {
                    name: timing.summary()
                    for name, timing in sorted(self._operation_timings.items())
                },
                "sqlite": ({} if repository_metrics is None else repository_metrics()),
            }
        if operation == "reset_service_metrics":
            self._operation_timings.clear()
            repository = self.registry.repository
            reset_repository = getattr(repository, "reset_performance_metrics", None)
            if reset_repository is not None:
                reset_repository()
            return self.registry.hot_state_metrics()
        if operation == "evict_completed":
            run_id_value = payload.get("run_id")
            return self.registry.evict_completed(
                before_timestamp_ns=int(payload["before_timestamp_ns"]),
                run_id=None if run_id_value is None else str(run_id_value),
            )
        if operation == "update_track":
            track = track_from_dict(_object(payload["track"]))
            prediction_value = payload.get("prediction")
            prediction = (
                None
                if prediction_value is None
                else prediction_from_dict(_object(prediction_value))
            )
            record = self.registry.update_track(
                track,
                prediction,
                event_id=str(payload.get("event_id") or request_id),
            )
            return record_to_dict(record, include_history=False)
        if operation == "update_tracks":
            updates = []
            for raw_update in _array(payload["updates"]):
                update = _object(raw_update)
                track = track_from_dict(_object(update["track"]))
                prediction_value = update.get("prediction")
                prediction = (
                    None
                    if prediction_value is None
                    else prediction_from_dict(_object(prediction_value))
                )
                updates.append(
                    (
                        track,
                        prediction,
                        str(update.get("event_id") or uuid.uuid4().hex),
                    )
                )
            return [
                record_to_dict(record, include_history=False)
                for record in self.registry.update_tracks(tuple(updates))
            ]
        if operation == "update_track_revisions":
            updates = []
            for raw_update in _array(payload["updates"]):
                update = _object(raw_update)
                track = track_from_dict(_object(update["track"]))
                prediction_value = update.get("prediction")
                prediction = (
                    None
                    if prediction_value is None
                    else prediction_from_dict(_object(prediction_value))
                )
                updates.append(
                    (
                        track,
                        prediction,
                        str(update.get("event_id") or uuid.uuid4().hex),
                    )
                )
            return [
                {
                    "bean_ref": bean_ref_to_dict(record.bean_ref),
                    "revision": record.revision,
                }
                for record in self.registry.update_tracks(tuple(updates))
            ]
        if operation == "update_frame_and_submit_jobs":
            updates = _decode_track_updates(_array(payload.get("updates", [])))
            jobs = []
            for raw_item in _array(payload.get("jobs", [])):
                item = _object(raw_item)
                job = inference_job_from_dict(_object(item["job"]))
                jobs.append((job, str(item.get("event_id") or job.job_id)))
            revisions, canonical_jobs = self.registry.update_frame_and_submit_jobs(
                updates, tuple(jobs)
            )
            return {
                "revisions": [
                    {
                        "bean_ref": bean_ref_to_dict(bean_ref),
                        "revision": revision,
                    }
                    for bean_ref, revision in revisions.items()
                ],
                "jobs": [inference_job_to_dict(job) for job in canonical_jobs],
            }
        if operation == "add_enrichment":
            record = self.registry.add_enrichment(
                bean_ref_from_dict(_object(payload["bean_ref"])),
                enrichment_from_dict(_object(payload["enrichment"])),
                event_id=str(payload.get("event_id") or request_id),
            )
            return record_to_dict(record, include_history=False)
        if operation == "add_enrichments":
            additions = []
            for raw_item in _array(payload.get("additions", [])):
                item = _object(raw_item)
                enrichment = enrichment_from_dict(_object(item["enrichment"]))
                additions.append(
                    (
                        bean_ref_from_dict(_object(item["bean_ref"])),
                        enrichment,
                        str(
                            item.get("event_id")
                            or enrichment.result_id
                            or request_id
                        ),
                    )
                )
            return [
                record_to_dict(record, include_history=False)
                for record in self.registry.add_enrichments(tuple(additions))
            ]
        if operation == "submit_inference_job":
            job = inference_job_from_dict(_object(payload["job"]))
            record = self.registry.submit_inference_job(
                job, event_id=str(payload.get("event_id") or request_id)
            )
            return record_to_dict(record, include_history=False)
        if operation == "submit_inference_job_revision":
            job = inference_job_from_dict(_object(payload["job"]))
            record = self.registry.submit_inference_job(
                job, event_id=str(payload.get("event_id") or request_id)
            )
            return record.revision
        if operation == "update_inference_job":
            timing_marks = {
                str(key): int(value)
                for key, value in _object(
                    payload.get("timing_marks_ns", {})
                ).items()
            }
            record = self.registry.update_inference_job(
                bean_ref_from_dict(_object(payload["bean_ref"])),
                str(payload["job_id"]),
                InferenceStatus(str(payload["status"])),
                int(payload["timestamp_ns"]),
                detail=str(payload.get("detail", "")),
                timing_marks_ns=timing_marks,
                event_id=str(payload.get("event_id") or request_id),
            )
            return record_to_dict(record, include_history=False)
        if operation == "complete_inference_job":
            timing_marks = {
                str(key): int(value)
                for key, value in _object(
                    payload.get("timing_marks_ns", {})
                ).items()
            }
            timing_marks.setdefault(
                "registry_classification_received_monotonic_ns",
                time.monotonic_ns(),
            )
            record = self.registry.complete_inference_job(
                bean_ref_from_dict(_object(payload["bean_ref"])),
                str(payload["job_id"]),
                enrichment_from_dict(_object(payload["enrichment"])),
                timing_marks_ns=timing_marks,
                event_id=str(payload.get("event_id") or request_id),
            )
            return record_to_dict(record, include_history=False)
        if operation in {
            "complete_inference_jobs",
            "complete_inference_jobs_ack",
        }:
            received_ns = time.monotonic_ns()
            completions = []
            for raw_item in _array(payload.get("completions", [])):
                item = _object(raw_item)
                timing_marks = {
                    str(key): int(value)
                    for key, value in _object(
                        item.get("timing_marks_ns", {})
                    ).items()
                }
                timing_marks.setdefault(
                    "registry_classification_received_monotonic_ns", received_ns
                )
                completions.append(
                    (
                        bean_ref_from_dict(_object(item["bean_ref"])),
                        str(item["job_id"]),
                        enrichment_from_dict(_object(item["enrichment"])),
                        timing_marks,
                        str(item.get("event_id") or f"complete:{item['job_id']}"),
                    )
                )
            records = self.registry.complete_inference_jobs(tuple(completions))
            if operation == "complete_inference_jobs_ack":
                return len(records)
            return [
                record_to_dict(record, include_history=False) for record in records
            ]
        if operation == "set_sorting_decision":
            record = self.registry.set_sorting_decision(
                bean_ref_from_dict(_object(payload["bean_ref"])),
                decision_from_dict(_object(payload["decision"])),
                event_id=str(payload.get("event_id") or request_id),
            )
            return record_to_dict(record, include_history=False)
        if operation == "acknowledge_sorting_decision":
            record = self.registry.acknowledge_sorting_decision(
                bean_ref_from_dict(_object(payload["bean_ref"])),
                str(payload["decision_id"]),
                int(payload["timestamp_ns"]),
                event_id=str(payload.get("event_id") or request_id),
            )
            return record_to_dict(record, include_history=False)
        if operation in {"record_actuation", "record_actuation_ack"}:
            record = self.registry.record_actuation(
                bean_ref_from_dict(_object(payload["bean_ref"])),
                actuation_from_dict(_object(payload["actuation"])),
                event_id=str(payload.get("event_id") or request_id),
            )
            if operation == "record_actuation_ack":
                return record.revision
            return record_to_dict(record, include_history=False)
        raise ValueError(f"unknown registry operation: {operation}")

    def _publish_waiting(self, socket: zmq.Socket) -> None:
        waiting = []
        while True:
            try:
                waiting.append(self._event_queue.get_nowait())
            except queue.Empty:
                break
        if not waiting:
            return
        if len(waiting) == 1:
            event = waiting[0]
            topic = event.kind
            envelope = {"schema": REGISTRY_SCHEMA, "event": event_to_dict(event)}
        else:
            kinds = {event.kind for event in waiting}
            topic = waiting[0].kind if len(kinds) == 1 else "registry.batch"
            envelope = {
                "schema": REGISTRY_SCHEMA,
                "events": [event_to_dict(event) for event in waiting],
            }
        # One source-frame transaction or one GPU completion batch becomes one
        # transport message. Durable journal events remain individually ordered.
        socket.send_multipart((topic.encode("utf-8"), _encode(envelope)))
        for event in waiting:
            if self.event_observer is not None:
                self.event_observer(event)


class ZeroMQRegistryClient:
    """Thread-affine acknowledged command/query client."""

    def __init__(
        self,
        command_endpoint: str,
        *,
        context: zmq.Context | None = None,
        timeout_ms: int = 1_000,
    ) -> None:
        self.command_endpoint = command_endpoint
        self.context = context or zmq.Context.instance()
        self.timeout_ms = max(1, int(timeout_ms))
        self._socket: zmq.Socket | None = None
        self._thread_id: int | None = None

    def ping(self) -> dict[str, object]:
        return _object(self._request("ping", {}))

    def put_session(
        self, session: RunSession, *, expected_revision: int | None = None
    ) -> RunSession:
        return run_session_from_dict(
            _object(
                self._request(
                    "put_session",
                    {
                        "session": run_session_to_dict(session),
                        "expected_revision": expected_revision,
                    },
                )
            )
        )

    def get_session(self, run_id: str) -> RunSession:
        return run_session_from_dict(
            _object(self._request("get_session", {"run_id": run_id}))
        )

    def list_sessions(self) -> tuple[RunSession, ...]:
        return tuple(
            run_session_from_dict(_object(item))
            for item in _array(self._request("list_sessions", {}))
        )

    def get(self, bean_ref: BeanRef, *, include_history: bool = True) -> BeanRecord:
        return record_from_dict(
            _object(
                self._request(
                    "get",
                    {
                        "bean_ref": bean_ref_to_dict(bean_ref),
                        "include_history": include_history,
                    },
                )
            )
        )

    def get_many(
        self,
        bean_refs,
        *,
        include_history: bool = False,
    ) -> tuple[BeanRecord, ...]:
        return tuple(
            record_from_dict(_object(item))
            for item in _array(
                self._request(
                    "get_many",
                    {
                        "bean_refs": [
                            bean_ref_to_dict(bean_ref) for bean_ref in bean_refs
                        ],
                        "include_history": include_history,
                    },
                )
            )
        )

    def list_records(
        self,
        *,
        run_id: str | None = None,
        statuses: tuple[TrackStatus, ...] | None = None,
    ) -> tuple[BeanRecord, ...]:
        payload: dict[str, object] = {"run_id": run_id}
        if statuses is not None:
            payload["statuses"] = [status.value for status in statuses]
        return tuple(
            record_from_dict(_object(item))
            for item in _array(self._request("list", payload))
        )

    def list_active(self, *, run_id: str | None = None) -> tuple[BeanRecord, ...]:
        return tuple(
            record_from_dict(_object(item))
            for item in _array(self._request("list_active", {"run_id": run_id}))
        )

    def events_since(
        self, after_sequence: int, *, limit: int = 1_000
    ) -> tuple[BeanEvent, ...]:
        return tuple(
            event_from_dict(_object(item))
            for item in _array(
                self._request(
                    "events_since",
                    {"after_sequence": after_sequence, "limit": limit},
                )
            )
        )

    def events_since_compact(
        self, after_sequence: int, *, limit: int = 1_000
    ) -> tuple[BeanEvent, ...]:
        return tuple(
            event_from_dict(_object(item))
            for item in _array(
                self._request(
                    "events_since_compact",
                    {"after_sequence": after_sequence, "limit": limit},
                )
            )
        )

    def event_cursor(self) -> int:
        return int(self._request("event_cursor", {}))

    def hot_state_metrics(self) -> dict[str, int]:
        return {
            key: int(value)
            for key, value in _object(self._request("hot_state_metrics", {})).items()
        }

    def service_metrics(self) -> dict[str, object]:
        return _object(self._request("service_metrics", {}))

    def reset_service_metrics(self) -> dict[str, int]:
        return {
            key: int(value)
            for key, value in _object(
                self._request("reset_service_metrics", {})
            ).items()
        }

    def evict_completed(
        self, *, before_timestamp_ns: int, run_id: str | None = None
    ) -> int:
        return int(
            self._request(
                "evict_completed",
                {
                    "before_timestamp_ns": int(before_timestamp_ns),
                    "run_id": run_id,
                },
            )
        )

    def update_track(
        self,
        track,
        prediction=None,
        *,
        event_id: str | None = None,
    ) -> BeanRecord:
        identifier = event_id or uuid.uuid4().hex
        encoded_track = track_to_dict(track, include_history=False)
        # Only the newest observation crosses IPC on each update. BeanRegistry
        # merges it into hot history, avoiding quadratic metadata copying.
        if track.history:
            encoded_track["history"] = [observation_to_dict(track.history[-1])]
        result = self._request(
            "update_track",
            {
                "track": encoded_track,
                "prediction": (
                    None if prediction is None else prediction_to_dict(prediction)
                ),
                "event_id": identifier,
            },
        )
        return record_from_dict(_object(result))

    def update_tracks(
        self,
        updates,
    ) -> tuple[BeanRecord, ...]:
        encoded_updates = _encode_track_updates(updates)
        return tuple(
            record_from_dict(_object(item))
            for item in _array(
                self._request("update_tracks", {"updates": encoded_updates})
            )
        )

    def update_track_revisions(self, updates) -> dict[BeanRef, int]:
        result: dict[BeanRef, int] = {}
        for item in _array(
            self._request(
                "update_track_revisions",
                {"updates": _encode_track_updates(updates)},
            )
        ):
            value = _object(item)
            result[bean_ref_from_dict(_object(value["bean_ref"]))] = int(
                value["revision"]
            )
        return result

    def update_frame_and_submit_jobs(
        self, updates, jobs
    ) -> tuple[dict[BeanRef, int], tuple[InferenceJob, ...]]:
        response = _object(
            self._request(
                "update_frame_and_submit_jobs",
                {
                    "updates": _encode_track_updates(updates),
                    "jobs": [
                        {
                            "job": inference_job_to_dict(job),
                            "event_id": event_id or job.job_id,
                        }
                        for job, event_id in jobs
                    ],
                },
            )
        )
        revisions: dict[BeanRef, int] = {}
        for raw_item in _array(response.get("revisions", [])):
            item = _object(raw_item)
            revisions[bean_ref_from_dict(_object(item["bean_ref"]))] = int(
                item["revision"]
            )
        canonical_jobs = tuple(
            inference_job_from_dict(_object(item))
            for item in _array(response.get("jobs", []))
        )
        return revisions, canonical_jobs

    def add_enrichment(
        self,
        bean_ref: BeanRef,
        enrichment: Enrichment,
        *,
        event_id: str | None = None,
    ) -> BeanRecord:
        identifier = event_id or enrichment.result_id or uuid.uuid4().hex
        if not enrichment.result_id:
            enrichment = Enrichment(
                enrichment.source,
                enrichment.kind,
                enrichment.value,
                enrichment.timestamp_ns,
                enrichment.version,
                identifier,
                enrichment.confidence,
            )
        return record_from_dict(
            _object(
                self._request(
                    "add_enrichment",
                    {
                        "bean_ref": bean_ref_to_dict(bean_ref),
                        "enrichment": enrichment_to_dict(enrichment),
                        "event_id": identifier,
                    },
                )
            )
        )

    def add_enrichments(self, additions) -> tuple[BeanRecord, ...]:
        return tuple(
            record_from_dict(_object(item))
            for item in _array(
                self._request(
                    "add_enrichments",
                    {
                        "additions": [
                            {
                                "bean_ref": bean_ref_to_dict(bean_ref),
                                "enrichment": enrichment_to_dict(enrichment),
                                "event_id": (
                                    event_id
                                    or enrichment.result_id
                                    or uuid.uuid4().hex
                                ),
                            }
                            for bean_ref, enrichment, event_id in additions
                        ]
                    },
                )
            )
        )

    def submit_inference_job(
        self, job: InferenceJob, *, event_id: str | None = None
    ) -> BeanRecord:
        return record_from_dict(
            _object(
                self._request(
                    "submit_inference_job",
                    {
                        "job": inference_job_to_dict(job),
                        "event_id": event_id or job.job_id,
                    },
                )
            )
        )

    def submit_inference_job_revision(
        self, job: InferenceJob, *, event_id: str | None = None
    ) -> int:
        return int(
            self._request(
                "submit_inference_job_revision",
                {
                    "job": inference_job_to_dict(job),
                    "event_id": event_id or job.job_id,
                },
            )
        )

    def update_inference_job(
        self,
        bean_ref: BeanRef,
        job_id: str,
        status: InferenceStatus,
        timestamp_ns: int,
        *,
        detail: str = "",
        timing_marks_ns: Mapping[str, int] | None = None,
        event_id: str | None = None,
    ) -> BeanRecord:
        return record_from_dict(
            _object(
                self._request(
                    "update_inference_job",
                    {
                        "bean_ref": bean_ref_to_dict(bean_ref),
                        "job_id": job_id,
                        "status": status.value,
                        "timestamp_ns": timestamp_ns,
                        "detail": detail,
                        "timing_marks_ns": dict(timing_marks_ns or {}),
                        "event_id": event_id or uuid.uuid4().hex,
                    },
                )
            )
        )

    def complete_inference_job(
        self,
        bean_ref: BeanRef,
        job_id: str,
        enrichment: Enrichment,
        *,
        timing_marks_ns: Mapping[str, int] | None = None,
        event_id: str | None = None,
    ) -> BeanRecord:
        return record_from_dict(
            _object(
                self._request(
                    "complete_inference_job",
                    {
                        "bean_ref": bean_ref_to_dict(bean_ref),
                        "job_id": job_id,
                        "enrichment": enrichment_to_dict(enrichment),
                        "timing_marks_ns": dict(timing_marks_ns or {}),
                        "event_id": event_id or f"complete:{job_id}",
                    },
                )
            )
        )

    def complete_inference_jobs(self, completions) -> tuple[BeanRecord, ...]:
        return tuple(
            record_from_dict(_object(item))
            for item in _array(
                self._request(
                    "complete_inference_jobs",
                    {"completions": _encode_inference_completions(completions)},
                )
            )
        )

    def complete_inference_jobs_ack(self, completions) -> int:
        """Commit a GPU result batch without echoing materialized records."""

        return int(
            self._request(
                "complete_inference_jobs_ack",
                {"completions": _encode_inference_completions(completions)},
            )
        )

    def set_sorting_decision(
        self,
        bean_ref: BeanRef,
        decision: SortingDecision,
        *,
        event_id: str | None = None,
    ) -> BeanRecord:
        identifier = event_id or decision.decision_id or uuid.uuid4().hex
        return record_from_dict(
            _object(
                self._request(
                    "set_sorting_decision",
                    {
                        "bean_ref": bean_ref_to_dict(bean_ref),
                        "decision": decision_to_dict(decision),
                        "event_id": identifier,
                    },
                )
            )
        )

    def acknowledge_sorting_decision(
        self,
        bean_ref: BeanRef,
        decision_id: str,
        timestamp_ns: int,
        *,
        event_id: str | None = None,
    ) -> BeanRecord:
        return record_from_dict(
            _object(
                self._request(
                    "acknowledge_sorting_decision",
                    {
                        "bean_ref": bean_ref_to_dict(bean_ref),
                        "decision_id": decision_id,
                        "timestamp_ns": timestamp_ns,
                        "event_id": event_id or uuid.uuid4().hex,
                    },
                )
            )
        )

    def record_actuation(
        self,
        bean_ref: BeanRef,
        result: ActuationResult,
        *,
        event_id: str | None = None,
    ) -> BeanRecord:
        return record_from_dict(
            _object(
                self._request(
                    "record_actuation",
                    {
                        "bean_ref": bean_ref_to_dict(bean_ref),
                        "actuation": actuation_to_dict(result),
                        "event_id": event_id or f"actuation:{result.decision_id}",
                    },
                )
            )
        )

    def record_actuation_ack(
        self,
        bean_ref: BeanRef,
        result: ActuationResult,
        *,
        event_id: str | None = None,
    ) -> int:
        """Commit an actuation audit without echoing materialized bean state."""

        payload = {
            "bean_ref": bean_ref_to_dict(bean_ref),
            "actuation": actuation_to_dict(result),
            "event_id": event_id or f"actuation:{result.decision_id}",
        }
        try:
            return int(self._request("record_actuation_ack", payload))
        except RegistryRemoteError as exc:
            if not (
                exc.error_type == "ValueError"
                and "unknown registry operation: record_actuation_ack"
                in exc.remote_message
            ):
                raise
            # Rolling upgrades may briefly pair a new actuator with the previous
            # Registry service. Preserve the durable result while accepting the
            # larger legacy response until Registry is restarted.
            return self.record_actuation(
                bean_ref,
                result,
                event_id=str(payload["event_id"]),
            ).revision

    def close(self) -> None:
        if self._socket is not None:
            self._socket.close(0)
            self._socket = None

    def __enter__(self) -> ZeroMQRegistryClient:  # noqa: PYI034 - Python 3.10
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        self.close()

    def _request(self, operation: str, payload: Mapping[str, object]) -> object:
        current_thread = threading.get_ident()
        if self._thread_id is None:
            self._thread_id = current_thread
        elif self._thread_id != current_thread:
            raise RegistryTransportError(
                "ZeroMQRegistryClient is thread-affine; create one client per thread"
            )
        request_id = uuid.uuid4().hex
        request = {
            "schema": REGISTRY_SCHEMA,
            "request_id": request_id,
            "operation": operation,
            "payload": dict(payload),
        }
        socket = self._ensure_socket()
        try:
            socket.send(_encode(request))
            if not socket.poll(self.timeout_ms, zmq.POLLIN):
                raise RegistryTransportError(
                    f"BeanRegistry did not answer {operation!r} within "
                    f"{self.timeout_ms} ms"
                )
            response = _object(json.loads(socket.recv().decode("utf-8")))
        except Exception:
            self._reset_socket()
            raise
        if response.get("schema") != REGISTRY_SCHEMA:
            raise RegistryTransportError("invalid BeanRegistry response schema")
        if response.get("request_id") != request_id:
            raise RegistryTransportError(
                "BeanRegistry response ID does not match request"
            )
        if not response.get("ok"):
            error = _object(response.get("error", {}))
            raise RegistryRemoteError(
                str(error.get("type", "RegistryError")),
                str(error.get("message", "unknown registry failure")),
            )
        return response.get("result")

    def _ensure_socket(self) -> zmq.Socket:
        if self._socket is None:
            socket = self.context.socket(zmq.REQ)
            socket.setsockopt(zmq.LINGER, 0)
            socket.setsockopt(zmq.MAXMSGSIZE, MAX_MESSAGE_BYTES)
            socket.setsockopt(zmq.SNDHWM, 1_024)
            socket.setsockopt(zmq.RCVHWM, 1_024)
            socket.connect(self.command_endpoint)
            self._socket = socket
        return self._socket

    def _reset_socket(self) -> None:
        if self._socket is not None:
            self._socket.close(0)
            self._socket = None


class ZeroMQRegistrySubscriber:
    """Receive replaceable registry events; query the registry after any gap."""

    def __init__(
        self,
        event_endpoint: str,
        *,
        topic: str = "",
        context: zmq.Context | None = None,
        capacity: int = 4_096,
    ) -> None:
        self.context = context or zmq.Context.instance()
        self.socket = self.context.socket(zmq.SUB)
        self.socket.setsockopt(zmq.LINGER, 0)
        self.socket.setsockopt(zmq.MAXMSGSIZE, MAX_MESSAGE_BYTES)
        self.socket.setsockopt(zmq.RCVHWM, max(1, int(capacity)))
        self.socket.setsockopt_string(zmq.SUBSCRIBE, topic)
        self.socket.connect(event_endpoint)
        self._pending: deque[BeanEvent] = deque()

    def receive(self, *, timeout_ms: int | None = None) -> BeanEvent | None:
        if self._pending:
            return self._pending.popleft()
        events = self.receive_many(timeout_ms=timeout_ms)
        if not events:
            return None
        self._pending.extend(events[1:])
        return events[0]

    def receive_many(
        self, *, timeout_ms: int | None = None
    ) -> tuple[BeanEvent, ...]:
        """Receive one transport envelope containing one or more journal events."""

        if self._pending:
            events = tuple(self._pending)
            self._pending.clear()
            return events
        if timeout_ms is not None and not self.socket.poll(
            max(0, int(timeout_ms)), zmq.POLLIN
        ):
            return ()
        _topic, encoded = self.socket.recv_multipart()
        envelope = _object(json.loads(encoded.decode("utf-8")))
        if envelope.get("schema") != REGISTRY_SCHEMA:
            raise RegistryTransportError("invalid BeanRegistry event schema")
        if "events" in envelope:
            events = tuple(
                event_from_dict(_object(item))
                for item in _array(envelope["events"])
            )
            if not events:
                raise RegistryTransportError("empty BeanRegistry event batch")
            return events
        return (event_from_dict(_object(envelope["event"])),)

    def close(self) -> None:
        self.socket.close(0)

    def __enter__(self) -> ZeroMQRegistrySubscriber:  # noqa: PYI034 - Python 3.10
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        self.close()


def _encode_track_updates(updates) -> list[dict[str, object]]:
    encoded_updates: list[dict[str, object]] = []
    for track, prediction, event_id in updates:
        encoded_track = track_to_dict(track, include_history=False)
        if track.history:
            encoded_track["history"] = [observation_to_dict(track.history[-1])]
        encoded_updates.append(
            {
                "track": encoded_track,
                "prediction": (
                    None if prediction is None else prediction_to_dict(prediction)
                ),
                "event_id": event_id or uuid.uuid4().hex,
            }
        )
    return encoded_updates


def _encode_inference_completions(completions) -> list[dict[str, object]]:
    return [
        {
            "bean_ref": bean_ref_to_dict(bean_ref),
            "job_id": job_id,
            "enrichment": enrichment_to_dict(enrichment),
            "timing_marks_ns": dict(timing_marks_ns or {}),
            "event_id": event_id or f"complete:{job_id}",
        }
        for (
            bean_ref,
            job_id,
            enrichment,
            timing_marks_ns,
            event_id,
        ) in completions
    ]


def _decode_track_updates(values: list[object]):
    updates = []
    for raw_update in values:
        update = _object(raw_update)
        prediction_value = update.get("prediction")
        updates.append(
            (
                track_from_dict(_object(update["track"])),
                (
                    None
                    if prediction_value is None
                    else prediction_from_dict(_object(prediction_value))
                ),
                str(update.get("event_id") or uuid.uuid4().hex),
            )
        )
    return tuple(updates)


def _encode(value: object) -> bytes:
    try:
        return json.dumps(
            value, allow_nan=False, separators=(",", ":"), sort_keys=True
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise RegistryTransportError(
            f"registry message is not finite JSON: {exc}"
        ) from exc


def _object(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise TypeError("registry message value must be an object")
    return value


def _array(value: object) -> list[object]:
    if not isinstance(value, list):
        raise TypeError("registry message value must be an array")
    return value
