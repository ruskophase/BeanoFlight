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
from .mock_inference import MockInferencerService, MockInferenceSettings
from .registry_models import InferenceStatus
from .registry_zmq import ZeroMQRegistryClient
from .runtime_priority import (
    apply_latency_thread_profile,
    apply_performance_affinity,
)
from .sorter import SorterService
from .telemetry import summarize_samples
from .tensorrt_inference import DEFAULT_TENSORRT_ENGINE
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
    result.add_argument("--clock-start-lead-ms", type=float, default=50.0)
    result.add_argument("--maximum-clock-offset-ms", type=float, default=2.0)
    result.add_argument("--crops-per-bean", type=int, default=1)
    result.add_argument("--crop-size", type=int, default=224)
    result.add_argument(
        "--inference-backend",
        choices=("tensorrt", "mock"),
        default=("tensorrt" if DEFAULT_TENSORRT_ENGINE.is_file() else "mock"),
        help="real TensorRT execution or conservative deterministic timing model",
    )
    result.add_argument(
        "--inference-engine",
        type=Path,
        default=DEFAULT_TENSORRT_ENGINE,
        help="shared-layer1 stereo ResNet18 TensorRT engine",
    )
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
    result.add_argument(
        "--single-view-inference",
        action="store_true",
        help="A/B baseline: duplicate CamL instead of transporting genuine CamR",
    )
    result.add_argument(
        "--no-emergency-microbatch",
        action="store_true",
        help="disable deadline-aware second-sample microbatches for A/B testing",
    )
    result.add_argument("--database", type=Path)
    result.add_argument("--output", type=Path)
    result.add_argument(
        "--soak-runs",
        type=int,
        default=0,
        metavar="N",
        help=(
            "run N full-pipeline repetitions and fail unless every acceptance "
            "criterion passes"
        ),
    )
    result.add_argument(
        "--minimum-three-sample-rate",
        type=float,
        default=0.95,
    )
    result.add_argument("--minimum-samples-per-bean", type=int, default=2)
    result.add_argument(
        "--expected-beans",
        type=int,
        help="optional exact public bean-ID count required in every soak run",
    )
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
    if arguments.clock_start_lead_ms <= 0 or arguments.maximum_clock_offset_ms <= 0:
        raise SystemExit("clock timing limits must be positive")
    if arguments.soak_runs < 0:
        raise SystemExit("--soak-runs cannot be negative")
    if not 0.0 <= arguments.minimum_three_sample_rate <= 1.0:
        raise SystemExit("--minimum-three-sample-rate must be between zero and one")
    if not 1 <= arguments.minimum_samples_per_bean <= 5:
        raise SystemExit("--minimum-samples-per-bean must be between one and five")
    if arguments.expected_beans is not None and arguments.expected_beans <= 0:
        raise SystemExit("--expected-beans must be positive")
    if arguments.soak_runs:
        if arguments.crops_per_bean < 3:
            raise SystemExit("soak acceptance requires --crops-per-bean 3 or more")
        arguments.scenarios = ("full",)
        arguments.repeats = arguments.soak_runs

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
        args=(
            commands,
            crops,
            classifications,
            arguments.inference_backend,
            str(arguments.inference_engine),
            service_stop,
            inference_ready,
        ),
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
                    f"drops {int(summary['crops_dropped'])} · "
                    f"clock {float(summary.get('clock_start_offset_ms', 0.0)):+.3f} ms",
                    flush=True,
                )
        acceptance = (
            _soak_acceptance(
                runs,
                target_fps=arguments.target_fps,
                minimum_three_sample_rate=arguments.minimum_three_sample_rate,
                minimum_samples_per_bean=arguments.minimum_samples_per_bean,
                expected_beans=arguments.expected_beans,
            )
            if arguments.soak_runs
            else None
        )
        report = {
            "schema": "beanoflight-performance-benchmark/v2",
            "recording": str(arguments.recording.resolve()),
            "database": str(database.resolve()),
            "target_fps": arguments.target_fps,
            "repeats": arguments.repeats,
            "adaptive_edge_resize": not arguments.no_adaptive_edge_resize,
            "genuine_stereo_crops": not arguments.single_view_inference,
            "emergency_microbatch": not arguments.no_emergency_microbatch,
            "inference_backend": arguments.inference_backend,
            "inference_engine": str(arguments.inference_engine.resolve()),
            "esp32_actuator": bool(arguments.esp32_actuator),
            "esp32_port": arguments.esp32_port,
            "summaries": _scenario_summaries(
                runs,
                arguments.target_fps,
                require_successful_actuations=arguments.esp32_actuator,
            ),
            "acceptance": acceptance,
            "runs": runs,
        }
        encoded = json.dumps(report, indent=2, allow_nan=False) + "\n"
        if arguments.output is not None:
            arguments.output.write_text(encoded, encoding="utf-8")
            print(f"Report: {arguments.output.resolve()}")
        else:
            print(encoded, end="")
        if acceptance is not None:
            print(
                "Soak acceptance: "
                + ("PASS" if acceptance["passed"] else "FAIL"),
                flush=True,
            )
        return 0 if acceptance is None or acceptance["passed"] else 2
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


def _run_inferencer(
    commands, crops, classifications, backend, engine_path, stop, ready
) -> None:
    apply_performance_affinity("general")
    service = MockInferencerService(
        registry_endpoint=commands,
        crop_endpoint=crops,
        classification_endpoint=classifications,
        settings=MockInferenceSettings(
            backend=backend,
            engine_path=engine_path if backend == "tensorrt" else "",
        ),
        activity=None,
    )
    service.start()
    if service.ready.wait(15.0) and not service.startup_error:
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
        "--clock-start-lead-ms",
        str(arguments.clock_start_lead_ms),
        "--maximum-clock-offset-ms",
        str(arguments.maximum_clock_offset_ms),
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
    if arguments.single_view_inference:
        command.append("--single-view-inference")
    if arguments.no_emergency_microbatch:
        command.append("--no-emergency-microbatch")
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
) -> dict[str, object]:
    client = ZeroMQRegistryClient(endpoint, timeout_ms=2_000)
    records = ()
    session = None
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
        session = client.get_session(run_id)
        evicted = client.evict_completed(
            before_timestamp_ns=(1 << 63) - 1,
            run_id=run_id,
        )
    finally:
        client.close()
    jobs = tuple(job for record in records for job in record.inference_jobs)
    incomplete_jobs = tuple(
        {
            "bean_id": str(record.bean_ref),
            "job_id": job.job_id,
            "status": job.status.value,
            "detail": job.detail,
        }
        for record in records
        for job in record.inference_jobs
        if job.status != InferenceStatus.COMPLETED
    )
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
    evidence_inference = tuple(
        item.value.get("inference", {})
        for item in evidence
        if isinstance(item.value, dict)
        and isinstance(item.value.get("inference"), dict)
    )
    pair_values = tuple(
        inference.get("stereo_pair")
        for inference in evidence_inference
        if bool(inference.get("stereo_pair_complete", False))
    )
    sync_deltas_ns = tuple(
        abs(int(pair.get("synchronization_delta_ns", 0)))
        for pair in pair_values
        if isinstance(pair, dict)
    )
    refinement_distances_px = tuple(
        float(pair.get("refinement_distance_px", 0.0))
        for pair in pair_values
        if isinstance(pair, dict)
    )
    return {
        "beans": len(records),
        "beans_with_jobs": sum(bool(record.inference_jobs) for record in records),
        "jobs": len(jobs),
        "jobs_completed": sum(job.status == InferenceStatus.COMPLETED for job in jobs),
        "jobs_dropped": sum(job.status == InferenceStatus.DROPPED for job in jobs),
        "jobs_failed": sum(job.status == InferenceStatus.FAILED for job in jobs),
        "incomplete_jobs": incomplete_jobs,
        "classification_evidence": len(evidence),
        "stereo_pairs_complete": sum(
            bool(inference.get("stereo_pair_complete", False))
            for inference in evidence_inference
        ),
        "stereo_pairs_incomplete": sum(
            not bool(inference.get("stereo_pair_complete", False))
            for inference in evidence_inference
        ),
        "stereo_pairing": {
            "maximum_synchronization_delta_ns": max(sync_deltas_ns, default=0),
            "refinement_distance_px": summarize_samples(
                refinement_distances_px
            ),
        },
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
        "clock_consistency": _clock_consistency(records, session),
        "identity_continuity": _identity_continuity(records),
        "timing_ledger": summarize_timing_ledgers(records),
    }


def _clock_consistency(records, session) -> dict[str, object]:
    raw_contract = (
        session.settings.get("clock_contract", {})
        if session is not None and isinstance(session.settings, dict)
        else {}
    )
    raw_contracts = (
        session.settings.get("clock_contracts", ())
        if session is not None and isinstance(session.settings, dict)
        else ()
    )
    if not isinstance(raw_contracts, (list, tuple)):
        raw_contracts = ()
    contracts = {}
    for contract in (*raw_contracts, raw_contract):
        if not isinstance(contract, dict):
            continue
        epoch = int(contract.get("epoch", -1))
        contracts[epoch] = {
            "run_clock_source_ns": int(contract.get("source_timestamp_ns", -1)),
            "run_clock_monotonic_ns": int(contract.get("monotonic_ns", -1)),
            "run_clock_epoch": epoch,
            "run_clock_scale_ppb": int(contract.get("playback_scale_ppb", -1)),
        }
    job_mismatches = []
    decision_mismatches = []
    evidence_mismatches = []
    jobs_checked = 0
    decisions_checked = 0
    evidence_checked = 0
    for record in records:
        for job in record.inference_jobs:
            jobs_checked += 1
            actual = job.timing_marks_ns
            expected = contracts.get(int(actual.get("run_clock_epoch", -1)), {})
            differences = {
                key: int(actual.get(key, -1))
                for key, value in expected.items()
                if int(actual.get(key, -1)) != value
            }
            if not expected:
                differences["run_clock_epoch"] = int(
                    actual.get("run_clock_epoch", -1)
                )
            if differences and len(job_mismatches) < 20:
                job_mismatches.append(
                    {
                        "job_id": job.job_id,
                        "differences": differences,
                    }
                )
        if record.decision is not None:
            decisions_checked += 1
            actual = record.decision.timing_marks_ns
            expected = contracts.get(int(actual.get("run_clock_epoch", -1)), {})
            differences = {
                key: int(actual.get(key, -1))
                for key, value in expected.items()
                if int(actual.get(key, -1)) != value
            }
            if not expected:
                differences["run_clock_epoch"] = int(
                    actual.get("run_clock_epoch", -1)
                )
            if differences and len(decision_mismatches) < 20:
                decision_mismatches.append(
                    {
                        "decision_id": record.decision.decision_id,
                        "differences": differences,
                    }
                )
        for enrichment in record.enrichments:
            if enrichment.kind != CLASSIFICATION_EVIDENCE:
                continue
            evidence_checked += 1
            inference = (
                enrichment.value.get("inference", {})
                if isinstance(enrichment.value, dict)
                else {}
            )
            actual_epoch = int(inference.get("clock_epoch", -1))
            consistent = bool(inference.get("clock_consistent", False))
            if (
                actual_epoch not in contracts or not consistent
            ) and len(evidence_mismatches) < 20:
                evidence_mismatches.append(
                    {
                        "result_id": enrichment.result_id,
                        "clock_epoch": actual_epoch,
                        "clock_consistent": consistent,
                    }
                )
    contract_valid = bool(contracts) and all(
        all(value >= 0 for value in contract.values())
        for contract in contracts.values()
    )
    return {
        "contracts": contracts,
        "contract_valid": contract_valid,
        "jobs_checked": jobs_checked,
        "job_mismatches": job_mismatches,
        "decisions_checked": decisions_checked,
        "decision_mismatches": decision_mismatches,
        "evidence_checked": evidence_checked,
        "evidence_mismatches": evidence_mismatches,
        "all_consistent": bool(contract_valid)
        and not job_mismatches
        and not decision_mismatches
        and not evidence_mismatches,
    }


def _identity_continuity(records) -> dict[str, object]:
    """Find likely public-ID splits across adjacent falling-bean observations."""

    suspects = []
    candidates = [record for record in records if record.track.history]
    for earlier in candidates:
        earlier_history = earlier.track.history
        last = earlier_history[-1]
        for later in candidates:
            if later.bean_ref == earlier.bean_ref:
                continue
            first = later.track.history[0]
            frame_gap = first.frame_index - last.frame_index
            if not 1 <= frame_gap <= 2 or first.timestamp_ns <= last.timestamp_ns:
                continue
            if first.position_mm[1] <= last.position_mm[1]:
                continue
            dt = (first.timestamp_ns - last.timestamp_ns) / 1_000_000_000.0
            if len(earlier_history) >= 2:
                previous = earlier_history[-2]
                history_dt = (
                    last.timestamp_ns - previous.timestamp_ns
                ) / 1_000_000_000.0
                if history_dt <= 0:
                    continue
                vx = (last.position_mm[0] - previous.position_mm[0]) / history_dt
                vy = (last.position_mm[1] - previous.position_mm[1]) / history_dt
            else:
                vx = earlier.track.state[2]
                vy = earlier.track.state[3]
            predicted_x = last.position_mm[0] + vx * dt
            predicted_y = last.position_mm[1] + vy * dt + 0.5 * 9_810.0 * dt * dt
            x_residual = abs(first.position_mm[0] - predicted_x)
            y_residual = abs(first.position_mm[1] - predicted_y)
            if x_residual > 4.0 or y_residual > 10.0:
                continue
            if earlier.prediction is not None and later.prediction is not None:
                crossing_delta_ms = abs(
                    earlier.prediction.crossing_timestamp_ns
                    - later.prediction.crossing_timestamp_ns
                ) / 1_000_000.0
                if crossing_delta_ms > 30.0:
                    continue
            else:
                crossing_delta_ms = None
            suspects.append(
                {
                    "earlier_bean_id": str(earlier.bean_ref),
                    "later_bean_id": str(later.bean_ref),
                    "frame_gap": frame_gap,
                    "x_residual_mm": x_residual,
                    "y_residual_mm": y_residual,
                    "crossing_delta_ms": crossing_delta_ms,
                }
            )
    return {
        "suspected_fragments": len(suspects),
        "suspects": suspects[:20],
    }


def _soak_acceptance(
    runs: list[dict[str, object]],
    *,
    target_fps: float,
    minimum_three_sample_rate: float,
    minimum_samples_per_bean: int,
    expected_beans: int | None,
) -> dict[str, object]:
    selected = [run for run in runs if run.get("scenario") == "full"]
    bean_counts = [int(run["outcome"]["beans"]) for run in selected]
    per_run_three_sample_rates = []
    per_run_minimum_samples = []
    for run in selected:
        classifications = [
            bean.get("classification")
            for bean in run["outcome"]["timing_ledger"].get("per_bean", ())
            if bean.get("classification") is not None
        ]
        complete = sum(
            int(item.get("sample_count", 0)) >= 3 for item in classifications
        )
        per_run_three_sample_rates.append(
            complete / len(classifications) if classifications else 0.0
        )
        per_run_minimum_samples.append(
            min(
                (int(item.get("sample_count", 0)) for item in classifications),
                default=0,
            )
        )
    checks = {
        "runs_present": bool(selected),
        "target_fps_held": bool(selected)
        and all(
            float(run["summary"]["achieved_fps"]) >= max(0.0, target_fps - 1.0)
            for run in selected
        ),
        "zero_skipped_frames": all(
            int(run["summary"].get("frames_skipped", 0)) == 0
            for run in selected
        ),
        "zero_missed_frame_deadlines": all(
            int(run["summary"].get("missed_deadlines", 0)) == 0
            for run in selected
        ),
        "zero_crop_or_job_drops": all(
            int(run["summary"].get("crops_dropped", 0)) == 0
            and int(run["outcome"].get("jobs_dropped", 0)) == 0
            and int(run["outcome"].get("jobs_failed", 0)) == 0
            for run in selected
        ),
        "genuine_stereo_evidence": all(
            int(run["outcome"].get("stereo_pairs_complete", 0))
            == int(run["outcome"].get("classification_evidence", 0))
            == int(run["outcome"].get("jobs_completed", 0))
            and int(run["outcome"].get("stereo_pairs_incomplete", 0)) == 0
            and bool(
                run["summary"]
                .get("timings", {})
                .get("crop_selection", {})
                .get("stereo_enabled", False)
            )
            and int(
                run["outcome"]
                .get("stereo_pairing", {})
                .get("maximum_synchronization_delta_ns", 0)
            )
            <= 1_000_000
            for run in selected
        ),
        "zero_late_decisions": all(
            int(
                run["outcome"]["timing_ledger"]
                .get("results", {})
                .get("too_late", 0)
            )
            == 0
            for run in selected
        ),
        "zero_suspected_duplicate_ids": all(
            int(
                run["outcome"]
                .get("identity_continuity", {})
                .get("suspected_fragments", 0)
            )
            == 0
            for run in selected
        ),
        "stable_bean_count": len(set(bean_counts)) <= 1,
        "expected_bean_count": expected_beans is None
        or all(value == expected_beans for value in bean_counts),
        "minimum_evidence_met": bool(per_run_minimum_samples)
        and all(
            value >= minimum_samples_per_bean
            for value in per_run_minimum_samples
        ),
        "three_sample_rate_met": bool(per_run_three_sample_rates)
        and all(
            value >= minimum_three_sample_rate
            for value in per_run_three_sample_rates
        ),
        "clock_start_synchronized": all(
            bool(run["summary"].get("clock_synchronized", False))
            for run in selected
        ),
        "clock_propagation_consistent": all(
            bool(
                run["outcome"]
                .get("clock_consistency", {})
                .get("all_consistent", False)
            )
            for run in selected
        ),
        "outcomes_settled": all(
            bool(run["outcome"].get("settled", False)) for run in selected
        ),
    }
    return {
        "schema": "beanoflight-soak-acceptance/v1",
        "criteria": {
            "target_fps": target_fps,
            "minimum_three_sample_rate": minimum_three_sample_rate,
            "minimum_samples_per_bean": minimum_samples_per_bean,
            "expected_beans": expected_beans,
        },
        "measurements": {
            "runs": len(selected),
            "bean_counts": bean_counts,
            "three_sample_rates": per_run_three_sample_rates,
            "minimum_samples": per_run_minimum_samples,
            "clock_start_offsets_ms": [
                float(run["summary"].get("clock_start_offset_ms", 0.0))
                for run in selected
            ],
            "clock_anchor_misses": [
                int(run["summary"].get("clock_anchor_misses", 0))
                for run in selected
            ],
        },
        "checks": checks,
        "passed": all(checks.values()),
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
