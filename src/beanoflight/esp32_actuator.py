"""ESP32-S2 gate scheduler bridge and safety/audit service."""

from __future__ import annotations

import fcntl
import os
import queue
import select
import struct
import termios
import threading
import time
import tty
import zlib
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path

import zmq

from .actuation_transport import (
    DEFAULT_ACTUATION_ENDPOINT,
    ActuationPlan,
    ZeroMQActuationPlanReceiver,
)
from .registry_models import ActuationResult
from .registry_service import DEFAULT_COMMAND_ENDPOINT
from .registry_zmq import ZeroMQRegistryClient
from .runtime_priority import lower_current_thread_priority

FIRMWARE_PROTOCOL = "beano-actuator-v1"
DEFAULT_ESP32_PORT = (
    "/dev/serial/by-path/platform-3610000.usb-usb-0:2.1:1.0"
)
GATE_INDICES = tuple(range(-10, 11))
GATE_GPIOS = (
    1,
    2,
    3,
    4,
    5,
    6,
    7,
    8,
    9,
    10,
    11,
    12,
    13,
    14,
    16,
    17,
    18,
    21,
    33,
    34,
    35,
)
GATE_GPIO_MAP = dict(zip(GATE_INDICES, GATE_GPIOS))


class ESP32ProtocolError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ActuatorActivity:
    kind: str
    detail: str = ""
    decision_id: str = ""
    gate_indices: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True)
class _PendingHardwarePlan:
    plan: ActuationPlan
    sequence: int
    clock_offset_ns: int
    admitted_monotonic_ns: int
    sent_monotonic_ns: int
    open_board_us: int
    close_board_us: int
    opened_board_us: int | None = None
    acknowledged_board_us: int | None = None
    acknowledged: bool = False


@dataclass(frozen=True, slots=True, order=True)
class _QueuedHardwarePlan:
    open_monotonic_ns: int
    arrival_order: int
    plan: ActuationPlan = field(compare=False)
    admitted_monotonic_ns: int = field(compare=False)


@dataclass(frozen=True, slots=True)
class _ResultAudit:
    plan: ActuationPlan
    result: ActuationResult
    attempts: int = 0


class _SerialCDC:
    def __init__(self, path: str) -> None:
        self.path = path
        self.fd: int | None = None
        self.buffer = bytearray()

    def open(self) -> None:
        fd = os.open(self.path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        tty.setraw(fd)
        attributes = termios.tcgetattr(fd)
        attributes[2] |= termios.CLOCAL | termios.CREAD
        attributes[2] &= ~termios.HUPCL
        attributes[4] = termios.B115200
        attributes[5] = termios.B115200
        termios.tcsetattr(fd, termios.TCSANOW, attributes)
        # Native USB CDC uses DTR as its host-ready signal. Explicitly assert it
        # so restarting BeanoActuator does not require a physical board reset.
        fcntl.ioctl(
            fd,
            termios.TIOCMBIS,
            struct.pack("I", termios.TIOCM_DTR),
        )
        self.fd = fd
        self.buffer.clear()

    def close(self) -> None:
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
        self.fd = None
        self.buffer.clear()

    def write(self, message: str) -> None:
        if self.fd is None:
            raise OSError("ESP32 serial port is not open")
        encoded = message.encode("ascii")
        view = memoryview(encoded)
        while view:
            try:
                written = os.write(self.fd, view)
            except BlockingIOError:
                select.select([], [self.fd], [], 0.01)
                continue
            view = view[written:]

    def read_lines(self, timeout: float) -> tuple[str, ...]:
        if self.fd is None:
            return ()
        readable, _, _ = select.select([self.fd], [], [], max(0.0, timeout))
        if readable:
            chunk = os.read(self.fd, 4096)
            if not chunk:
                raise OSError("ESP32 serial device disconnected")
            self.buffer.extend(chunk)
        lines = []
        while b"\n" in self.buffer:
            raw, _, remainder = self.buffer.partition(b"\n")
            self.buffer[:] = remainder
            raw = raw.strip(b"\r")
            if raw:
                lines.append(raw.decode("ascii"))
        if len(self.buffer) > 4096:
            self.buffer.clear()
            raise ESP32ProtocolError("ESP32 line exceeded 4096 bytes")
        return tuple(lines)


class ESP32ActuatorService:
    """Receive approved plans, schedule them on ESP32 and persist observed cycles."""

    def __init__(
        self,
        *,
        registry_endpoint: str = DEFAULT_COMMAND_ENDPOINT,
        actuation_endpoint: str = DEFAULT_ACTUATION_ENDPOINT,
        serial_port: str = DEFAULT_ESP32_PORT,
        minimum_board_notice_ms: float = 0.3,
        activity: Callable[[ActuatorActivity], None] | None = None,
    ) -> None:
        self.registry_endpoint = registry_endpoint
        self.actuation_endpoint = actuation_endpoint
        self.serial_port = serial_port
        self.minimum_board_notice_ns = round(minimum_board_notice_ms * 1_000_000)
        self.activity = activity
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._plans: queue.PriorityQueue[_QueuedHardwarePlan] = (
            queue.PriorityQueue(maxsize=256)
        )
        self._tests: queue.Queue[int] = queue.Queue(maxsize=4)
        self._results: queue.Queue[_ResultAudit] = queue.Queue(maxsize=256)
        self._accepted_decisions: set[str] = set()
        self._accepted_lock = threading.Lock()
        self._pending: dict[int, _PendingHardwarePlan] = {}
        self._request_plans: dict[int, int] = {}
        self._clock_samples: deque[tuple[int, int]] = deque(maxlen=32)
        self._clock_offset_ns: int | None = None
        self._clock_rtt_ns: int | None = None
        self._gate_mask = 0
        self._state_lock = threading.Lock()
        self._next_request = 1
        self._next_plan = 1
        self._plan_arrival_order = 0
        self.connected = False
        self.synchronized = False
        self.ready = threading.Event()
        self.startup_error = ""
        self.plans_received = 0
        self.plans_scheduled = 0
        self.plans_rejected = 0
        self.cycles_completed = 0
        self.cycles_failed = 0
        self.protocol_errors = 0

    @property
    def gate_states(self) -> dict[int, bool]:
        with self._state_lock:
            mask = self._gate_mask
        return {gate: bool(mask & (1 << (gate + 10))) for gate in GATE_INDICES}

    @property
    def clock_offset_ns(self) -> int | None:
        return self._clock_offset_ns

    @property
    def clock_rtt_ms(self) -> float | None:
        return None if self._clock_rtt_ns is None else self._clock_rtt_ns / 1_000_000

    def start(self) -> None:
        if self._threads:
            return
        for name, target in (
            ("beano-actuator-plan-and-esp32-io", self._io_loop),
            ("beano-actuator-registry-audit", self._audit_loop),
        ):
            thread = threading.Thread(target=target, name=name, daemon=True)
            self._threads.append(thread)
            thread.start()

    def request_led_test(self, *, interval_ms: int = 80) -> bool:
        if not self.connected or not self.synchronized:
            return False
        try:
            self._tests.put_nowait(max(20, min(500, int(interval_ms))))
        except queue.Full:
            return False
        return True

    def close(self, *, drain: bool = True) -> None:
        if drain:
            deadline = time.monotonic() + 2.0
            while (
                (not self._plans.empty() or not self._results.empty())
                and time.monotonic() < deadline
            ):
                self._stop.wait(0.01)
        self._stop.set()
        for thread in self._threads:
            thread.join(2.0)
        self._threads.clear()

    def _accept_plan(self, plan: ActuationPlan) -> tuple[bool, str]:
        self.plans_received += 1
        with self._accepted_lock:
            if plan.decision_id in self._accepted_decisions:
                return True, "duplicate plan already admitted"
            if not self.connected or not self.synchronized:
                self.plans_rejected += 1
                return False, "ESP32 is not connected and clock-synchronized"
            if plan.open_monotonic_ns - time.monotonic_ns() < self.minimum_board_notice_ns:
                self.plans_rejected += 1
                return False, "plan reached actuator below its minimum notice"
            try:
                admitted_ns = time.monotonic_ns()
                self._plan_arrival_order += 1
                self._plans.put_nowait(
                    _QueuedHardwarePlan(
                        plan.open_monotonic_ns,
                        self._plan_arrival_order,
                        plan,
                        admitted_ns,
                    )
                )
            except queue.Full:
                self.plans_rejected += 1
                return False, "ESP32 actuator queue is full"
            self._accepted_decisions.add(plan.decision_id)
        return True, "queued for ESP32 hardware timer"

    def _io_loop(self) -> None:
        try:
            receiver = ZeroMQActuationPlanReceiver(self.actuation_endpoint)
        except Exception as exc:  # noqa: BLE001 - surfaced to GUI
            self.startup_error = str(exc)
            self._emit("error", detail=self.startup_error)
            self.ready.set()
            return
        self.actuation_endpoint = receiver.endpoint
        self.ready.set()
        serial = _SerialCDC(self.serial_port)
        poller = zmq.Poller()
        poller.register(receiver.socket, zmq.POLLIN)
        last_ping_ns = 0
        next_serial_open_ns = 0
        try:
            while not self._stop.is_set():
                now_ns = time.monotonic_ns()
                if serial.fd is None and now_ns >= next_serial_open_ns:
                    try:
                        serial.open()
                        poller.register(serial.fd, zmq.POLLIN)
                        self.connected = True
                        self.synchronized = False
                        self._clock_samples.clear()
                        self._emit("connected", detail=self.serial_port)
                    except OSError as exc:
                        self.connected = False
                        self.synchronized = False
                        if not Path(self.serial_port).exists():
                            detail = f"waiting for {self.serial_port}"
                        else:
                            detail = str(exc)
                        self._emit("waiting", detail=detail)
                        next_serial_open_ns = now_ns + 250_000_000
                readable = dict(poller.poll(10))
                # The same thread owns plan admission and USB. This removes an
                # operating-system scheduling hop between an IPC ACK and the
                # actual SCHEDULE write while retaining a bounded priority queue.
                if receiver.socket in readable:
                    for _index in range(32):
                        if not receiver.socket.poll(0, zmq.POLLIN):
                            break
                        receiver.receive(timeout_ms=0, accept=self._accept_plan)
                if serial.fd is None:
                    continue
                try:
                    # A ZeroMQ arrival wakes the kernel poll and the admitted
                    # plan enters USB before routine serial input is handled.
                    plans_sent = self._send_queued_plans(serial)
                    for line in serial.read_lines(0.0):
                        try:
                            self._handle_line(line, time.monotonic_ns())
                        except ESP32ProtocolError as exc:
                            # ESP-IDF boot ROM and second-stage bootloader write
                            # ordinary console lines before the CRC protocol starts.
                            if _looks_like_protocol_message(line):
                                self.protocol_errors += 1
                            self._emit("ignored", detail=str(exc))
                    self._send_test_requests(serial)
                    now_ns = time.monotonic_ns()
                    # A SCHEDULE command also feeds the board watchdog. Avoid
                    # putting routine clock traffic immediately ahead of urgent
                    # plans in the CDC receive queue. During sustained plan
                    # traffic, still refresh clock sync at least once per second.
                    ping_due_ns = 1_000_000_000 if plans_sent else 100_000_000
                    if now_ns - last_ping_ns >= ping_due_ns:
                        request = self._allocate_request()
                        serial.write(
                            encode_protocol_line(
                                "PING",
                                request,
                                now_ns // 1_000,
                            )
                        )
                        last_ping_ns = now_ns
                    self._expire_unacknowledged()
                except (OSError, ESP32ProtocolError) as exc:
                    self.protocol_errors += 1
                    self._emit("error", detail=str(exc))
                    self._fail_pending(f"ESP32 link lost: {exc}")
                    if serial.fd is not None:
                        try:
                            poller.unregister(serial.fd)
                        except KeyError:
                            pass
                    serial.close()
                    self.connected = False
                    self.synchronized = False
                    next_serial_open_ns = time.monotonic_ns() + 100_000_000
        finally:
            if serial.fd is not None:
                try:
                    request = self._allocate_request()
                    serial.write(encode_protocol_line("ALL_OFF", request))
                    # Do not close CDC while firmware is printing the reply. The
                    # ROM console write can otherwise remain blocked after a
                    # host restart, leaving the board unable to read new PINGs.
                    deadline = time.monotonic() + 0.15
                    acknowledged = False
                    while time.monotonic() < deadline and not acknowledged:
                        for line in serial.read_lines(0.01):
                            try:
                                fields = decode_protocol_line(line)
                            except ESP32ProtocolError:
                                continue
                            acknowledged = (
                                fields[0] == "ACK"
                                and len(fields) >= 2
                                and fields[1] == str(request)
                            )
                            if acknowledged:
                                break
                except (OSError, ESP32ProtocolError):
                    pass
            serial.close()
            self.connected = False
            self.synchronized = False
            receiver.close()

    def _send_test_requests(self, serial: _SerialCDC) -> None:
        for _index in range(4):
            try:
                interval_ms = self._tests.get_nowait()
            except queue.Empty:
                return
            try:
                serial.write(
                    encode_protocol_line(
                        "TEST",
                        self._allocate_request(),
                        interval_ms,
                    )
                )
                self._emit("test", detail=f"LED chase {interval_ms} ms per gate")
            finally:
                self._tests.task_done()

    def _send_queued_plans(self, serial: _SerialCDC) -> int:
        if not self.synchronized or self._clock_offset_ns is None:
            return 0
        sent = 0
        for _index in range(32):
            try:
                queued = self._plans.get_nowait()
            except queue.Empty:
                return sent
            try:
                plan = queued.plan
                sequence = self._allocate_plan()
                request = self._allocate_request()
                offset = self._clock_offset_ns
                open_us = (plan.open_monotonic_ns - offset) // 1_000
                close_us = (plan.close_monotonic_ns - offset) // 1_000
                mask = gate_indices_to_mask(plan.gate_indices)
                pending = _PendingHardwarePlan(
                    plan,
                    sequence,
                    offset,
                    queued.admitted_monotonic_ns,
                    time.monotonic_ns(),
                    open_us,
                    close_us,
                )
                self._pending[sequence] = pending
                self._request_plans[request] = sequence
                serial.write(
                    encode_protocol_line(
                        "SCHEDULE",
                        request,
                        sequence,
                        f"{mask:08X}",
                        open_us,
                        close_us,
                    )
                )
                sent += 1
                self.plans_scheduled += 1
                self._emit(
                    "scheduled",
                    plan,
                    detail=(
                        f"board plan {sequence} · gates {plan.gate_indices} · "
                        f"notice {(plan.open_monotonic_ns - time.monotonic_ns()) / 1_000_000:.2f} ms"
                    ),
                )
            finally:
                self._plans.task_done()
        return sent

    def _handle_line(self, line: str, received_ns: int) -> None:
        fields = decode_protocol_line(line)
        command = fields[0]
        if command == "READY":
            if len(fields) < 4 or fields[1] != FIRMWARE_PROTOCOL:
                raise ESP32ProtocolError("unexpected ESP32 firmware identity")
            self._emit("ready", detail=f"firmware {fields[1]} boot {fields[2]}")
            return
        if command == "PONG":
            if len(fields) not in {4, 6}:
                raise ESP32ProtocolError("invalid PONG")
            if len(fields) == 6 and fields[4] != FIRMWARE_PROTOCOL:
                raise ESP32ProtocolError("unexpected ESP32 firmware identity")
            host_send_ns = int(fields[2]) * 1_000
            board_ns = int(fields[3]) * 1_000
            rtt_ns = max(0, received_ns - host_send_ns)
            offset_ns = ((host_send_ns + received_ns) // 2) - board_ns
            self._clock_samples.append((rtt_ns, offset_ns))
            best = min(self._clock_samples, key=lambda item: item[0])
            self._clock_rtt_ns, self._clock_offset_ns = best
            self.synchronized = len(self._clock_samples) >= 3
            if len(fields) == 6 and len(self._clock_samples) == 1:
                self._emit(
                    "ready",
                    detail=f"firmware {fields[4]} boot {fields[5]}",
                )
            return
        if command == "ACK":
            if len(fields) < 5:
                raise ESP32ProtocolError("invalid ACK")
            request = int(fields[1])
            plan_sequence = self._request_plans.pop(request, None)
            if plan_sequence is not None and plan_sequence in self._pending:
                self._pending[plan_sequence] = replace(
                    self._pending[plan_sequence],
                    acknowledged=True,
                    acknowledged_board_us=int(fields[4]),
                )
            return
        if command == "ERR":
            if len(fields) < 4:
                raise ESP32ProtocolError("invalid ERR")
            request = int(fields[1])
            plan_sequence = self._request_plans.pop(request, None)
            if plan_sequence is not None:
                pending = self._pending.get(plan_sequence)
                board_us = int(fields[3])
                notice = (
                    ""
                    if pending is None
                    else f"; board admission notice {(pending.open_board_us - board_us) / 1_000:.3f} ms"
                )
                self._fail_plan(
                    plan_sequence,
                    f"ESP32 rejected plan: {fields[2]}{notice}",
                )
            return
        if command in {"OPEN", "CLOSE"}:
            if len(fields) != 4:
                raise ESP32ProtocolError(f"invalid {command}")
            sequence = int(fields[1])
            mask = int(fields[2], 16)
            board_us = int(fields[3])
            with self._state_lock:
                self._gate_mask = (
                    self._gate_mask | mask
                    if command == "OPEN"
                    else self._gate_mask & ~mask
                )
            pending = self._pending.get(sequence)
            if pending is None:
                return
            if command == "OPEN":
                self._pending[sequence] = replace(
                    pending, opened_board_us=board_us
                )
                self._emit("opened", pending.plan)
            else:
                self._complete_plan(sequence, board_us)
            return
        if command == "WATCHDOG":
            with self._state_lock:
                self._gate_mask = 0
            self._fail_pending("ESP32 watchdog forced every gate off")
            self._emit("watchdog", detail="all outputs forced off")
            return
        if command == "STATUS":
            return
        raise ESP32ProtocolError(f"unknown ESP32 message {command!r}")

    def _complete_plan(self, sequence: int, closed_board_us: int) -> None:
        pending = self._pending.pop(sequence, None)
        if pending is None:
            return
        opened_board_us = pending.opened_board_us
        if opened_board_us is None:
            self._queue_failed_result(pending.plan, "ESP32 reported close without open")
            return
        actual_open_host_ns = opened_board_us * 1_000 + pending.clock_offset_ns
        actual_close_host_ns = closed_board_us * 1_000 + pending.clock_offset_ns
        actual_open_source_ns = host_to_source_ns(pending.plan, actual_open_host_ns)
        actual_close_source_ns = host_to_source_ns(pending.plan, actual_close_host_ns)
        success = (
            actual_open_host_ns
            <= pending.plan.crossing_monotonic_ns
            <= actual_close_host_ns
        )
        board_notice_ms = (
            0.0
            if pending.acknowledged_board_us is None
            else (pending.open_board_us - pending.acknowledged_board_us) / 1_000
        )
        result = ActuationResult(
            decision_id=pending.plan.decision_id,
            source="esp32-s2-gptimer",
            actual_open_timestamp_ns=actual_open_source_ns,
            actual_close_timestamp_ns=actual_close_source_ns,
            success=success,
            detail=(
                "ESP32 hardware-timed gate covered predicted crossing"
                if success
                else "ESP32 hardware gate window missed predicted crossing"
            )
            + (
                f"; host clock-sync RTT {(self.clock_rtt_ms or 0.0):.3f} ms; "
                f"actuator admission notice {(pending.plan.open_monotonic_ns - pending.admitted_monotonic_ns) / 1_000_000:.3f} ms; "
                f"USB queue {(pending.sent_monotonic_ns - pending.admitted_monotonic_ns) / 1_000_000:.3f} ms; "
                f"board admission notice {board_notice_ms:.3f} ms; "
                f"open error {(actual_open_host_ns - pending.plan.open_monotonic_ns) / 1_000_000:.3f} ms; "
                f"close error {(actual_close_host_ns - pending.plan.close_monotonic_ns) / 1_000_000:.3f} ms"
            ),
        )
        self._results.put(_ResultAudit(pending.plan, result))
        self.cycles_completed += int(success)
        self.cycles_failed += int(not success)
        self._emit("closed", pending.plan, detail=result.detail)

    def _expire_unacknowledged(self) -> None:
        now_ns = time.monotonic_ns()
        expired = tuple(
            sequence
            for sequence, pending in self._pending.items()
            if not pending.acknowledged
            and now_ns >= pending.plan.close_monotonic_ns + 5_000_000
        )
        for sequence in expired:
            self._fail_plan(
                sequence,
                "ESP32 did not acknowledge or report a cycle by the close deadline",
            )

    def _fail_plan(self, sequence: int, detail: str) -> None:
        for request, request_sequence in tuple(self._request_plans.items()):
            if request_sequence == sequence:
                self._request_plans.pop(request, None)
        pending = self._pending.pop(sequence, None)
        if pending is not None:
            self._queue_failed_result(pending.plan, detail)

    def _fail_pending(self, detail: str) -> None:
        for sequence in tuple(self._pending):
            self._fail_plan(sequence, detail)

    def _queue_failed_result(self, plan: ActuationPlan, detail: str) -> None:
        now_source = host_to_source_ns(plan, time.monotonic_ns())
        result = ActuationResult(
            decision_id=plan.decision_id,
            source="esp32-s2-gptimer",
            actual_open_timestamp_ns=now_source,
            actual_close_timestamp_ns=now_source,
            success=False,
            detail=detail,
        )
        self._results.put(_ResultAudit(plan, result))
        self.cycles_failed += 1
        self._emit("failed", plan, detail=detail)

    def _audit_loop(self) -> None:
        lower_current_thread_priority()
        registry = ZeroMQRegistryClient(self.registry_endpoint, timeout_ms=2_000)
        try:
            while not self._stop.is_set() or not self._results.empty():
                try:
                    audit = self._results.get(timeout=0.05)
                except queue.Empty:
                    continue
                try:
                    registry.record_actuation_ack(
                        audit.plan.bean_ref,
                        audit.result,
                        event_id=f"actuation:{audit.result.decision_id}",
                    )
                except Exception as exc:  # noqa: BLE001 - decision audit may trail plan
                    registry.close()
                    if audit.attempts < 20 and not self._stop.is_set():
                        self._stop.wait(min(0.05, 0.005 * (audit.attempts + 1)))
                        self._results.put(replace(audit, attempts=audit.attempts + 1))
                    else:
                        self._emit(
                            "error",
                            audit.plan,
                            detail=f"actuation audit failed: {exc}",
                        )
                    registry = ZeroMQRegistryClient(
                        self.registry_endpoint, timeout_ms=2_000
                    )
                finally:
                    self._results.task_done()
        finally:
            registry.close()

    def _allocate_request(self) -> int:
        value = self._next_request
        self._next_request = 1 if value >= 0x7FFFFFFF else value + 1
        return value

    def _allocate_plan(self) -> int:
        value = self._next_plan
        self._next_plan = 1 if value >= 0x7FFFFFFF else value + 1
        return value

    def _emit(
        self,
        kind: str,
        plan: ActuationPlan | None = None,
        *,
        detail: str = "",
    ) -> None:
        if self.activity is not None:
            self.activity(
                ActuatorActivity(
                    kind,
                    detail,
                    "" if plan is None else plan.decision_id,
                    () if plan is None else plan.gate_indices,
                )
            )


def gate_indices_to_mask(gates: tuple[int, ...]) -> int:
    mask = 0
    for gate in gates:
        if gate not in GATE_GPIO_MAP:
            raise ESP32ProtocolError(f"gate {gate} has no ESP32 GPIO")
        mask |= 1 << (gate + 10)
    return mask


def host_to_source_ns(plan: ActuationPlan, host_monotonic_ns: int) -> int:
    return plan.run_clock_source_ns + round(
        (host_monotonic_ns - plan.run_clock_monotonic_ns)
        * plan.run_clock_scale_ppb
        / 1_000_000_000
    )


def encode_protocol_line(command: str, *fields: object) -> str:
    if not command or any(character in command for character in ",\r\n"):
        raise ESP32ProtocolError("invalid protocol command")
    body = ",".join((command, *(str(field) for field in fields)))
    checksum = zlib.crc32(body.encode("ascii")) & 0xFFFFFFFF
    return f"{body},{checksum:08X}\n"


def decode_protocol_line(line: str) -> tuple[str, ...]:
    value = line.strip("\r\n")
    try:
        body, checksum_text = value.rsplit(",", 1)
        expected = int(checksum_text, 16)
    except (ValueError, TypeError) as exc:
        raise ESP32ProtocolError("malformed protocol checksum") from exc
    actual = zlib.crc32(body.encode("ascii")) & 0xFFFFFFFF
    if actual != expected:
        raise ESP32ProtocolError("protocol checksum mismatch")
    fields = tuple(body.split(","))
    if not fields or not fields[0]:
        raise ESP32ProtocolError("empty protocol command")
    return fields


def _looks_like_protocol_message(line: str) -> bool:
    command = line.partition(",")[0]
    return command in {
        "READY",
        "PONG",
        "ACK",
        "ERR",
        "OPEN",
        "CLOSE",
        "WATCHDOG",
        "STATUS",
    }
