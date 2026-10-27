import copy
import json
import os
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
from test_calibration import write_pinkplane

from beanoflight.actuation_transport import (
    ZeroMQActuationPlanPublisher,
    ZeroMQActuationPlanReceiver,
    plan_from_dict,
    plan_to_dict,
)
from beanoflight.calibration import MetricPlaneCalibration
from beanoflight.esp32_actuator import ESP32ActuatorService, decode_protocol_line
from beanoflight.models import BeanRef, TrackSnapshot, TrackStatus
from beanoflight.nozzle_map import (
    NozzleMapError,
    load_nozzle_layout,
    resolve_gate_layout,
)
from beanoflight.prediction import TrajectoryPredictor
from beanoflight.registry import BeanRegistry
from beanoflight.registry_models import (
    Enrichment,
    GateWindow,
    RunSession,
    RunState,
    SortingDecision,
    decision_from_dict,
    decision_to_dict,
    prediction_from_dict,
    prediction_to_dict,
)
from beanoflight.sorter import (
    SorterService,
    SorterSettings,
    _advance_nozzle_windows,
    _external_actuation_plan,
    _pending_actuation,
    _select_gate_indices,
)


class MeasuredNozzleTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        path = self.root / "homography.json"
        write_pinkplane(path)
        geometry = json.loads(path.read_text())
        binding = {
            "intrinsic_sha256": "a" * 64,
            "logical_name": "CamL",
            "image_size_px": [400, 300],
        }
        geometry["metadata"] = {"intrinsic_bindings": {"CamL": binding}}
        path.write_text(json.dumps(geometry))
        self.calibration = MetricPlaneCalibration.from_pinkplane(
            path, image_size_px=(400, 300)
        )
        self.document = {
            "schema": "pinkplane-nozzle-map/v2",
            "measurement_camera": "CamL",
            "source": {
                "homography_sha256": self.calibration.source_sha256,
                "cameras": {
                    "CamL": {"intrinsics": binding, "image_size_px": [400, 300]}
                },
            },
            "settings": {"hole_pitch_mm": 9.16, "expected_count": 3},
            "coordinates": {
                "units": "mm",
                "tracking": {
                    "camera": "CamL",
                    "pixel_to_mm_matrix": self.calibration.pixel_to_mm_matrix.tolist(),
                },
            },
            "nozzles": [
                {
                    "nozzle_id": i,
                    "measurement_camera": "CamL",
                    "valve_channel": None,
                    "intersection_tracking_mm": [x, y],
                    "cameras": {
                        "CamL": {
                            "intersection_undistorted_px": list(
                                self.calibration.mm_to_pixel((x, y))
                            )
                        }
                    },
                }
                for i, (x, y) in enumerate(((-8.0, 55.0), (0.0, 60.0), (11.0, 65.0)), 1)
            ],
        }
        self.path = self.root / "nozzles.json"
        self.layout = self.load()
        self.track = TrackSnapshot(
            BeanRef("nozzle-run", 1),
            TrackStatus.CONFIRMED,
            1_000_000_000,
            (0.0, 0.0, 0.0, 900.0),
            tuple(map(tuple, np.diag([0.04, 0.04, 25.0, 25.0]))),
            3,
            0,
            (10, 10, 20, 20),
            (),
        )

    def load(self, document=None):
        self.path.write_text(
            json.dumps(self.document if document is None else document)
        )
        return load_nozzle_layout(self.path, self.calibration)

    def test_actual_centres_midpoint_zones_and_immutable_snapshot(self):
        gates = self.layout.gates
        np.testing.assert_allclose(
            [(g.left_mm, g.right_mm) for g in gates],
            [(-12.0, -4.0), (-4.0, 5.5), (5.5, 16.5)],
            atol=1e-9,
        )
        np.testing.assert_allclose(
            [g.line_y_mm for g in gates], [55.0, 60.0, 65.0], atol=1e-9
        )
        self.assertEqual([g.label for g in gates], ["N1", "N2", "N3"])
        self.path.write_text("invalid")
        self.assertEqual(self.layout.to_dict()["nozzle_map"], self.document)
        with self.assertRaises(NozzleMapError):
            resolve_gate_layout(self.calibration, self.path)
        self.assertFalse(resolve_gate_layout(self.calibration).measured_gates)

    def test_rejects_mismatched_geometry_intrinsics_dimensions_and_units(self):
        mutations = [
            lambda d: d["source"].update(homography_sha256="wrong"),
            lambda d: d["source"]["cameras"]["CamL"]["intrinsics"].update(
                intrinsic_sha256="wrong"
            ),
            lambda d: d["source"]["cameras"]["CamL"].update(image_size_px=[800, 600]),
            lambda d: d["settings"].update(hole_pitch_mm=10.0),
            lambda d: d["coordinates"].update(units="pixels"),
            lambda d: d["nozzles"][0].update(intersection_tracking_mm=[0.0, 0.0]),
            lambda d: d["nozzles"][0].update(valve_channel=21),
            lambda d: d["nozzles"][0].update(nozzle_id=2),
            lambda d: d["nozzles"].reverse(),
            lambda d: d["nozzles"][0]["cameras"]["CamL"].update(
                intersection_undistorted_px=[float("nan"), 2.0]
            ),
        ]
        for mutate in mutations:
            data = copy.deepcopy(self.document)
            mutate(data)
            with self.subTest(mutate=mutate), self.assertRaises(NozzleMapError):
                self.load(data)

    def test_per_nozzle_predictions_and_wire_round_trip(self):
        predictor = TrajectoryPredictor(
            self.layout, gravity_mm_s2=0.0, process_acceleration_sigma_mm_s2=0.0
        )
        prediction = predictor.predict(self.track)
        self.assertEqual(
            prediction_from_dict(prediction_to_dict(prediction)), prediction
        )
        for p in prediction.gates:
            self.assertAlmostEqual(p.seconds_until_crossing, p.gate.line_y_mm / 900.0)
            self.assertGreater(p.time_std_ms, 0.0)
        self.assertEqual(
            max(prediction.gates, key=lambda p: p.probability).gate.nozzle_id, 2
        )
        self.assertEqual(prediction.nozzle_map_sha256, self.layout.nozzle_map_sha256)
        partial = predictor.predict(replace(self.track, state=(0.0, 61.0, 0.0, 900.0)))
        self.assertEqual([p.probability for p in partial.gates[:2]], [0.0, 0.0])
        self.assertEqual(
            [p.crossing_timestamp_ns for p in partial.gates[:2]], [None, None]
        )
        self.assertIsNone(
            predictor.predict(replace(self.track, state=(0.0, 66.0, 0.0, 900.0)))
        )

    def test_adjacent_probabilities_not_added_at_different_heights(self):
        prediction = TrajectoryPredictor(self.layout).predict(self.track)
        probabilities = tuple(replace(p, probability=0.2) for p in prediction.gates)
        self.assertEqual(
            _select_gate_indices(probabilities, 0.35, allow_adjacent_pair=True),
            ((), None),
        )
        same_height = tuple(
            replace(p, gate=replace(p.gate, line_y_mm=60.0)) for p in probabilities
        )
        self.assertEqual(
            _select_gate_indices(same_height, 0.35, allow_adjacent_pair=True)[1], 0.4
        )

    def make_record(self, *, mapped=False):
        registry = BeanRegistry()
        anchor = time.monotonic_ns()
        session = RunSession(
            self.track.bean_ref.run_id,
            0,
            RunState.RUNNING,
            "test",
            "simulation",
            100,
            60.0,
            60.0,
            self.track.timestamp_ns,
            self.track.timestamp_ns,
            anchor,
            False,
            1,
            1,
            {"nozzle_layout": self.layout.to_dict()},
        )
        registry.put_session(session)
        prediction = TrajectoryPredictor(self.layout).predict(self.track)
        if mapped:
            prediction = replace(
                prediction,
                gates=tuple(
                    replace(p, gate=replace(p.gate, valve_channel=i))
                    for i, p in enumerate(prediction.gates)
                ),
            )
        record = registry.update_track(self.track, prediction)
        record = registry.add_enrichment(
            self.track.bean_ref,
            Enrichment(
                "test",
                "classification",
                {"category": "mould"},
                self.track.timestamp_ns,
                confidence=0.99,
            ),
        )
        return registry, session, record, anchor

    def test_sorter_builds_windows_and_blocks_unmapped_hardware(self):
        for external in (False, True):
            registry, _session, record, anchor = self.make_record()
            service = SorterService(
                actuation_endpoint="ipc:///unused-nozzle-test" if external else "",
                settings=SorterSettings(gate_probability_threshold=0.01),
            )
            with (
                patch.object(service, "_schedule") as schedule,
                patch.object(service, "_queue_audit") as audit,
            ):
                service._consider(record, registry, arrival_monotonic_ns=anchor)
                decision = audit.call_args.args[0].record.decision
            if external:
                self.assertEqual(decision.gate_indices, ())
                self.assertIn("mapping", decision.reason)
                schedule.assert_not_called()
            else:
                self.assertTrue(decision.gate_windows)
                schedule.assert_called_once()
                self.assertEqual(
                    decision_from_dict(decision_to_dict(decision)), decision
                )
                for w in decision.gate_windows:
                    p = next(
                        p
                        for p in record.prediction.gates
                        if p.gate.index == w.gate_index
                    )
                    self.assertEqual(w.crossing_timestamp_ns, p.crossing_timestamp_ns)

    def timed_pending(self):
        _registry, session, record, anchor = self.make_record(mapped=True)
        base = self.track.timestamp_ns
        windows = (
            GateWindow(1, base + 20_000_000, base + 15_000_000, base + 25_000_000, 0),
            GateWindow(3, base + 40_000_000, base + 35_000_000, base + 45_000_000, 20),
        )
        decision = SortingDecision(
            "measured-1",
            "test",
            base,
            windows[0].open_timestamp_ns,
            (1, 3),
            close_timestamp_ns=windows[-1].close_timestamp_ns,
            crossing_timestamp_ns=windows[0].crossing_timestamp_ns,
            gate_windows=windows,
            nozzle_map_sha256=self.layout.nozzle_map_sha256,
        )
        record = replace(record, decision=decision)
        return _pending_actuation(record, session, anchor), anchor

    def test_virtual_scheduler_uses_individual_windows_and_aggregates_audit(self):
        pending, anchor = self.timed_pending()
        for milliseconds, expected in (
            (15, [(1, True)]),
            (25, [(1, False)]),
            (35, [(3, True)]),
            (45, [(3, False)]),
        ):
            pending, changes, result, _ = _advance_nozzle_windows(
                pending, anchor + milliseconds * 1_000_000
            )
            self.assertEqual(changes, expected)
            if milliseconds < 45:
                self.assertIsNone(result)
        self.assertTrue(result.success)
        self.assertIn("N3", result.detail)

    def test_external_plan_maps_explicit_channels_and_keeps_separate_times(self):
        pending, _ = self.timed_pending()
        plan = _external_actuation_plan(pending)
        self.assertEqual(plan.gate_indices, (-10, 10))
        self.assertEqual(plan_from_dict(plan_to_dict(plan)), plan)
        self.assertEqual(len(plan.pulse_plans()), 2)
        self.assertNotEqual(
            plan.pulses[0].crossing_source_ns, plan.pulses[1].crossing_source_ns
        )
        bad = replace(
            pending.record.decision,
            gate_windows=(
                replace(pending.record.decision.gate_windows[0], valve_channel=None),
            ),
        )
        with self.assertRaisesRegex(ValueError, "explicit"):
            _external_actuation_plan(
                replace(pending, record=replace(pending.record, decision=bad))
            )
        bad = replace(pending.record.decision, gate_windows=())
        with self.assertRaisesRegex(ValueError, "per-nozzle windows"):
            _external_actuation_plan(
                replace(pending, record=replace(pending.record, decision=bad))
            )

    def test_live_and_replay_sessions_snapshot_the_same_layout(self):
        from test_replay import (
            FakeEngine,
            FakeLiveSource,
            FakeRegistry,
            FakeSequentialSource,
        )

        from beanoflight.replay import ReplayRunner, ReplaySettings

        for source_type in (FakeSequentialSource, FakeLiveSource):
            source = source_type(frame_count=2)
            engine = FakeEngine()
            engine.gate_layout = self.layout
            registry = FakeRegistry(source)
            ReplayRunner(
                source,
                engine,
                registry,
                settings=ReplaySettings(
                    target_fps=60, prebuffer_frames=0, maximum_frames=2
                ),
            ).run()
            for session in registry.sessions:
                self.assertEqual(
                    session.settings["nozzle_layout"], self.layout.to_dict()
                )

    def test_actuator_emits_separate_firmware_commands_and_one_parent_result(self):
        pending, anchor = self.timed_pending()
        plan = _external_actuation_plan(pending)
        service = ESP32ActuatorService()
        service.connected = service.synchronized = True
        service._clock_offset_ns = anchor

        class Serial:
            def __init__(self):
                self.lines = []

            def write(self, line):
                self.lines.append(decode_protocol_line(line))

        serial = Serial()
        with patch("beanoflight.esp32_actuator.time.monotonic_ns", return_value=anchor):
            self.assertTrue(service._accept_plan(plan)[0])
            self.assertEqual(service._send_queued_plans(serial), 2)
        self.assertEqual([line[3] for line in serial.lines], ["00000001", "00100000"])
        for sequence, p in list(service._pending.items()):
            service._pending[sequence] = replace(p, opened_board_us=p.open_board_us)
            service._complete_plan(sequence, p.close_board_us)
        self.assertEqual(service._results.qsize(), 1)
        self.assertTrue(service._results.get_nowait().result.success)

    def test_measured_plan_transport_round_trip(self):
        pending, _ = self.timed_pending()
        plan = _external_actuation_plan(pending)
        receiver = ZeroMQActuationPlanReceiver(f"ipc://{self.root / 'pulses.sock'}")
        publisher = ZeroMQActuationPlanPublisher(receiver.endpoint, timeout_ms=100)
        received = []
        worker = threading.Thread(
            target=lambda: received.append(
                receiver.receive(timeout_ms=1000, accept=lambda p: (True, "test only"))
            )
        )
        try:
            worker.start()
            self.assertTrue(publisher.submit(plan).accepted)
            worker.join(2)
            self.assertEqual(received, [plan])
        finally:
            publisher.close()
            receiver.close()

    def test_failed_multi_pulse_link_produces_one_failed_parent_audit(self):
        pending, anchor = self.timed_pending()
        plan = _external_actuation_plan(pending)
        service = ESP32ActuatorService()
        service.connected = service.synchronized = True
        service._clock_offset_ns = anchor
        serial = SimpleNamespace(
            write=lambda line: (_ for _ in ()).throw(OSError("test link failure"))
        )
        with patch("beanoflight.esp32_actuator.time.monotonic_ns", return_value=anchor):
            self.assertTrue(service._accept_plan(plan)[0])
            with self.assertRaises(OSError):
                service._send_queued_plans(serial)
            service._fail_pending("test link failure")
        self.assertEqual(service._results.qsize(), 1)
        self.assertFalse(service._results.get_nowait().result.success)
        self.assertEqual(service._pulse_results, {})

    @unittest.skipUnless(os.environ.get("DISPLAY"), "Tk display unavailable")
    def test_gui_load_reject_and_explicit_virtual_fallback(self):
        from beanoflight.app import BeanoFlightApp

        app = BeanoFlightApp(homography_path=self.calibration.source_path)
        app.withdraw()
        app.source = SimpleNamespace(
            path=self.root,
            metadata=SimpleNamespace(width=400, height=300),
            close=lambda: None,
        )
        try:
            app._load_calibration(self.calibration.source_path)
            with (
                patch(
                    "beanoflight.app.filedialog.askopenfilename",
                    return_value=str(self.path),
                ),
                patch.object(app, "_refresh_display"),
            ):
                app.select_nozzle_map()
            self.assertEqual(app.gate_layout, self.layout)
            self.assertIn("Measured CamL", app.layout_var.get())
            self.assertIn("measured nozzle y=55.00–65.00", app.calibration_var.get())
            self.path.write_text("invalid")
            with (
                patch(
                    "beanoflight.app.filedialog.askopenfilename",
                    return_value=str(self.path),
                ),
                patch("beanoflight.app.messagebox.showerror") as error,
            ):
                app.select_nozzle_map()
            error.assert_called_once()
            self.assertIsNotNone(app.gate_layout.nozzle_map_sha256)
            app.use_virtual_layout()
            self.assertFalse(app.gate_layout.measured_gates)
            self.assertIn("Virtual layout", app.layout_var.get())
            app.update_idletasks()
        finally:
            for callback in app.tk.call("after", "info"):
                app.after_cancel(callback)
            app._close()


if __name__ == "__main__":
    unittest.main()
