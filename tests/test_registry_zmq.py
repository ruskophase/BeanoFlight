import tempfile
import threading
import time
import unittest
from pathlib import Path

try:
    import zmq
except ImportError:  # The package dependency is installed in production and CI.
    zmq = None

from test_registry import track

from beanoflight.models import BeanRef
from beanoflight.registry import BeanRegistry
from beanoflight.registry_models import Enrichment, SortingDecision
from beanoflight.registry_sqlite import SQLiteBeanRepository


@unittest.skipIf(zmq is None, "pyzmq is not installed in this interpreter")
class ZeroMQRegistryTests(unittest.TestCase):
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

            batched = client.update_tracks(
                (
                    (
                        track(bean_ref, 1, 116_666_667, -10.0),
                        None,
                        "track-1",
                    ),
                )
            )[0]
            self.assertEqual(batched.revision, 2)

            enriched = client.add_enrichment(
                bean_ref,
                Enrichment(
                    "resnet", "defect", "clear", 120, "model-v1", "result-1", 0.97
                ),
            )
            self.assertEqual(enriched.revision, 3)
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
            self.assertEqual(decided.revision, 4)
            self.assertEqual(acknowledged.revision, 5)
            self.assertEqual(client.list_active(run_id="zmq-run"), (acknowledged,))
            journal = client.events_since(0)
            self.assertEqual(
                [event.kind for event in journal],
                [
                    "bean.created",
                    "track.updated",
                    "enrichment.added",
                    "sorting.decision",
                    "sorting.acknowledged",
                ],
            )
            self.assertEqual(
                [event.stream_sequence for event in journal], [1, 2, 3, 4, 5]
            )
            self.assertEqual(client.event_cursor(), 5)
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
