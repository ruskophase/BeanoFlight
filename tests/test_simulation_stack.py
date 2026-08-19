import tempfile
import threading
import time
import unittest
from pathlib import Path

import numpy as np
from test_registry import track

from beanoflight.crop import CropPayload
from beanoflight.mock_inference import MockInferencerService, MockInferenceSettings
from beanoflight.models import BeanRef
from beanoflight.prediction import GateLayout, TrajectoryPredictor
from beanoflight.registry import BeanRegistry
from beanoflight.registry_models import (
    InferenceJob,
    InferenceStatus,
    RunSession,
    RunState,
)
from beanoflight.registry_sqlite import SQLiteBeanRepository
from beanoflight.registry_zmq import ZeroMQRegistryClient, ZeroMQRegistryServer
from beanoflight.replay import CropDispatcher
from beanoflight.sorter import SorterService, SorterSettings
from beanoflight.sorting_context_transport import (
    SortingContext,
    ZeroMQSortingContextPublisher,
)


class SimulationStackTests(unittest.TestCase):
    def test_crop_to_inference_to_decision_to_virtual_actuation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            commands = f"ipc://{root / 'commands.sock'}"
            events = f"ipc://{root / 'events.sock'}"
            crops = f"ipc://{root / 'crops.sock'}"
            classifications = f"ipc://{root / 'classifications.sock'}"
            sorting_contexts = f"ipc://{root / 'sorting-contexts.sock'}"
            repository = SQLiteBeanRepository(root / "registry.db")
            registry = BeanRegistry(repository)
            server = ZeroMQRegistryServer(
                registry, command_endpoint=commands, event_endpoint=events
            )
            stop = threading.Event()
            ready = threading.Event()
            server_thread = threading.Thread(
                target=server.serve_forever,
                args=(stop,),
                kwargs={"ready": ready},
                daemon=True,
            )
            server_thread.start()
            self.assertTrue(ready.wait(2.0))

            sorter = SorterService(
                registry_endpoint=commands,
                event_endpoint=events,
                classification_endpoint=classifications,
                sorting_context_endpoint=sorting_contexts,
                settings=SorterSettings(
                    reject_categories=("mould",),
                    minimum_confidence=0.9,
                    gate_probability_threshold=0.05,
                    open_lead_ms=8,
                    close_lag_ms=12,
                ),
            )
            sorter.start()
            self.assertTrue(sorter.ready.wait(2.0))
            self.assertFalse(sorter.startup_error)
            inferencer = MockInferencerService(
                registry_endpoint=commands,
                crop_endpoint=crops,
                classification_endpoint=classifications,
                settings=MockInferenceSettings(
                    latency_ms=0,
                    jitter_ms=0,
                    categories=("mould",),
                    weights=(1.0,),
                    confidence_min=0.95,
                    confidence_max=0.95,
                ),
            )
            inferencer.start()
            self.assertTrue(inferencer.ready.wait(2.0))
            # Allow the non-blocking PUSH/PULL connection to finish its local
            # IPC handshake before the synthetic zero-latency inference.
            time.sleep(0.1)
            client = ZeroMQRegistryClient(commands, timeout_ms=2_000)
            run_id = "stack-run"
            session = client.put_session(
                RunSession(
                    run_id,
                    0,
                    RunState.RUNNING,
                    "/synthetic",
                    "mkv",
                    3,
                    60,
                    60,
                    100,
                    100,
                    time.monotonic_ns(),
                    False,
                    time.time_ns(),
                    time.time_ns(),
                    {},
                )
            )
            bean_ref = BeanRef(run_id, 1)
            snapshot = track(bean_ref, 0, 100, -25.0)
            prediction = TrajectoryPredictor(GateLayout(60.0)).predict(snapshot)
            current = client.update_track(snapshot, prediction, event_id="track")
            context_publisher = ZeroMQSortingContextPublisher(sorting_contexts)
            self.assertTrue(
                context_publisher.send_batch(
                    run_id=run_id,
                    frame_index=0,
                    source_fps=session.source_fps,
                    target_fps=session.target_fps,
                    clock_source_timestamp_ns=session.clock_source_timestamp_ns,
                    clock_monotonic_ns=session.clock_monotonic_ns,
                    items=(SortingContext(snapshot, prediction),),
                )
            )
            job = InferenceJob(
                "job-1",
                bean_ref,
                InferenceStatus.SUBMITTED,
                "CamL",
                0,
                100,
                current.revision,
                300,
                300,
                False,
                100,
                100,
            )
            dispatcher = CropDispatcher(commands, crops, timeout_ms=2_000)
            dispatcher.start()
            self.assertTrue(
                dispatcher.register_and_enqueue(
                    CropPayload(job, np.zeros((300, 300, 3), dtype=np.uint8)),
                    client,
                )
            )

            deadline = time.monotonic() + 5.0
            result = None
            while time.monotonic() < deadline:
                result = client.get(bean_ref)
                if result.actuation is not None:
                    break
                time.sleep(0.02)
            self.assertIsNotNone(result.actuation)
            self.assertTrue(result.actuation.success, result.actuation.detail)
            self.assertEqual(result.enrichments[-1].value["category"], "mould")
            self.assertTrue(result.decision.gate_indices)
            self.assertGreater(sorter.event_notifications, 0)
            self.assertGreater(sorter.direct_evidence_received, 0)
            self.assertGreater(sorter.context_cache_hits, 0)
            self.assertEqual(sorter.direct_registry_reads, 0)
            self.assertIn(
                "registry_classification_received_monotonic_ns",
                result.inference_jobs[0].timing_marks_ns,
            )
            self.assertIn(
                "sorter_event_received_monotonic_ns",
                result.decision.timing_marks_ns,
            )
            self.assertEqual(
                result.decision.timing_marks_ns["classification_direct_path"],
                1,
                (
                    sorter.direct_evidence_received,
                    sorter.registry_recovery_decisions,
                    sorter.event_notifications,
                    result.decision.timing_marks_ns,
                ),
            )
            self.assertEqual(
                result.decision.timing_marks_ns["sorting_context_direct_path"],
                1,
            )
            self.assertGreater(
                result.decision.timing_marks_ns[
                    "sorter_direct_received_monotonic_ns"
                ],
                result.decision.timing_marks_ns[
                    "direct_result_send_monotonic_ns"
                ],
            )

            dispatcher.close()
            context_publisher.close()
            inferencer.close()
            sorter.close()
            client.close()
            stop.set()
            server_thread.join(2.0)
            repository.close()
            self.assertFalse(server_thread.is_alive())


if __name__ == "__main__":
    unittest.main()
