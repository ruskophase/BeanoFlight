"""Acknowledged low-latency inference evidence transport to BeanoSorter."""

from __future__ import annotations

import json
import os
import queue
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field

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
from .sorting_context_transport import (
    SortingContext,
    sorting_context_from_dict,
    sorting_context_to_dict,
)

DIRECT_EVIDENCE_SCHEMA = "beanoflight-direct-evidence/v3"
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
    sorting_context: SortingContext | None = None


@dataclass(frozen=True, slots=True)
class DirectEvidenceBatch:
    batch_id: str
    sent_monotonic_ns: int
    items: tuple[DirectInferenceEvidence, ...]


@dataclass(frozen=True, slots=True)
class DirectEvidenceDelivery:
    """Immutable audit snapshot for one asynchronous delivery."""

    batch_id: str
    queued_monotonic_ns: int
    first_sent_monotonic_ns: int
    completed_monotonic_ns: int
    receiver_received_monotonic_ns: int
    attempts: int
    acknowledged: bool
    terminal: bool


@dataclass(slots=True)
class DirectEvidenceReceipt:
    """Thread-safe handle completed by the publisher's transport worker."""

    batch_id: str
    queued_monotonic_ns: int
    first_sent_monotonic_ns: int = 0
    completed_monotonic_ns: int = 0
    receiver_received_monotonic_ns: int = 0
    attempts: int = 0
    acknowledged: bool = False
    terminal: bool = False
    _finished: threading.Event = field(
        default_factory=threading.Event, init=False, repr=False
    )
    _lock: threading.Lock = field(
        default_factory=threading.Lock, init=False, repr=False
    )

    def wait(self, timeout: float | None = None) -> DirectEvidenceDelivery:
        self._finished.wait(timeout)
        return self.snapshot()

    def snapshot(self) -> DirectEvidenceDelivery:
        with self._lock:
            return DirectEvidenceDelivery(
                self.batch_id,
                self.queued_monotonic_ns,
                self.first_sent_monotonic_ns,
                self.completed_monotonic_ns,
                self.receiver_received_monotonic_ns,
                self.attempts,
                self.acknowledged,
                self.terminal,
            )

    def _mark_attempt(self, sent_monotonic_ns: int) -> None:
        with self._lock:
            if not self.first_sent_monotonic_ns:
                self.first_sent_monotonic_ns = sent_monotonic_ns
            self.attempts += 1

    def _complete(
        self,
        *,
        acknowledged: bool,
        receiver_received_monotonic_ns: int = 0,
    ) -> None:
        with self._lock:
            if self.terminal:
                return
            self.completed_monotonic_ns = time.monotonic_ns()
            self.receiver_received_monotonic_ns = int(
                receiver_received_monotonic_ns
            )
            self.acknowledged = bool(acknowledged)
            self.terminal = True
        self._finished.set()


@dataclass(slots=True)
class _OutboundEvidence:
    receipt: DirectEvidenceReceipt
    items: tuple[DirectInferenceEvidence, ...]
    encoded: bytes = b""
    retry_due_monotonic_ns: int = 0


class ZeroMQDirectEvidencePublisher:
    """Non-blocking producer with acknowledgement and retry off the hot path."""

    def __init__(
        self,
        endpoint: str = DEFAULT_DIRECT_EVIDENCE_ENDPOINT,
        *,
        context: zmq.Context | None = None,
        capacity: int = 256,
        acknowledgement_timeout_ms: int = 5,
        maximum_attempts: int = 3,
        acknowledgement_endpoint: str | None = None,
    ) -> None:
        self.endpoint = endpoint
        self.context, self._owns_context = _direct_transport_context(
            endpoint,
            context,
        )
        self.capacity = max(1, int(capacity))
        self.acknowledgement_timeout_ms = max(
            1, int(acknowledgement_timeout_ms)
        )
        self.maximum_attempts = max(1, int(maximum_attempts))
        self.acknowledgement_endpoint = (
            acknowledgement_endpoint or _derived_acknowledgement_endpoint(endpoint)
        )
        self._outbound: queue.Queue[_OutboundEvidence] = queue.Queue(
            maxsize=self.capacity
        )
        self._closed = False
        self._state_lock = threading.Lock()
        self._wake_read_fd, self._wake_write_fd = os.pipe()
        os.set_blocking(self._wake_read_fd, False)
        os.set_blocking(self._wake_write_fd, False)
        self._stop = threading.Event()
        self._worker_ready = threading.Event()
        self._worker = threading.Thread(
            target=self._delivery_loop,
            name="beano-direct-evidence-delivery",
            daemon=True,
        )
        self._worker.start()
        if not self._worker_ready.wait(1.0):
            raise DirectEvidenceTransportError(
                "direct evidence delivery worker did not become ready"
            )

    def send_batch(
        self,
        batch_id: str,
        items: tuple[DirectInferenceEvidence, ...],
    ) -> DirectEvidenceReceipt:
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
            if (
                item.sorting_context is not None
                and item.sorting_context.track.bean_ref != item.job.bean_ref
            ):
                raise DirectEvidenceTransportError(
                    "direct evidence context must identify its inference job bean"
                )
        queued_ns = time.monotonic_ns()
        receipt = DirectEvidenceReceipt(str(batch_id), queued_ns)
        with self._state_lock:
            if self._closed:
                raise DirectEvidenceTransportError(
                    "direct evidence publisher is closed"
                )
            try:
                self._outbound.put_nowait(
                    _OutboundEvidence(receipt, tuple(items))
                )
            except queue.Full as exc:
                raise DirectEvidenceTransportError(
                    "direct evidence delivery queue is full"
                ) from exc
        self._wake_worker()
        return receipt

    def close(self) -> None:
        with self._state_lock:
            if self._closed:
                return
            self._closed = True
        self._stop.set()
        self._wake_worker()
        self._worker.join(2.0)
        os.close(self._wake_read_fd)
        os.close(self._wake_write_fd)
        if self._owns_context:
            self.context.destroy(linger=0)

    def _wake_worker(self) -> None:
        try:
            os.write(self._wake_write_fd, b"\x00")
        except BlockingIOError:
            pass

    def _delivery_loop(self) -> None:
        sender = self.context.socket(zmq.PUSH)
        sender.setsockopt(zmq.LINGER, 0)
        sender.setsockopt(zmq.MAXMSGSIZE, MAX_DIRECT_EVIDENCE_BYTES)
        sender.setsockopt(zmq.SNDHWM, self.capacity)
        sender.connect(self.endpoint)
        acknowledgements = self.context.socket(zmq.PULL)
        acknowledgements.setsockopt(zmq.LINGER, 0)
        acknowledgements.setsockopt(zmq.MAXMSGSIZE, MAX_DIRECT_EVIDENCE_BYTES)
        acknowledgements.setsockopt(zmq.RCVHWM, self.capacity)
        acknowledgements.connect(self.acknowledgement_endpoint)
        poller = zmq.Poller()
        poller.register(acknowledgements, zmq.POLLIN)
        poller.register(self._wake_read_fd, zmq.POLLIN)
        pending: OrderedDict[str, _OutboundEvidence] = OrderedDict()
        self._worker_ready.set()
        try:
            while not self._stop.is_set() or not self._outbound.empty() or pending:
                self._drain_outbound(sender, pending)
                self._drain_acknowledgements(acknowledgements, pending)
                self._retry_due(sender, pending)
                if self._stop.is_set() and not self._outbound.empty():
                    continue
                timeout_ms = _next_delivery_timeout_ms(
                    pending,
                    default_ms=100,
                )
                readable = dict(poller.poll(timeout_ms))
                if self._wake_read_fd in readable:
                    _drain_file_descriptor(self._wake_read_fd)
                if acknowledgements in readable:
                    self._drain_acknowledgements(acknowledgements, pending)
        finally:
            for outbound in pending.values():
                outbound.receipt._complete(acknowledged=False)
                self._outbound.task_done()
            while True:
                try:
                    outbound = self._outbound.get_nowait()
                except queue.Empty:
                    break
                outbound.receipt._complete(acknowledged=False)
                self._outbound.task_done()
            sender.close(0)
            acknowledgements.close(0)

    def _drain_outbound(
        self,
        sender: zmq.Socket,
        pending: OrderedDict[str, _OutboundEvidence],
    ) -> None:
        while True:
            try:
                outbound = self._outbound.get_nowait()
            except queue.Empty:
                return
            if outbound.receipt.batch_id in pending:
                outbound.receipt._complete(acknowledged=False)
                self._outbound.task_done()
                continue
            pending[outbound.receipt.batch_id] = outbound
            self._send(sender, outbound)
            if outbound.receipt.snapshot().terminal:
                pending.pop(outbound.receipt.batch_id, None)
                self._outbound.task_done()

    def _drain_acknowledgements(
        self,
        acknowledgements: zmq.Socket,
        pending: OrderedDict[str, _OutboundEvidence],
    ) -> None:
        while acknowledgements.poll(0, zmq.POLLIN):
            try:
                message = _object(
                    json.loads(acknowledgements.recv().decode("utf-8"))
                )
            except (ValueError, UnicodeDecodeError, zmq.ZMQError):
                continue
            if message.get("schema") != DIRECT_EVIDENCE_ACK_SCHEMA:
                continue
            batch_id = str(message.get("batch_id", ""))
            outbound = pending.get(batch_id)
            if outbound is None:
                continue
            if message.get("ok") is True:
                pending.pop(batch_id, None)
                outbound.receipt._complete(
                    acknowledged=True,
                    receiver_received_monotonic_ns=int(
                        message.get("received_monotonic_ns", 0)
                    ),
                )
                self._outbound.task_done()
            else:
                outbound.retry_due_monotonic_ns = time.monotonic_ns()

    def _retry_due(
        self,
        sender: zmq.Socket,
        pending: OrderedDict[str, _OutboundEvidence],
    ) -> None:
        now_ns = time.monotonic_ns()
        for batch_id, outbound in tuple(pending.items()):
            if outbound.retry_due_monotonic_ns > now_ns:
                continue
            if outbound.receipt.snapshot().attempts >= self.maximum_attempts:
                pending.pop(batch_id, None)
                outbound.receipt._complete(acknowledged=False)
                self._outbound.task_done()
                continue
            self._send(sender, outbound)
            if outbound.receipt.snapshot().terminal:
                pending.pop(batch_id, None)
                self._outbound.task_done()

    def _send(self, sender: zmq.Socket, outbound: _OutboundEvidence) -> None:
        sent_ns = time.monotonic_ns()
        if not outbound.encoded:
            outbound.encoded = _encode(
                {
                    "schema": DIRECT_EVIDENCE_SCHEMA,
                    "batch_id": outbound.receipt.batch_id,
                    "sent_monotonic_ns": sent_ns,
                    "items": [
                        {
                            "job": inference_job_to_dict(item.job),
                            "enrichment": enrichment_to_dict(item.enrichment),
                            "sorting_context": (
                                None
                                if item.sorting_context is None
                                else sorting_context_to_dict(
                                    item.sorting_context
                                )
                            ),
                        }
                        for item in outbound.items
                    ],
                }
            )
            if len(outbound.encoded) > MAX_DIRECT_EVIDENCE_BYTES:
                outbound.receipt._complete(acknowledged=False)
                return
        try:
            sender.send(outbound.encoded, flags=zmq.NOBLOCK)
        except zmq.Again:
            pass
        outbound.receipt._mark_attempt(sent_ns)
        outbound.retry_due_monotonic_ns = sent_ns + round(
            self.acknowledgement_timeout_ms * 1_000_000
        )


class ZeroMQDirectEvidenceReceiver:
    """Acknowledging PULL consumer owned by the real-time sorter worker."""

    def __init__(
        self,
        endpoint: str = DEFAULT_DIRECT_EVIDENCE_ENDPOINT,
        *,
        context: zmq.Context | None = None,
        capacity: int = 256,
        acknowledgement_endpoint: str | None = None,
    ) -> None:
        self.endpoint = endpoint
        self.context, self._owns_context = _direct_transport_context(
            endpoint,
            context,
        )
        self.acknowledgement_endpoint = (
            acknowledgement_endpoint or _derived_acknowledgement_endpoint(endpoint)
        )
        self.socket = self.context.socket(zmq.PULL)
        self.socket.setsockopt(zmq.LINGER, 0)
        self.socket.setsockopt(zmq.MAXMSGSIZE, MAX_DIRECT_EVIDENCE_BYTES)
        self.socket.setsockopt(zmq.RCVHWM, max(1, int(capacity)))
        self.socket.bind(endpoint)
        self.endpoint = self.socket.getsockopt_string(zmq.LAST_ENDPOINT)
        self.acknowledgements = self.context.socket(zmq.PUSH)
        self.acknowledgements.setsockopt(zmq.LINGER, 0)
        self.acknowledgements.setsockopt(zmq.SNDHWM, max(1, int(capacity)))
        self.acknowledgements.bind(self.acknowledgement_endpoint)

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
                    (
                        None
                        if _object(raw).get("sorting_context") is None
                        else sorting_context_from_dict(
                            _object(raw).get("sorting_context")
                        )
                    ),
                )
                for raw in raw_items
            )
            for item in items:
                if (
                    item.enrichment.kind != CLASSIFICATION_EVIDENCE
                    or item.enrichment.result_id != item.job.job_id
                    or (
                        item.sorting_context is not None
                        and item.sorting_context.track.bean_ref
                        != item.job.bean_ref
                    )
                ):
                    raise DirectEvidenceTransportError(
                        "invalid classification evidence item"
                    )
            batch = DirectEvidenceBatch(batch_id, sent_ns, items)
        except Exception:
            self.acknowledgements.send(
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
            self.acknowledgements.send(
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
            self.acknowledgements.send(
                _encode(
                    {
                        "schema": DIRECT_EVIDENCE_ACK_SCHEMA,
                        "batch_id": batch_id,
                        "ok": False,
                    }
                )
            )
            return None
        self.acknowledgements.send(
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
        self.acknowledgements.close(0)
        if self._owns_context:
            self.context.destroy(linger=0)


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


def _derived_acknowledgement_endpoint(endpoint: str) -> str:
    if endpoint.startswith(("ipc://", "inproc://")):
        return f"{endpoint}.ack"
    raise DirectEvidenceTransportError(
        "a separate acknowledgement endpoint is required for non-local "
        "direct evidence transport"
    )


def _direct_transport_context(
    endpoint: str,
    context: zmq.Context | None,
) -> tuple[zmq.Context, bool]:
    """Give IPC evidence its own native I/O worker instead of bulk traffic's.

    An in-process endpoint must share its caller's singleton context.  IPC and
    explicitly addressed network transports can use an independently owned
    context, preventing Registry, crop and trajectory sockets from creating
    head-of-line blocking in the direct evidence I/O worker.
    """

    if context is not None:
        return context, False
    if endpoint.startswith("inproc://"):
        return zmq.Context.instance(), False
    return zmq.Context(io_threads=1), True


def _next_delivery_timeout_ms(
    pending: OrderedDict[str, _OutboundEvidence],
    *,
    default_ms: int,
) -> int:
    if not pending:
        return max(1, int(default_ms))
    now_ns = time.monotonic_ns()
    next_due_ns = min(item.retry_due_monotonic_ns for item in pending.values())
    remaining_ns = max(0, next_due_ns - now_ns)
    return min(max(0, int(remaining_ns / 1_000_000)), max(1, int(default_ms)))


def _drain_file_descriptor(file_descriptor: int) -> None:
    while True:
        try:
            if not os.read(file_descriptor, 4_096):
                return
        except BlockingIOError:
            return
