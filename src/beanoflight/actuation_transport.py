"""Acknowledged transport for approved gate plans to BeanoActuator."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass

import zmq

from .models import BeanRef
from .registry_models import bean_ref_from_dict, bean_ref_to_dict

ACTUATION_PLAN_SCHEMA = "beanoflight-actuation-plan/v1"
ACTUATION_ACK_SCHEMA = "beanoflight-actuation-plan-ack/v1"
DEFAULT_ACTUATION_ENDPOINT = "ipc:///tmp/beanoflight-actuation-plans.ipc"
MAX_ACTUATION_PLAN_BYTES = 64 * 1024


class ActuationTransportError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ActuationPlan:
    decision_id: str
    bean_ref: BeanRef
    gate_indices: tuple[int, ...]
    open_monotonic_ns: int
    close_monotonic_ns: int
    crossing_monotonic_ns: int
    open_source_ns: int
    close_source_ns: int
    crossing_source_ns: int
    run_clock_source_ns: int
    run_clock_monotonic_ns: int
    run_clock_scale_ppb: int

    def validate(self) -> None:
        if not self.decision_id or not self.gate_indices:
            raise ActuationTransportError("actuation plan ID and gates are required")
        if any(gate < -10 or gate > 10 for gate in self.gate_indices):
            raise ActuationTransportError("actuation gate index must be -10 through +10")
        if len(set(self.gate_indices)) != len(self.gate_indices):
            raise ActuationTransportError("actuation plan gates must be unique")
        if not (
            0 < self.open_monotonic_ns
            <= self.crossing_monotonic_ns
            <= self.close_monotonic_ns
        ):
            raise ActuationTransportError("invalid monotonic actuation window")
        if not (
            0 <= self.open_source_ns
            <= self.crossing_source_ns
            <= self.close_source_ns
        ):
            raise ActuationTransportError("invalid source-clock actuation window")
        if self.run_clock_monotonic_ns <= 0 or self.run_clock_scale_ppb <= 0:
            raise ActuationTransportError("actuation plan run clock is invalid")


@dataclass(frozen=True, slots=True)
class ActuationPlanReceipt:
    accepted: bool
    detail: str
    received_monotonic_ns: int


class ZeroMQActuationPlanPublisher:
    """Single-sorter REQ client with bounded acknowledgement retry."""

    def __init__(
        self,
        endpoint: str = DEFAULT_ACTUATION_ENDPOINT,
        *,
        context: zmq.Context | None = None,
        timeout_ms: int = 5,
        maximum_attempts: int = 3,
    ) -> None:
        self.endpoint = endpoint
        self.context = context or zmq.Context.instance()
        self.timeout_ms = max(1, int(timeout_ms))
        self.maximum_attempts = max(1, int(maximum_attempts))
        self.socket = self._new_socket()

    def _new_socket(self) -> zmq.Socket:
        socket = self.context.socket(zmq.REQ)
        socket.setsockopt(zmq.LINGER, 0)
        socket.setsockopt(zmq.MAXMSGSIZE, MAX_ACTUATION_PLAN_BYTES)
        socket.connect(self.endpoint)
        return socket

    def _reset(self) -> None:
        self.socket.close(0)
        self.socket = self._new_socket()

    def submit(self, plan: ActuationPlan) -> ActuationPlanReceipt:
        plan.validate()
        encoded = _encode(
            {
                "schema": ACTUATION_PLAN_SCHEMA,
                "sent_monotonic_ns": time.monotonic_ns(),
                "plan": plan_to_dict(plan),
            }
        )
        for attempt in range(self.maximum_attempts):
            try:
                self.socket.send(encoded, flags=zmq.NOBLOCK)
                if self.socket.poll(self.timeout_ms, zmq.POLLIN):
                    reply = _object(json.loads(self.socket.recv().decode("utf-8")))
                    if (
                        reply.get("schema") == ACTUATION_ACK_SCHEMA
                        and reply.get("decision_id") == plan.decision_id
                    ):
                        return ActuationPlanReceipt(
                            bool(reply.get("accepted")),
                            str(reply.get("detail", "")),
                            int(reply.get("received_monotonic_ns", 0)),
                        )
            except (ValueError, UnicodeDecodeError, zmq.ZMQError):
                pass
            if attempt + 1 < self.maximum_attempts:
                self._reset()
        self._reset()
        return ActuationPlanReceipt(False, "actuator did not acknowledge plan", 0)

    def close(self) -> None:
        self.socket.close(0)


class ZeroMQActuationPlanReceiver:
    """Actuator-side REP endpoint which acknowledges bounded-queue admission."""

    def __init__(
        self,
        endpoint: str = DEFAULT_ACTUATION_ENDPOINT,
        *,
        context: zmq.Context | None = None,
    ) -> None:
        self.endpoint = endpoint
        self.context = context or zmq.Context.instance()
        self.socket = self.context.socket(zmq.REP)
        self.socket.setsockopt(zmq.LINGER, 0)
        self.socket.setsockopt(zmq.MAXMSGSIZE, MAX_ACTUATION_PLAN_BYTES)
        self.socket.bind(endpoint)
        self.endpoint = self.socket.getsockopt_string(zmq.LAST_ENDPOINT)

    def receive(
        self,
        *,
        timeout_ms: int,
        accept,
    ) -> ActuationPlan | None:
        if not self.socket.poll(max(0, int(timeout_ms)), zmq.POLLIN):
            return None
        decision_id = ""
        received_ns = time.monotonic_ns()
        try:
            encoded = self.socket.recv()
            if len(encoded) > MAX_ACTUATION_PLAN_BYTES:
                raise ActuationTransportError("actuation plan exceeds size limit")
            message = _object(json.loads(encoded.decode("utf-8")))
            if message.get("schema") != ACTUATION_PLAN_SCHEMA:
                raise ActuationTransportError("invalid actuation plan schema")
            plan = plan_from_dict(_object(message.get("plan")))
            decision_id = plan.decision_id
            accepted, detail = accept(plan)
        except Exception as exc:  # noqa: BLE001 - negative acknowledgement
            accepted, detail, plan = False, str(exc), None
        self.socket.send(
            _encode(
                {
                    "schema": ACTUATION_ACK_SCHEMA,
                    "decision_id": decision_id,
                    "accepted": bool(accepted),
                    "detail": str(detail),
                    "received_monotonic_ns": received_ns,
                }
            )
        )
        return plan if accepted else None

    def close(self) -> None:
        self.socket.close(0)


def plan_to_dict(plan: ActuationPlan) -> dict[str, object]:
    return {
        "decision_id": plan.decision_id,
        "bean_ref": bean_ref_to_dict(plan.bean_ref),
        "gate_indices": list(plan.gate_indices),
        "open_monotonic_ns": plan.open_monotonic_ns,
        "close_monotonic_ns": plan.close_monotonic_ns,
        "crossing_monotonic_ns": plan.crossing_monotonic_ns,
        "open_source_ns": plan.open_source_ns,
        "close_source_ns": plan.close_source_ns,
        "crossing_source_ns": plan.crossing_source_ns,
        "run_clock_source_ns": plan.run_clock_source_ns,
        "run_clock_monotonic_ns": plan.run_clock_monotonic_ns,
        "run_clock_scale_ppb": plan.run_clock_scale_ppb,
    }


def plan_from_dict(value: dict[str, object]) -> ActuationPlan:
    raw_gates = value.get("gate_indices")
    if not isinstance(raw_gates, list):
        raise ActuationTransportError("actuation gate indices must be an array")
    plan = ActuationPlan(
        decision_id=str(value.get("decision_id", "")),
        bean_ref=bean_ref_from_dict(_object(value.get("bean_ref"))),
        gate_indices=tuple(int(item) for item in raw_gates),
        open_monotonic_ns=int(value.get("open_monotonic_ns", 0)),
        close_monotonic_ns=int(value.get("close_monotonic_ns", 0)),
        crossing_monotonic_ns=int(value.get("crossing_monotonic_ns", 0)),
        open_source_ns=int(value.get("open_source_ns", -1)),
        close_source_ns=int(value.get("close_source_ns", -1)),
        crossing_source_ns=int(value.get("crossing_source_ns", -1)),
        run_clock_source_ns=int(value.get("run_clock_source_ns", -1)),
        run_clock_monotonic_ns=int(value.get("run_clock_monotonic_ns", 0)),
        run_clock_scale_ppb=int(value.get("run_clock_scale_ppb", 0)),
    )
    plan.validate()
    return plan


def _encode(value: object) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _object(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ActuationTransportError("actuation message value must be an object")
    return value
