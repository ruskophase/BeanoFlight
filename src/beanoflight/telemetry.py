"""Low-overhead timing and host telemetry for repeatable replay profiling."""

from __future__ import annotations

import math
import os
import resource
import threading
import time
from collections import deque
from collections.abc import Iterable
from pathlib import Path


def summarize_samples(values: Iterable[float]) -> dict[str, float | int]:
    samples = sorted(float(value) for value in values if math.isfinite(float(value)))
    if not samples:
        return {"count": 0, "mean": 0.0, "p50": 0.0, "p95": 0.0, "p99": 0.0, "max": 0.0}

    def percentile(fraction: float) -> float:
        return samples[min(len(samples) - 1, round((len(samples) - 1) * fraction))]

    return {
        "count": len(samples),
        "mean": sum(samples) / len(samples),
        "p50": percentile(0.50),
        "p95": percentile(0.95),
        "p99": percentile(0.99),
        "max": samples[-1],
    }


class TimingAccumulator:
    """Thread-safe bounded timing history with percentile summaries."""

    def __init__(self, capacity: int = 4_096) -> None:
        self._values: deque[float] = deque(maxlen=max(1, int(capacity)))
        self._lock = threading.Lock()

    def add(self, milliseconds: float) -> None:
        if math.isfinite(milliseconds) and milliseconds >= 0:
            with self._lock:
                self._values.append(float(milliseconds))

    def summary(self) -> dict[str, float | int]:
        with self._lock:
            values = tuple(self._values)
        return summarize_samples(values)

    def clear(self) -> None:
        with self._lock:
            self._values.clear()


class SystemTelemetrySampler:
    """Sample portable load plus available Linux thermal and CPU-frequency data."""

    def __init__(self, interval_seconds: float = 0.5) -> None:
        self.interval_seconds = max(0.1, float(interval_seconds))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._samples: list[dict[str, object]] = []
        self._start_usage = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._start_usage = resource.getrusage(resource.RUSAGE_SELF)
        self._samples.append(_system_sample())
        self._thread = threading.Thread(
            target=self._run, name="beanoflight-system-telemetry", daemon=True
        )
        self._thread.start()

    def stop(self) -> dict[str, object]:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(1.0)
        self._thread = None
        self._samples.append(_system_sample())
        end_usage = resource.getrusage(resource.RUSAGE_SELF)
        start_usage = self._start_usage or end_usage
        temperatures: dict[str, list[float]] = {}
        frequencies: list[float] = []
        loads: list[float] = []
        for sample in self._samples:
            loads.append(float(sample["load_1m"]))
            frequencies.extend(float(value) for value in sample["cpu_frequency_mhz"])
            for name, value in sample["temperatures_c"].items():
                temperatures.setdefault(str(name), []).append(float(value))
        return {
            "samples": len(self._samples),
            "process_cpu_seconds": (
                end_usage.ru_utime
                + end_usage.ru_stime
                - start_usage.ru_utime
                - start_usage.ru_stime
            ),
            "max_rss_mib": end_usage.ru_maxrss / 1_024.0,
            "load_1m": summarize_samples(loads),
            "cpu_frequency_mhz": summarize_samples(frequencies),
            "temperature_c": {
                name: summarize_samples(values)
                for name, values in sorted(temperatures.items())
            },
        }

    def _run(self) -> None:
        while not self._stop.wait(self.interval_seconds):
            self._samples.append(_system_sample())


def _system_sample() -> dict[str, object]:
    try:
        load_1m = os.getloadavg()[0]
    except OSError:
        load_1m = 0.0
    return {
        "monotonic_ns": time.monotonic_ns(),
        "load_1m": load_1m,
        "temperatures_c": _temperatures_c(),
        "cpu_frequency_mhz": _cpu_frequencies_mhz(),
    }


def _temperatures_c() -> dict[str, float]:
    result: dict[str, float] = {}
    for zone in Path("/sys/class/thermal").glob("thermal_zone*"):
        try:
            name = (zone / "type").read_text(encoding="utf-8").strip() or zone.name
            value = float((zone / "temp").read_text(encoding="utf-8").strip())
            if abs(value) > 1_000:
                value /= 1_000.0
            if math.isfinite(value):
                result[name] = value
        except (OSError, TypeError, ValueError):
            continue
    return result


def _cpu_frequencies_mhz() -> tuple[float, ...]:
    result: list[float] = []
    for path in Path("/sys/devices/system/cpu").glob(
        "cpu[0-9]*/cpufreq/scaling_cur_freq"
    ):
        try:
            value = float(path.read_text(encoding="utf-8").strip()) / 1_000.0
            if math.isfinite(value) and value > 0:
                result.append(value)
        except (OSError, TypeError, ValueError):
            continue
    return tuple(result)
