"""Repeatable isolated multi-process benchmark for the simulation hot path."""

from __future__ import annotations

import argparse
import json
import multiprocessing
import os
import subprocess
import sys
import tempfile
import time
from collections.abc import Sequence
from pathlib import Path

from .classification import (
    CLASSIFICATION_DECISION_BASIS,
    CLASSIFICATION_EVIDENCE,
    CLASSIFICATION_POOLED,
)
from .esp32_actuator import DEFAULT_ESP32_PORT, ESP32ActuatorService
from .mock_inference import MockInferencerService
from .registry_models import InferenceStatus
from .registry_zmq import ZeroMQRegistryClient
from .runtime_priority import (
    apply_latency_thread_profile,
    apply_performance_affinity,
)
from .sorter import SorterService
from .telemetry import summarize_samples
from .timing_ledger import summarize_timing_ledgers


def _scenarios(value: str) -> tuple[str, ...]:
    result = tuple(item.strip().lower() for item in value.split(",") if item.strip())
    if not result or any(item not in {"core", "full"} for item in result):
        raise argparse.ArgumentTypeError("scenarios must contain core and/or full")
    return tuple(dict.fromkeys(result))


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        prog="beano-performance-benchmark",
        description="Run repeatable core and full-pipeline BeanoFlight replays",
    )
    result.add_argument("recording", type=Path)
    result.add_argument(
        "--background-frames",
        required=True,
        help="3 human-confirmed empty zero-based frame indices",
    )
    result.add_argument("--scenarios", type=_scenarios, default=("core", "full"))
    result.add_argument("--repeats", type=int, default=5)
    result.add_argument("--target-fps", type=float, default=60.0)
    result.add_argument("--maximum-frames", type=int, default=1_000)
    result.add_argument("--prebuffer-frames", type=int, default=60)
    result.add_argument("--crops-per-bean", type=int, default=1)
    result.add_argument("--crop-size", type=int, default=224)
    result.add_argument(
        "--no-adaptive-edge-resize",
        action="store_true",
        help="disable resizing smaller complete crops near the frame edge",
    )
    result.add_argument(
        "--crop-processing",
        choices=("ml-fast", "calibrated"),
        default="ml-fast",
    )
    result.add_argument("--database", type=Path)
    result.add_argument("--output", type=Path)
    result.add_argument(
        "--esp32-actuator",
        action="store_true",
        help="send approved plans to the connected ESP32-S2 actuator",
    )
    result.add_argument("--esp32-port", default=DEFAULT_ESP32_PORT)
    return result


def main(argv: Sequence[str] | None = None) -> int:
    arguments = parser().parse_args(argv)
    if arguments.repeats <= 0:
        raise SystemExit("--repeats must be positive")
    if arguments.target_fps < 0:
        raise SystemExit("--target-fps cannot be negative")

    temporary = tempfile.TemporaryDirectory(prefix="beanoflight-benchmark-")
    root = Path(temporary.name)
    database = arguments.database or root / "benchmark.db"
    commands = f"ipc://{root / 'commands.ipc'}"
    events = f"ipc://{root / 'events.ipc'}"
    crops = f"ipc://{root / 'crops.ipc'}"
    classifications = f"ipc://{root / 'classifications.ipc'}"
    sorting_contexts = f"ipc://{root / 'sorting-contexts.ipc'}"
    actuation_plans = f"ipc://{root / 'actuation-plans.ipc'}"
    registry_process = None
    context = multiprocessing.get_context("spawn")
    service_stop = context.Event()
    inference_ready = context.Event()
    sorter_ready = context.Event()
    actuator_ready = context.Event()
    inferencer = context.Process(
        target=_run_inferencer,
        args=(commands, crops, classifications, service_stop, inference_ready),
        name="beano-benchmark-inferencer",
    )
    sorter = context.Process(
        target=_run_sorter,
        args=(
            commands,
            events,
            classifications,
            sorting_contexts,
            actuation_plans if arguments.esp32_actuator else "",
            service_stop,
            sorter_ready,
        ),
        name="beano-benchmark-sorter",
    )
    actuator = (
        context.Process(
            target=_run_actuator,
            args=(
                commands,
                actuation_plans,
                arguments.esp32_port,
                service_stop,
                actuator_ready,
            ),
            name="beano-benchmark-actuator",
        )
        if arguments.esp32_actuator
        else None
    )
    try:
        registry_process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "beanoflight.registry_service",
                "--database",
                str(database),
                "--commands",
                commands,
                "--events",
                events,
                "--quiet",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        apply_performance_affinity("general", pid=registry_process.pid)
        _wait_for_registry(commands, registry_process)
        if actuator is not None:
            actuator.start()
            if not actuator_ready.wait(5.0):
                raise RuntimeError(
                    "benchmark ESP32 actuator did not connect and synchronize"
                )
        sorter.start()
        if not sorter_ready.wait(5.0):
            raise RuntimeError("benchmark sorter did not become ready")
        inferencer.start()
        if not inference_ready.wait(5.0):
            raise RuntimeError("benchmark inferencer did not become ready")

        runs: list[dict[str, object]] = []
        for scenario in arguments.scenarios:
            for repeat in range(1, arguments.repeats + 1):
                summary = _run_replay(
                    arguments,
                    commands,
                    crops,
                    sorting_contexts,
                    scenario,
                )
                outcome = _wait_for_outcome(
                    commands,
                    str(summary["run_id"]),
                    int(summary["crops_submitted"]),
                )
                run = {
                    "scenario": scenario,
                    "repeat": repeat,
                    "summary": summary,
                    "outcome": outcome,
                }
                runs.append(run)
                print(
                    f"{scenario} {repeat}/{arguments.repeats}: "
                    f"{float(summary['achieved_fps']):.2f} FPS · "
                    f"analysis {float(summary['mean_processing_ms']):.2f} ms · "
                    f"skipped {int(summary.get('frames_skipped', 0))} · "
                    f"crops {int(summary['crops_submitted'])} · "
                    f"drops {int(summary['crops_dropped'])}",
                    flush=True,
                )
        report = {
            "schema": "beanoflight-performance-benchmark/v1",
            "recording": str(arguments.recording.resolve()),
            "database": str(database.resolve()),
            "target_fps": arguments.target_fps,
            "repeats": arguments.repeats,
            "adaptive_edge_resize": not arguments.no_adaptive_edge_resize,
            "esp32_actuator": bool(arguments.esp32_actuator),
            "esp32_port": arguments.esp32_port,
            "summaries": _scenario_summaries(
                runs,
                arguments.target_fps,
                require_successful_actuations=arguments.esp32_actuator,
            ),
            "runs": runs,
        }
        encoded = json.dumps(report, indent=2, allow_nan=False) + "\n"
        if arguments.output is not None:
            arguments.output.write_text(encoded, encoding="utf-8")
            print(f"Report: {arguments.output.resolve()}")
        else:
            print(encoded, end="")
        return 0
    finally:
        service_stop.set()
        for process in (sorter, inferencer, actuator):
            if process is None:
                continue
            if process.pid is not None:
                process.join(3.0)
                if process.is_alive():
                    process.terminate()
                    process.join(2.0)
        if registry_process is not None and registry_process.poll() is None:
            registry_process.terminate()
            try:
                registry_process.wait(timeout=3.0)
            except subprocess.TimeoutExpired:
                registry_process.kill()
                registry_process.wait(timeout=2.0)
        temporary.cleanup()


def _run_inferencer(commands, crops, classifications, stop, ready) -> None:
    apply_performance_affinity("general")
    service = MockInferencerService(
        registry_endpoint=commands,
        crop_endpoint=crops,
        classification_endpoint=classifications,
        activity=None,
    )
    service.start()
    if service.ready.wait(5.0):
        ready.set()
    stop.wait()
    service.close(drain=False)


def _run_sorter(
    commands,
    events,
    classifications,
    sorting_contexts,
    actuation_plans,
    stop,
    ready,
) -> None:
    apply_performance_affinity("sorter")
    apply_latency_thread_profile()
    service = SorterService(
        registry_endpoint=commands,
        event_endpoint=events,
        classification_endpoint=classifications,
        sorting_context_endpoint=sorting_contexts,
        actuation_endpoint=actuation_plans,
        activity=None,
    )
    service.start()
    if service.ready.wait(5.0) and not service.startup_error:
        ready.set()
    stop.wait()
    service.close()


def _run_actuator(commands, plans, serial_port, stop, ready) -> None:
    apply_performance_affinity("actuator")
    apply_latency_thread_profile()
    service = ESP32ActuatorService(
        registry_endpoint=commands,
        actuation_endpoint=plans,
        serial_port=serial_port,
        activity=None,
    )
    service.start()
    service.ready.wait(5.0)
    deadline = time.monotonic() + 5.0
    while (
        not stop.is_set()
        and not service.startup_error
        and not service.synchronized
        and time.monotonic() < deadline
    ):
        stop.wait(0.02)
    if service.synchronized and not service.startup_error:
        ready.set()
    stop.wait()
    service.close(drain=False)


def _wait_for_registry(endpoint: str, process: subprocess.Popen) -> None:
    client = ZeroMQRegistryClient(endpoint, timeout_ms=100)
    try:
        for _attempt in range(100):
            if process.poll() is not None:
                detail = "" if process.stderr is None else process.stderr.read().strip()
                raise RuntimeError(f"benchmark registry exited: {detail}")
            try:
                client.ping()
                return
            except Exception:  # noqa: BLE001 - bounded startup retry
                time.sleep(0.02)
    finally:
        client.close()
    raise RuntimeError("benchmark registry did not become ready")


def _run_replay(
    arguments,
    commands: str,
    crops: str,
    sorting_contexts: str,
    scenario: str,
) -> dict:
    command = [
        sys.executable,
        "-m",
        "beanoflight.system_test",
        str(arguments.recording),
        "--background-frames",
        arguments.background_frames,
        "--optimized-raw",
        "--target-fps",
        str(arguments.target_fps),
        "--prebuffer-frames",
        str(arguments.prebuffer_frames),
        "--maximum-frames",
        str(arguments.maximum_frames),
        "--crops-per-bean",
        str(arguments.crops_per_bean),
        "--crop-size",
        str(arguments.crop_size),
        "--crop-processing",
        arguments.crop_processing,
        "--registry",
        commands,
        "--crops",
        crops,
        "--sorting-contexts",
        sorting_contexts,
        "--progress-every",
        str(arguments.maximum_frames + 1),
    ]
    if scenario == "core":
        command.append("--no-crops")
    if arguments.no_adaptive_edge_resize:
        command.append("--no-adaptive-edge-resize")
    replay = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=dict(os.environ),
    )
    apply_performance_affinity("general", pid=replay.pid)
    stdout, stderr = replay.communicate()
    if replay.returncode:
        raise RuntimeError(
            f"{scenario} replay failed ({replay.returncode}): {stderr}"
        )
    marker = stdout.rfind("\n{")
    if marker < 0:
        raise RuntimeError(f"{scenario} replay did not return a JSON summary")
    return json.loads(stdout[marker + 1 :])


def _wait_for_outcome(
    endpoint: str, run_id: str, expected_jobs: int
) -> dict[str, int | bool]:
    client = ZeroMQRegistryClient(endpoint, timeout_ms=2_000)
    records = ()
    settled = False
    try:
        deadline = time.monotonic() + 5.0
        while True:
            records = client.list_records(run_id=run_id)
            jobs = tuple(job for record in records for job in record.inference_jobs)
            decisions = sum(record.decision is not None for record in records)
            expected_decisions = sum(bool(record.inference_jobs) for record in records)
            terminal = sum(
                job.status
                in {
                    InferenceStatus.COMPLETED,
                    InferenceStatus.DROPPED,
                    InferenceStatus.FAILED,
                }
                for job in jobs
            )
            finalized_decisions = sum(
                record.decision is not None
                and (
                    record.decision.acknowledged_timestamp_ns is not None
                    or record.actuation is not None
                )
                for record in records
            )
            if (
                len(jobs) >= expected_jobs
                and terminal >= expected_jobs
                and decisions >= expected_decisions
                and finalized_decisions >= decisions
            ):
                settled = True
                break
            if time.monotonic() >= deadline:
                break
            time.sleep(0.02)
        evicted = client.evict_completed(
            before_timestamp_ns=(1 << 63) - 1,
            run_id=run_id,
        )
    finally:
        client.close()
    jobs = tuple(job for record in records for job in record.inference_jobs)
    actuation_failures = tuple(
        {
            "bean_id": str(record.bean_ref),
            "source": record.actuation.source,
            "detail": record.actuation.detail,
        }
        for record in records
        if record.actuation is not None and not record.actuation.success
    )
    evidence = tuple(
        item
        for record in records
        for item in record.enrichments
        if item.kind == CLASSIFICATION_EVIDENCE
    )
    pooled = tuple(
        item
        for record in records
        for item in record.enrichments
        if item.kind == CLASSIFICATION_POOLED
    )
    decision_bases = tuple(
        item
        for record in records
        for item in record.enrichments
        if item.kind == CLASSIFICATION_DECISION_BASIS
    )
    return {
        "beans": len(records),
        "beans_with_jobs": sum(bool(record.inference_jobs) for record in records),
        "jobs": len(jobs),
        "jobs_completed": sum(job.status == InferenceStatus.COMPLETED for job in jobs),
        "jobs_dropped": sum(job.status == InferenceStatus.DROPPED for job in jobs),
        "jobs_failed": sum(job.status == InferenceStatus.FAILED for job in jobs),
        "classification_evidence": len(evidence),
        "classification_pooled": len(pooled),
        "classification_decision_bases": len(decision_bases),
        "classification_complete_pools": sum(
            isinstance(item.value, dict)
            and isinstance(item.value.get("ensemble"), dict)
            and not item.value["ensemble"].get("deadline_fallback", False)
            for item in pooled
        ),
        "classification_deadline_fallbacks": sum(
            isinstance(item.value, dict)
            and isinstance(item.value.get("ensemble"), dict)
            and bool(item.value["ensemble"].get("deadline_fallback", False))
            for item in pooled
        ),
        "decisions": sum(record.decision is not None for record in records),
        "actuations": sum(record.actuation is not None for record in records),
        "actuations_succeeded": sum(
            record.actuation is not None and record.actuation.success
            for record in records
        ),
        "actuations_failed": sum(
            record.actuation is not None and not record.actuation.success
            for record in records
        ),
        "actuation_failures": actuation_failures,
        "two_gate_decisions": sum(
            record.decision is not None and len(record.decision.gate_indices) == 2
            for record in records
        ),
        "combined_probability_decisions": sum(
            record.decision is not None
            and "adjacent gates combined probability" in record.decision.reason
            for record in records
        ),
        "low_confidence_defects": sum(
            record.decision is not None
            and "below confidence threshold" in record.decision.reason
            for record in records
        ),
        "hot_records_evicted": evicted,
        "settled": settled,
        "timing_ledger": summarize_timing_ledgers(records),
    }


def _scenario_summaries(
    runs: list[dict[str, object]],
    target_fps: float,
    *,
    require_successful_actuations: bool = False,
) -> dict[str, object]:
    minimum_acceptable_fps = max(0.0, target_fps - 1.0)
    scenarios: dict[str, object] = {}
    for scenario in ("core", "full"):
        selected = [run for run in runs if run["scenario"] == scenario]
        if not selected:
            continue
        fps = [float(run["summary"]["achieved_fps"]) for run in selected]
        timeline_fps = [
            float(
                run["summary"].get(
                    "source_timeline_fps", run["summary"]["achieved_fps"]
                )
            )
            for run in selected
        ]
        frames_skipped = [
            int(run["summary"].get("frames_skipped", 0)) for run in selected
        ]
        analysis = [float(run["summary"]["mean_processing_ms"]) for run in selected]
        within_target = all(value >= minimum_acceptable_fps for value in fps)
        outcomes_complete = all(
            bool(run["outcome"]["settled"])
            and int(run["outcome"]["jobs"]) == int(run["summary"]["crops_submitted"])
            and int(run["outcome"]["jobs_completed"]) == int(run["outcome"]["jobs"])
            and int(run["outcome"]["jobs_dropped"]) == 0
            and int(run["outcome"]["jobs_failed"]) == 0
            and int(run["outcome"]["decisions"])
            == int(run["outcome"]["beans_with_jobs"])
            and (
                not require_successful_actuations
                or int(run["outcome"].get("actuations_failed", 0)) == 0
            )
            for run in selected
        )
        scenarios[scenario] = {
            "fps": summarize_samples(fps),
            "source_timeline_fps": summarize_samples(timeline_fps),
            "frames_skipped": summarize_samples(frames_skipped),
            "mean_analysis_ms": summarize_samples(analysis),
            "minimum_fps": min(fps),
            "minimum_acceptable_fps": minimum_acceptable_fps,
            "all_within_one_fps_of_target": within_target,
            "all_outcomes_complete": outcomes_complete,
            "passed": within_target and outcomes_complete,
        }
    return scenarios


if __name__ == "__main__":
    raise SystemExit(main())
