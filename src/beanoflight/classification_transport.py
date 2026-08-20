"""Acknowledged low-latency inference evidence transport to BeanoSorter."""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass

import zmq

from .classification import CLASSIFICATION_EVIDENCE
from .registry_models import (
    Enrichment,
    InferenceJob,
    enrichment_from_dict,
    enrichment_to_dict,
    inference_job_from_dict,
    inference_job_to_dict,
)

DIRECT_EVIDENCE_SCHEMA = "beanoflight-direct-evidence/v2"
DIRECT_EVIDENCE_ACK_SCHEMA = "beanoflight-direct-evidence-ack/v1"
DEFAULT_DIRECT_EVIDENCE_ENDPOINT = (
    "ipc:///tmp/beanoflight-inference-evidence.ipc"
)
MAX_DIRECT_EVIDENCE_BYTES = 1024 * 1024
MAX_DIRECT_EVIDENCE_ITEMS = 16


class DirectEvidenceTransportError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class DirectInferenceEvidence:
    job: InferenceJob
    enrichment: Enrichment


@dataclass(frozen=True, slots=True)
class DirectEvidenceBatch:
    batch_id: str
    sent_monotonic_ns: int
    items: tuple[DirectInferenceEvidence, ...]


class ZeroMQDirectEvidencePublisher:
    """Low-latency acknowledged producer with bounded retry."""

    def __init__(
        self,
        endpoint: str = DEFAULT_DIRECT_EVIDENCE_ENDPOINT,
        *,
        context: zmq.Context | None = None,
        capacity: int = 256,
        acknowledgement_timeout_ms: int = 5,
        maximum_attempts: int = 3,
    ) -> None:
        self.endpoint = endpoint
        self.context = context or zmq.Context.instance()
        self.capacity = max(1, int(capacity))
        self.acknowledgement_timeout_ms = max(
            1, int(acknowledgement_timeout_ms)
        )
        self.maximum_attempts = max(1, int(maximum_attempts))
        self.socket = self._new_socket()

    def _new_socket(self) -> zmq.Socket:
        socket = self.context.socket(zmq.REQ)
        socket.setsockopt(zmq.LINGER, 0)
        socket.setsockopt(zmq.MAXMSGSIZE, MAX_DIRECT_EVIDENCE_BYTES)
        socket.setsockopt(zmq.SNDHWM, self.capacity)
        socket.setsockopt(zmq.RCVHWM, self.capacity)
        socket.connect(self.endpoint)
        return socket

    def _reset_socket(self) -> None:
        self.socket.setsockopt(zmq.LINGER, 0)
        self.socket.close(0)
        self.socket = self._new_socket()

    def send_batch(
        self,
        batch_id: str,
        items: tuple[DirectInferenceEvidence, ...],
    ) -> tuple[bool, int]:
        if not 1 <= len(items) <= MAX_DIRECT_EVIDENCE_ITEMS:
            raise DirectEvidenceTransportError(
                "direct evidence batch must contain between 1 and 16 items"
            )
        for item in items:
            if item.enrichment.kind != CLASSIFICATION_EVIDENCE:
                raise DirectEvidenceTransportError(
                    "direct transport accepts classification evidence only"
                )
            if item.enrichment.result_id != item.job.job_id:
                raise DirectEvidenceTransportError(
                    "direct evidence result must identify its inference job"
                )
        sent_ns = time.monotonic_ns()
        encoded = _encode(
            {
                "schema": DIRECT_EVIDENCE_SCHEMA,
                "batch_id": str(batch_id),
                "sent_monotonic_ns": sent_ns,
                "items": [
                    {
                        "job": inference_job_to_dict(item.job),
                        "enrichment": enrichment_to_dict(item.enrichment),
                    }
                    for item in items
                ],
            }
        )
        if len(encoded) > MAX_DIRECT_EVIDENCE_BYTES:
            raise DirectEvidenceTransportError(
                "direct evidence batch exceeds transport size limit"
            )
        for attempt in range(self.maximum_attempts):
            try:
                self.socket.send(encoded, flags=zmq.NOBLOCK)
                if self.socket.poll(
                    self.acknowledgement_timeout_ms, zmq.POLLIN
                ):
                    acknowledgement = _object(
                        json.loads(self.socket.recv().decode("utf-8"))
                    )
                    if (
                        acknowledgement.get("schema")
                        == DIRECT_EVIDENCE_ACK_SCHEMA
                        and acknowledgement.get("batch_id") == str(batch_id)
                        and acknowledgement.get("ok") is True
                    ):
                        return True, sent_ns
            except (ValueError, UnicodeDecodeError, zmq.ZMQError):
                pass
            if attempt + 1 < self.maximum_attempts:
                self._reset_socket()
        self._reset_socket()
        return False, sent_ns

    def close(self) -> None:
        self.socket.close(0)


class ZeroMQDirectEvidenceReceiver:
    """Acknowledging REP consumer owned by the real-time sorter worker."""

    def __init__(
        self,
        endpoint: str = DEFAULT_DIRECT_EVIDENCE_ENDPOINT,
        *,
        context: zmq.Context | None = None,
        capacity: int = 256,
    ) -> None:
        self.endpoint = endpoint
        self.context = context or zmq.Context.instance()
        self.socket = self.context.socket(zmq.REP)
        self.socket.setsockopt(zmq.LINGER, 0)
        self.socket.setsockopt(zmq.MAXMSGSIZE, MAX_DIRECT_EVIDENCE_BYTES)
        self.socket.setsockopt(zmq.RCVHWM, max(1, int(capacity)))
        self.socket.bind(endpoint)
        self.endpoint = self.socket.getsockopt_string(zmq.LAST_ENDPOINT)

    def receive_batch(
        self,
        *,
        timeout_ms: int | None = None,
        accept: Callable[[DirectEvidenceBatch, int], bool] | None = None,
    ) -> DirectEvidenceBatch | None:
        if timeout_ms is not None and not self.socket.poll(
            max(0, int(timeout_ms)), zmq.POLLIN
        ):
            return None
        encoded = self.socket.recv()
        batch_id = ""
        received_ns = time.monotonic_ns()
        try:
            if len(encoded) > MAX_DIRECT_EVIDENCE_BYTES:
                raise DirectEvidenceTransportError(
                    "direct evidence batch exceeds transport size limit"
                )
            message = _object(json.loads(encoded.decode("utf-8")))
            if message.get("schema") != DIRECT_EVIDENCE_SCHEMA:
                raise DirectEvidenceTransportError("invalid direct evidence schema")
            batch_id = str(message.get("batch_id", ""))
            sent_ns = int(message.get("sent_monotonic_ns", 0))
            raw_items = _array(message.get("items"))
            if (
                not batch_id
                or sent_ns <= 0
                or not 1 <= len(raw_items) <= MAX_DIRECT_EVIDENCE_ITEMS
            ):
                raise DirectEvidenceTransportError("invalid direct evidence batch")
            items = tuple(
                DirectInferenceEvidence(
                    inference_job_from_dict(_object(_object(raw)["job"])),
                    enrichment_from_dict(_object(_object(raw)["enrichment"])),
                )
                for raw in raw_items
            )
            for item in items:
                if (
                    item.enrichment.kind != CLASSIFICATION_EVIDENCE
                    or item.enrichment.result_id != item.job.job_id
                ):
                    raise DirectEvidenceTransportError(
                        "invalid classification evidence item"
                    )
            batch = DirectEvidenceBatch(batch_id, sent_ns, items)
        except Exception:
            self.socket.send(
                _encode(
                    {
                        "schema": DIRECT_EVIDENCE_ACK_SCHEMA,
                        "batch_id": batch_id,
                        "ok": False,
                    }
                )
            )
            raise
        try:
            admitted = accept is None or bool(accept(batch, received_ns))
        except Exception:
            self.socket.send(
                _encode(
                    {
                        "schema": DIRECT_EVIDENCE_ACK_SCHEMA,
                        "batch_id": batch_id,
                        "ok": False,
                    }
                )
            )
            raise
        if not admitted:
            self.socket.send(
                _encode(
                    {
                        "schema": DIRECT_EVIDENCE_ACK_SCHEMA,
                        "batch_id": batch_id,
                        "ok": False,
                    }
                )
            )
            return None
        self.socket.send(
            _encode(
                {
                    "schema": DIRECT_EVIDENCE_ACK_SCHEMA,
                    "batch_id": batch_id,
                    "ok": True,
                    "received_monotonic_ns": received_ns,
                }
            )
        )
        return batch

    def close(self) -> None:
        self.socket.close(0)


def _encode(value: object) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _object(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise DirectEvidenceTransportError(
            "direct evidence message value must be an object"
        )
    return value


def _array(value: object) -> list[object]:
    if not isinstance(value, list):
        raise DirectEvidenceTransportError(
            "direct evidence message value must be an array"
        )
    return value
