import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

try:
    import zmq
except ImportError:  # The package dependency is installed in production and CI.
    zmq = None

from test_registry import track

from beanoflight.models import BeanRef
from beanoflight.registry import BeanRegistry
from beanoflight.registry_models import (
    ActuationResult,
    Enrichment,
    InferenceJob,
    InferenceStatus,
    SortingDecision,
)
from beanoflight.registry_sqlite import SQLiteBeanRepository


@unittest.skipIf(zmq is None, "pyzmq is not installed in this interpreter")
class ZeroMQRegistryTests(unittest.TestCase):
    def test_compact_actuation_ack_falls_back_to_legacy_operation(self):
        from beanoflight.registry_zmq import (
            RegistryRemoteError,
            ZeroMQRegistryClient,
        )

        client = ZeroMQRegistryClient("inproc://actuation-compatibility")
        bean_ref = BeanRef("compatibility-run", 1)
        result = ActuationResult("decision-1", "actuator", 100, 120, True)
        try:
            with (
                patch.object(
                    client,
                    "_request",
                    side_effect=RegistryRemoteError(
                        "ValueError",
                        "unknown registry operation: record_actuation_ack",
                    ),
                ),
                patch.object(
                    client,
                    "record_actuation",
                    return_value=SimpleNamespace(revision=7),
                ) as legacy,
            ):
                self.assertEqual(client.record_actuation_ack(bean_ref, result), 7)
            legacy.assert_called_once()
        finally:
            client.close()

    def test_frame_events_share_one_transport_envelope(self):
        from beanoflight.registry_zmq import (
            ZeroMQRegistryClient,
            ZeroMQRegistryServer,
            ZeroMQRegistrySubscriber,
        )

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            command_endpoint = f"ipc://{root}/commands.sock"
            event_endpoint = f"ipc://{root}/events.sock"
            registry = BeanRegistry()
            server = ZeroMQRegistryServer(
                registry,
                command_endpoint=command_endpoint,
                event_endpoint=event_endpoint,
            )
            stop = threading.Event()
            ready = threading.Event()
            worker = threading.Thread(
                target=server.serve_forever,
                args=(stop,),
                kwargs={"ready": ready},
                daemon=True,
            )
            worker.start()
            self.assertTrue(ready.wait(2.0))
            client = ZeroMQRegistryClient(command_endpoint, timeout_ms=2_000)
            subscriber = ZeroMQRegistrySubscriber(event_endpoint)
            time.sleep(0.1)

            refs = (BeanRef("event-batch", 1), BeanRef("event-batch", 2))
            client.update_tracks(
                tuple(
                    (track(bean_ref, 0, 100, -25.0), None, f"track-{index}")
                    for index, bean_ref in enumerate(refs)
                )
            )
            events = subscriber.receive_many(timeout_ms=2_000)

            self.assertEqual(tuple(event.bean_ref for event in events), refs)
            self.assertEqual(
                tuple(event.stream_sequence for event in events), (1, 2)
            )
            client.close()
            subscriber.close()
            stop.set()
            worker.join(2.0)
            self.assertFalse(worker.is_alive())

    def test_acknowledged_commands_queries_and_event_fanout(self):
        from beanoflight.registry_zmq import (
            RegistryRemoteError,
            ZeroMQRegistryClient,
            ZeroMQRegistryServer,
            ZeroMQRegistrySubscriber,
        )

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = SQLiteBeanRepository(root / "beanoflight.db")
            registry = BeanRegistry(repository)
            command_endpoint = f"ipc://{root}/commands.sock"
            event_endpoint = f"ipc://{root}/events.sock"
            server = ZeroMQRegistryServer(
                registry,
                command_endpoint=command_endpoint,
                event_endpoint=event_endpoint,
            )
            stop = threading.Event()
            ready = threading.Event()
            worker = threading.Thread(
                target=server.serve_forever,
                args=(stop,),
                kwargs={"ready": ready},
                daemon=True,
            )
            worker.start()
            self.assertTrue(ready.wait(2.0))
            client = ZeroMQRegistryClient(command_endpoint, timeout_ms=2_000)
            subscriber = ZeroMQRegistrySubscriber(event_endpoint)
            ping = client.ping()
            self.assertEqual(ping["service"], "BeanRegistry")
            self.assertGreaterEqual(ping["api_version"], 2)
            self.assertIn("complete_inference_jobs_ack", ping["capabilities"])
            self.assertIn("record_actuation_ack", ping["capabilities"])
            self.assertEqual(ping["database"], str(repository.path.resolve()))
            # PUB/SUB subscriptions are asynchronous; allow the local handshake.
            time.sleep(0.1)
            bean_ref = BeanRef("zmq-run", 9)
            created = client.update_track(
                track(bean_ref, 0, 100, -25.0), event_id="track-0"
            )
            event = subscriber.receive(timeout_ms=2_000)
            self.assertIsNotNone(event)
            self.assertEqual(event.kind, "bean.created")
            self.assertEqual(event.revision, 1)
            queried = client.get(bean_ref)
            self.assertEqual(created.revision, queried.revision)
            self.assertEqual(created.track.state, queried.track.state)
            self.assertEqual(len(created.track.history), 0)
            self.assertEqual(len(queried.track.history), 1)
            self.assertEqual(
                client.get_many((bean_ref,), include_history=False),
                (created,),
            )

            revisions = client.update_track_revisions(
                (
                    (
                        track(bean_ref, 1, 116_666_667, -10.0),
                        None,
                        "track-1",
                    ),
                )
            )
            self.assertEqual(revisions, {bean_ref: 2})

            job = InferenceJob(
                "job-1",
                bean_ref,
                InferenceStatus.SUBMITTED,
                "CamL",
                1,
                116_666_667,
                2,
                300,
                300,
                False,
                116_666_667,
                116_666_667,
            )
            self.assertEqual(client.submit_inference_job_revision(job), 3)

            enriched = client.add_enrichment(
                bean_ref,
                Enrichment(
                    "resnet", "defect", "clear", 120, "model-v1", "result-1", 0.97
                ),
            )
            self.assertEqual(enriched.revision, 4)
            decision = SortingDecision(
                "decision-1",
                "sorter",
                125_000_000,
                180_000_000,
                (0,),
                "policy-v1",
            )
            decided = client.set_sorting_decision(bean_ref, decision)
            acknowledged = client.acknowledge_sorting_decision(
                bean_ref, decision.decision_id, 181_000_000
            )
            self.assertEqual(decided.revision, 5)
            self.assertEqual(acknowledged.revision, 6)
            self.assertEqual(client.list_active(run_id="zmq-run"), (acknowledged,))
            journal = client.events_since(0)
            self.assertEqual(
                [event.kind for event in journal],
                [
                    "bean.created",
                    "track.updated",
                    "inference.submitted",
                    "enrichment.added",
                    "sorting.decision",
                    "sorting.acknowledged",
                ],
            )
            self.assertEqual(
                [event.stream_sequence for event in journal], [1, 2, 3, 4, 5, 6]
            )
            self.assertEqual(client.event_cursor(), 6)
            actuation_revision = client.record_actuation_ack(
                bean_ref,
                ActuationResult(
                    decision.decision_id,
                    "esp32-s2-gptimer",
                    179_000_000,
                    191_000_000,
                    True,
                ),
            )
            self.assertEqual(actuation_revision, 7)
            self.assertTrue(client.get(bean_ref).actuation.success)
            self.assertEqual(client.event_cursor(), 7)
            metrics = client.service_metrics()
            self.assertGreater(
                metrics["operations_ms"]["update_track_revisions"]["count"], 0
            )
            self.assertEqual(metrics["hot_state"]["records"], 1)
            without_history = client.get(bean_ref, include_history=False)
            self.assertEqual(without_history.track.history, ())
            with self.assertRaises(RegistryRemoteError):
                client.get(BeanRef("zmq-run", 404))

            client.close()
            subscriber.close()
            stop.set()
            worker.join(2.0)
            self.assertFalse(worker.is_alive())
            repository.close()


if __name__ == "__main__":
    unittest.main()
