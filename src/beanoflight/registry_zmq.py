"""Versioned JSON-over-ZeroMQ IPC for BeanRegistry."""

from __future__ import annotations

import json
import queue
import threading
import uuid
from collections.abc import Mapping

import zmq

from .models import BeanEvent, BeanRef, TrackStatus
from .registry import BeanRegistry
from .registry_models import (
    REGISTRY_SCHEMA,
    BeanRecord,
    Enrichment,
    SortingDecision,
    bean_ref_from_dict,
    bean_ref_to_dict,
    decision_from_dict,
    decision_to_dict,
    enrichment_from_dict,
    enrichment_to_dict,
    event_from_dict,
    event_to_dict,
    observation_to_dict,
    prediction_from_dict,
    prediction_to_dict,
    record_from_dict,
    record_to_dict,
    track_from_dict,
    track_to_dict,
)

MAX_MESSAGE_BYTES = 4 * 1024 * 1024


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
    ) -> None:
        self.registry = registry
        self.command_endpoint = command_endpoint
        self.event_endpoint = event_endpoint
        self.context = context or zmq.Context.instance()
        self._event_queue = registry.subscribe(capacity=event_capacity)

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
        request_id = ""
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
        return _encode(response)

    def _dispatch(
        self, operation: str, payload: Mapping[str, object], request_id: str
    ) -> object:
        if operation == "ping":
            return {"service": "BeanRegistry", "schema": REGISTRY_SCHEMA}
        if operation == "get":
            record = self.registry.get(
                bean_ref_from_dict(_object(payload["bean_ref"]))
            )
            return record_to_dict(record)
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
                    else tuple(TrackStatus(str(item)) for item in _array(statuses_value))
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
        if operation == "add_enrichment":
            record = self.registry.add_enrichment(
                bean_ref_from_dict(_object(payload["bean_ref"])),
                enrichment_from_dict(_object(payload["enrichment"])),
                event_id=str(payload.get("event_id") or request_id),
            )
            return record_to_dict(record, include_history=False)
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
        raise ValueError(f"unknown registry operation: {operation}")

    def _publish_waiting(self, socket: zmq.Socket) -> None:
        while True:
            try:
                event = self._event_queue.get_nowait()
            except queue.Empty:
                return
            envelope = {"schema": REGISTRY_SCHEMA, "event": event_to_dict(event)}
            socket.send_multipart((event.kind.encode("utf-8"), _encode(envelope)))


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

    def get(self, bean_ref: BeanRef) -> BeanRecord:
        return record_from_dict(
            _object(self._request("get", {"bean_ref": bean_ref_to_dict(bean_ref)}))
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
            for item in _array(
                self._request("list_active", {"run_id": run_id})
            )
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
        encoded_updates: list[dict[str, object]] = []
        for track, prediction, event_id in updates:
            encoded_track = track_to_dict(track, include_history=False)
            if track.history:
                encoded_track["history"] = [observation_to_dict(track.history[-1])]
            encoded_updates.append(
                {
                    "track": encoded_track,
                    "prediction": (
                        None
                        if prediction is None
                        else prediction_to_dict(prediction)
                    ),
                    "event_id": event_id or uuid.uuid4().hex,
                }
            )
        return tuple(
            record_from_dict(_object(item))
            for item in _array(
                self._request("update_tracks", {"updates": encoded_updates})
            )
        )

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
            raise RegistryTransportError("BeanRegistry response ID does not match request")
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

    def receive(self, *, timeout_ms: int | None = None) -> BeanEvent | None:
        if timeout_ms is not None and not self.socket.poll(
            max(0, int(timeout_ms)), zmq.POLLIN
        ):
            return None
        _topic, encoded = self.socket.recv_multipart()
        envelope = _object(json.loads(encoded.decode("utf-8")))
        if envelope.get("schema") != REGISTRY_SCHEMA:
            raise RegistryTransportError("invalid BeanRegistry event schema")
        return event_from_dict(_object(envelope["event"]))

    def close(self) -> None:
        self.socket.close(0)

    def __enter__(self) -> ZeroMQRegistrySubscriber:  # noqa: PYI034 - Python 3.10
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        self.close()


def _encode(value: object) -> bytes:
    try:
        return json.dumps(
            value, allow_nan=False, separators=(",", ":"), sort_keys=True
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise RegistryTransportError(f"registry message is not finite JSON: {exc}") from exc


def _object(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise TypeError("registry message value must be an object")
    return value


def _array(value: object) -> list[object]:
    if not isinstance(value, list):
        raise TypeError("registry message value must be an array")
    return value
