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
    """Apply the role's CPU set to every extant thread in a process.

    Linux affinity is a per-thread property even when ``pid`` names a process
    leader.  Python, OpenCV, CUDA and ZeroMQ may create native threads while
    modules are imported, before the performance role is applied.  Pinning the
    leader alone therefore lets those threads continue to execute on CPUs
    reserved for the sorter or actuator.
    """

    if not hasattr(os, "sched_getaffinity") or not hasattr(os, "sched_setaffinity"):
        return frozenset()
    cpus = performance_cpu_set(role)
    if not cpus:
        return cpus
    process_id = os.getpid() if pid == 0 else int(pid)
    applied: set[int] = set()
    # A second sweep closes the small race in which an existing native thread
    # creates another thread while the first sweep is in progress.  Threads
    # created after their parent is pinned inherit the corrected mask.
    for _sweep in range(2):
        task_ids = _process_task_ids(process_id)
        for task_id in task_ids:
            if task_id in applied:
                continue
            try:
                os.sched_setaffinity(task_id, cpus)
            except ProcessLookupError:
                continue
            except OSError:
                return frozenset()
            applied.add(task_id)
        if set(_process_task_ids(process_id)).issubset(applied):
            break
    return cpus


def _process_task_ids(process_id: int) -> tuple[int, ...]:
    """Return Linux thread IDs, falling back to the process leader."""

    try:
        with os.scandir(f"/proc/{process_id}/task") as entries:
            task_ids = tuple(
                sorted(
                    int(entry.name)
                    for entry in entries
                    if entry.name.isdigit()
                )
            )
    except OSError:
        return (process_id,)
    return task_ids or (process_id,)


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


def apply_background_audit_thread_profile(
    *, cpu_from_end: int = 1
) -> frozenset[int]:
    """Put optional audit work behind the actuator on its reserved CPU.

    The lower scheduler priority (a higher numeric nice value) is the safety
    boundary: an actuator thread on the same CPU pre-empts this worker.
    Keeping audit work away from the general CPU set also prevents it
    competing with acquisition, detection and model inference when all cores
    are busy.
    """

    lower_current_thread_priority()
    if not all(
        hasattr(os, name)
        for name in ("cpu_count", "sched_setaffinity")
    ):
        return frozenset()
    cpu_count = os.cpu_count() or 0
    if cpu_count < 2 or cpu_from_end < 1:
        return frozenset()
    selected = frozenset((max(0, cpu_count - cpu_from_end),))
    try:
        os.sched_setaffinity(threading.get_native_id(), selected)
    except OSError:
        return frozenset()
    return selected
