"""Low-overhead timing and host telemetry for repeatable replay profiling."""

from __future__ import annotations

import math
import os
import resource
import threading
import time
from collections import deque
from collections.abc import Iterable, Mapping
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


def current_rss_mib(pid: int = 0) -> float:
    """Read current resident memory without allocating a process snapshot."""

    target_pid = os.getpid() if pid <= 0 else int(pid)
    try:
        fields = Path(f"/proc/{target_pid}/statm").read_text(
            encoding="ascii"
        ).split()
        resident_pages = int(fields[1])
        return resident_pages * os.sysconf("SC_PAGE_SIZE") / (1024.0 * 1024.0)
    except (IndexError, OSError, TypeError, ValueError):
        return 0.0


class SystemTelemetrySampler:
    """Sample portable load plus available Linux thermal and CPU-frequency data."""

    def __init__(
        self,
        interval_seconds: float = 0.5,
        *,
        watched_pids: Mapping[str, int] | None = None,
        maximum_temperature_c: float | None = None,
    ) -> None:
        self.interval_seconds = max(0.1, float(interval_seconds))
        self.watched_pids = {
            str(name): int(pid)
            for name, pid in (watched_pids or {}).items()
            if int(pid) > 0
        }
        self.maximum_temperature_c = (
            None
            if maximum_temperature_c is None
            else float(maximum_temperature_c)
        )
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._samples: list[dict[str, object]] = []
        self._lock = threading.Lock()
        self._start_usage = None
        self.thermal_abort = threading.Event()
        self.thermal_abort_detail = ""
        self.latest_max_temperature_c = 0.0

    def start(self) -> None:
        if self._thread is not None:
            return
        self._start_usage = resource.getrusage(resource.RUSAGE_SELF)
        self._capture_sample()
        self._thread = threading.Thread(
            target=self._run, name="beanoflight-system-telemetry", daemon=True
        )
        self._thread.start()

    def stop(self) -> dict[str, object]:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(1.0)
        self._thread = None
        self._capture_sample()
        end_usage = resource.getrusage(resource.RUSAGE_SELF)
        start_usage = self._start_usage or end_usage
        temperatures: dict[str, list[float]] = {}
        watched_rss: dict[str, list[tuple[int, float]]] = {}
        frequencies: list[float] = []
        loads: list[float] = []
        with self._lock:
            samples = tuple(self._samples)
        for sample in samples:
            loads.append(float(sample["load_1m"]))
            frequencies.extend(float(value) for value in sample["cpu_frequency_mhz"])
            for name, value in sample["temperatures_c"].items():
                temperatures.setdefault(str(name), []).append(float(value))
            monotonic_ns = int(sample["monotonic_ns"])
            for name, value in sample.get("watched_rss_mib", {}).items():
                watched_rss.setdefault(str(name), []).append(
                    (monotonic_ns, float(value))
                )
        return {
            "samples": len(samples),
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
            "maximum_temperature_c": self.maximum_temperature_c,
            "thermal_abort": self.thermal_abort.is_set(),
            "thermal_abort_detail": self.thermal_abort_detail,
            "watched_rss_mib": {
                name: _rss_summary(values)
                for name, values in sorted(watched_rss.items())
            },
        }

    def _run(self) -> None:
        while not self._stop.wait(self.interval_seconds):
            self._capture_sample()

    def _capture_sample(self) -> None:
        sample = _system_sample()
        sample["watched_rss_mib"] = {
            name: value
            for name, pid in self.watched_pids.items()
            if (value := current_rss_mib(pid)) > 0
        }
        temperatures = sample["temperatures_c"]
        hottest_name = ""
        hottest_c = 0.0
        if temperatures:
            hottest_name, hottest_c = max(
                temperatures.items(), key=lambda item: float(item[1])
            )
            hottest_c = float(hottest_c)
        with self._lock:
            self._samples.append(sample)
            self.latest_max_temperature_c = hottest_c
            if (
                self.maximum_temperature_c is not None
                and hottest_c >= self.maximum_temperature_c
                and not self.thermal_abort.is_set()
            ):
                self.thermal_abort_detail = (
                    f"{hottest_name} reached {hottest_c:.1f} C "
                    f"(limit {self.maximum_temperature_c:.1f} C)"
                )
                self.thermal_abort.set()


def _rss_summary(values: list[tuple[int, float]]) -> dict[str, float | int]:
    if not values:
        return {
            "count": 0,
            "first": 0.0,
            "last": 0.0,
            "growth": 0.0,
            "growth_per_hour": 0.0,
            "max": 0.0,
        }
    first_ns, first = values[0]
    last_ns, last = values[-1]
    elapsed_hours = max(0, last_ns - first_ns) / 3_600_000_000_000.0
    growth = last - first
    return {
        "count": len(values),
        "first": first,
        "last": last,
        "growth": growth,
        "growth_per_hour": growth / elapsed_hours if elapsed_hours > 0 else 0.0,
        "max": max(value for _timestamp_ns, value in values),
    }


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
