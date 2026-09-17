"""Bounded polling model for the read-only BeanRegistry monitor GUI."""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass

from .models import BeanEvent, BeanRef
from .registry_models import BeanRecord, RunSession
from .registry_service import DEFAULT_COMMAND_ENDPOINT
from .registry_zmq import ZeroMQRegistryClient


@dataclass(frozen=True, slots=True)
class RegistryMonitorSnapshot:
    connected: bool
    sessions: tuple[RunSession, ...] = ()
    records: tuple[BeanRecord, ...] = ()
    significant_events: tuple[BeanEvent, ...] = ()
    cursor: int = 0
    error: str = ""


class RegistryMonitorWorker:
    def __init__(
        self,
        callback: Callable[[RegistryMonitorSnapshot], None],
        *,
        registry_endpoint: str = DEFAULT_COMMAND_ENDPOINT,
        refresh_seconds: float = 0.5,
    ) -> None:
        self.callback = callback
        self.registry_endpoint = registry_endpoint
        self.refresh_seconds = max(0.05, float(refresh_seconds))
        self._stop = threading.Event()
        self._enabled = threading.Event()
        self._enabled.set()
        self._reset = threading.Event()
        self._reset.set()
        self._thread: threading.Thread | None = None
        self._cursor: int | None = None
        self._run_id = ""
        self._records: dict[BeanRef, BeanRecord] = {}

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run, name="beano-registry-monitor", daemon=True
        )
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(2.0)
        self._thread = None

    def set_enabled(self, enabled: bool) -> None:
        if enabled:
            self._reset.set()
            self._enabled.set()
        else:
            self._enabled.clear()

    def _run(self) -> None:
        client = ZeroMQRegistryClient(self.registry_endpoint, timeout_ms=1_000)
        try:
            while not self._stop.is_set():
                if not self._enabled.is_set():
                    self._stop.wait(0.1)
                    continue
                try:
                    client.ping()
                    if self._reset.is_set():
                        self._cursor = client.event_cursor()
                        self._run_id = ""
                        self._records.clear()
                        self._reset.clear()
                    sessions = client.list_sessions()
                    current = _latest_session(sessions)
                    current_run_id = "" if current is None else current.run_id
                    if current_run_id != self._run_id:
                        self._run_id = current_run_id
                        self._records = (
                            {}
                            if not current_run_id
                            else {
                                record.bean_ref: record
                                for record in client.list_records(run_id=current_run_id)
                            }
                        )
                    cursor = 0 if self._cursor is None else self._cursor
                    events = client.events_since_compact(cursor, limit=1_000)
                    if events:
                        self._cursor = events[-1].stream_sequence
                    changed_refs = dict.fromkeys(
                        event.bean_ref
                        for event in events
                        if event.bean_ref.run_id == current_run_id
                    )
                    for bean_ref in changed_refs:
                        self._records[bean_ref] = client.get(
                            bean_ref, include_history=False
                        )
                    significant = tuple(
                        event
                        for event in events
                        if event.bean_ref.run_id == current_run_id
                        and event.kind != "track.updated"
                    )
                    self.callback(
                        RegistryMonitorSnapshot(
                            True,
                            sessions,
                            tuple(
                                sorted(
                                    self._records.values(),
                                    key=lambda record: record.bean_ref,
                                )
                            ),
                            significant,
                            0 if self._cursor is None else self._cursor,
                        )
                    )
                except Exception as exc:  # noqa: BLE001 - monitor reconnect loop
                    client.close()
                    self.callback(
                        RegistryMonitorSnapshot(
                            False,
                            cursor=0 if self._cursor is None else self._cursor,
                            error=str(exc),
                        )
                    )
                self._stop.wait(self.refresh_seconds)
        finally:
            client.close()


def _latest_session(sessions: tuple[RunSession, ...]) -> RunSession | None:
    if not sessions:
        return None
    return max(
        sessions,
        key=lambda session: (
            session.updated_timestamp_ns,
            session.created_timestamp_ns,
            session.run_id,
        ),
    )
