"""Bounded polling model for the read-only BeanRegistry monitor GUI."""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass

from .models import BeanEvent
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
        refresh_seconds: float = 0.2,
    ) -> None:
        self.callback = callback
        self.registry_endpoint = registry_endpoint
        self.refresh_seconds = max(0.02, float(refresh_seconds))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._cursor = 0

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

    def _run(self) -> None:
        client = ZeroMQRegistryClient(self.registry_endpoint, timeout_ms=1_000)
        try:
            while not self._stop.is_set():
                try:
                    client.ping()
                    sessions = client.list_sessions()
                    records = client.list_records()
                    events = client.events_since(self._cursor, limit=1_000)
                    if events:
                        self._cursor = events[-1].stream_sequence
                    significant = tuple(
                        event for event in events if event.kind != "track.updated"
                    )
                    self.callback(
                        RegistryMonitorSnapshot(
                            True,
                            sessions,
                            records,
                            significant,
                            self._cursor,
                        )
                    )
                except Exception as exc:  # noqa: BLE001 - monitor reconnect loop
                    client.close()
                    self.callback(
                        RegistryMonitorSnapshot(
                            False, cursor=self._cursor, error=str(exc)
                        )
                    )
                self._stop.wait(self.refresh_seconds)
        finally:
            client.close()
