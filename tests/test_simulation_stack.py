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


class SimulationStackTests(unittest.TestCase):
    def test_crop_to_inference_to_decision_to_virtual_actuation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            commands = f"ipc://{root / 'commands.sock'}"
            events = f"ipc://{root / 'events.sock'}"
            crops = f"ipc://{root / 'crops.sock'}"
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

            inferencer = MockInferencerService(
                registry_endpoint=commands,
                crop_endpoint=crops,
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
            sorter = SorterService(
                registry_endpoint=commands,
                settings=SorterSettings(
                    reject_categories=("mould",),
                    minimum_confidence=0.9,
                    gate_probability_threshold=0.05,
                    open_lead_ms=8,
                    close_lag_ms=12,
                ),
            )
            sorter.start()
            client = ZeroMQRegistryClient(commands, timeout_ms=2_000)
            run_id = "stack-run"
            client.put_session(
                RunSession(
                    run_id,
                    0,
                    RunState.RUNNING,
                    "/synthetic",
                    "mkv",
                    3,
                    60,
                    0,
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
            self.assertTrue(result.actuation.success)
            self.assertEqual(result.enrichments[-1].value["category"], "mould")
            self.assertTrue(result.decision.gate_indices)

            dispatcher.close()
            inferencer.close()
            sorter.close()
            client.close()
            stop.set()
            server_thread.join(2.0)
            repository.close()
            self.assertFalse(server_thread.is_alive())


if __name__ == "__main__":
    unittest.main()
