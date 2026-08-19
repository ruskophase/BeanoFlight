"""Portable CPU placement for the latency-sensitive performance profile."""

from __future__ import annotations

import os
import sys
import threading
from collections.abc import Iterable


def performance_cpu_set(
    role: str,
    available: Iterable[int] | None = None,
) -> frozenset[int]:
    """Reserve the highest two CPUs for actuator and sorter when practical."""

    cpus = tuple(
        sorted(
            os.sched_getaffinity(0) if available is None else set(available)
        )
    )
    if len(cpus) < 4:
        return frozenset(cpus)
    if role == "actuator":
        return frozenset((cpus[-1],))
    if role == "sorter":
        return frozenset((cpus[-2],))
    if role == "general":
        return frozenset(cpus[:-2])
    raise ValueError(f"unknown performance CPU role {role!r}")


def apply_performance_affinity(role: str, *, pid: int = 0) -> frozenset[int]:
    """Apply the role's CPU set, returning an empty set when unsupported."""

    if not hasattr(os, "sched_getaffinity") or not hasattr(os, "sched_setaffinity"):
        return frozenset()
    cpus = performance_cpu_set(role)
    if not cpus:
        return cpus
    try:
        os.sched_setaffinity(pid, cpus)
    except OSError:
        return frozenset()
    return cpus


def apply_latency_thread_profile(*, switch_interval_ms: float = 1.0) -> float:
    """Shorten Python's GIL handoff for a process with real-time worker threads."""

    previous = sys.getswitchinterval()
    sys.setswitchinterval(max(0.1, float(switch_interval_ms)) / 1_000.0)
    return previous


def lower_current_thread_priority(*, increment: int = 10) -> bool:
    """Best-effort Linux nice adjustment for a non-deadline audit worker."""

    if not all(
        hasattr(os, name)
        for name in ("getpriority", "setpriority", "PRIO_PROCESS")
    ):
        return False
    try:
        thread_id = threading.get_native_id()
        current = os.getpriority(os.PRIO_PROCESS, thread_id)
        os.setpriority(
            os.PRIO_PROCESS,
            thread_id,
            min(19, current + max(1, int(increment))),
        )
    except OSError:
        return False
    return True
