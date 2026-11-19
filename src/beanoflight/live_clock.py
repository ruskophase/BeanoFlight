"""Short-horizon hardware-counter to host-monotonic clock snapshots.

Only simultaneous counter/host samples enter this model, never frame arrivals.
Rate is source nanoseconds per host nanosecond, matching actuation wire timing.
"""
from collections import deque
from dataclasses import dataclass, replace
import threading


class ClockMappingError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ClockSnapshot:
    source_ns: int
    monotonic_ns: int
    source_rate_ppb: int
    epoch: int
    bracket_ns: int
    residual_ns: int

    def source_to_monotonic_ns(self, source_ns: int) -> int:
        return self.monotonic_ns + round(
            (source_ns - self.source_ns) * 1_000_000_000 / self.source_rate_ppb)

    def apply(self, session):
        """Return a new session; outstanding plans retain their old snapshot."""
        return replace(session, clock_source_timestamp_ns=self.source_ns,
                       clock_monotonic_ns=self.monotonic_ns,
                       settings={**session.settings,
                                 'live_clock_source_rate_ppb': self.source_rate_ppb,
                                 'live_clock_epoch': self.epoch})


class LiveClockModel:
    MAX_SAMPLE_AGE_NS = 3_000_000_000

    def __init__(self):
        self._samples = deque(maxlen=11)
        self._snapshot = None
        self._failure = None
        self._lock = threading.Lock()

    def observe(self, source_ns: int, host_ns: int, bracket_ns: int):
        with self._lock:
            if self._failure:
                raise ClockMappingError(self._failure)
            try:
                if source_ns <= 0 or host_ns <= 0 or not 0 <= bracket_ns <= 50_000:
                    raise ClockMappingError('invalid or imprecise hardware clock sample')
                previous = self._snapshot
                if previous and (source_ns <= previous.source_ns or host_ns <= previous.monotonic_ns):
                    raise ClockMappingError('hardware clock samples are not increasing')
                residual = (host_ns - previous.source_to_monotonic_ns(source_ns)) if previous else 0
                if previous and abs(residual) > 1_000_000:
                    raise ClockMappingError('hardware clock mapping jumped by more than 1 ms')
                self._samples.append((source_ns, host_ns))
                first_source, first_host = self._samples[0]
                rate = previous.source_rate_ppb if previous else 1_000_000_000
                if host_ns - first_host >= 500_000_000:
                    rate = round((source_ns-first_source)*1_000_000_000/(host_ns-first_host))
                if abs(rate - 1_000_000_000) > 2_000_000:
                    raise ClockMappingError('hardware clock rate differs by more than 2000 ppm')
                self._snapshot = ClockSnapshot(source_ns, host_ns, rate,
                    previous.epoch + 1 if previous else 1, bracket_ns, residual)
            except ClockMappingError as exc:
                self._failure = str(exc)
                raise

    def snapshot(self, now_ns: int) -> ClockSnapshot:
        with self._lock:
            if self._failure:
                raise ClockMappingError(self._failure)
            snap = self._snapshot
            if snap is None:
                raise ClockMappingError('no hardware clock samples received')
            age = now_ns - snap.monotonic_ns
            if age < -50_000 or age > self.MAX_SAMPLE_AGE_NS:
                raise ClockMappingError('hardware clock sample is stale or in the future')
            return snap
