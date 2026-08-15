"""Thread-safe enrichment store and bounded event fan-out contracts."""

from __future__ import annotations

import queue
import threading
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from .models import BeanEvent, BeanRef


class EventBus:
    """Non-blocking bounded fan-out; slow consumers lose oldest events."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subscribers: list[queue.Queue[BeanEvent]] = []

    def subscribe(self, *, capacity: int = 128) -> queue.Queue[BeanEvent]:
        destination: queue.Queue[BeanEvent] = queue.Queue(maxsize=max(1, capacity))
        with self._lock:
            self._subscribers.append(destination)
        return destination

    def publish(self, event: BeanEvent) -> None:
        with self._lock:
            subscribers = tuple(self._subscribers)
        for destination in subscribers:
            try:
                destination.put_nowait(event)
            except queue.Full:
                try:
                    destination.get_nowait()
                except queue.Empty:
                    pass
                try:
                    destination.put_nowait(event)
                except queue.Full:
                    pass


@dataclass(frozen=True, slots=True)
class Enrichment:
    source: str
    kind: str
    value: Any
    timestamp_ns: int
    version: str = ""


class BeanStore:
    """Allows asynchronous workers to attach results without mutating tracks."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._values: dict[BeanRef, dict[str, list[Enrichment]]] = defaultdict(
            lambda: defaultdict(list)
        )

    def add(self, bean_ref: BeanRef, enrichment: Enrichment) -> None:
        with self._lock:
            self._values[bean_ref][enrichment.kind].append(enrichment)

    def snapshot(self, bean_ref: BeanRef) -> dict[str, tuple[Enrichment, ...]]:
        with self._lock:
            return {
                kind: tuple(values)
                for kind, values in self._values.get(bean_ref, {}).items()
            }
