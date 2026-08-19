import tempfile
import time
import unittest
from pathlib import Path

import zmq
from test_registry import track

from beanoflight.models import BeanRef
from beanoflight.prediction import GateLayout, TrajectoryPredictor
from beanoflight.sorting_context_transport import (
    SortingContext,
    SortingContextTransportError,
    ZeroMQSortingContextPublisher,
    ZeroMQSortingContextReceiver,
)


class SortingContextTransportTests(unittest.TestCase):
    def test_track_prediction_and_clock_round_trip(self):
        with tempfile.TemporaryDirectory() as temporary:
            endpoint = f"ipc://{Path(temporary) / 'contexts.sock'}"
            receiver = ZeroMQSortingContextReceiver(endpoint)
            publisher = ZeroMQSortingContextPublisher(endpoint)
            bean_ref = BeanRef("context-run", 1)
            snapshot = track(bean_ref, 3, 100, -25.0)
            prediction = TrajectoryPredictor(GateLayout(60.0)).predict(snapshot)
            clock_ns = time.monotonic_ns()

            self.assertTrue(
                publisher.send_batch(
                    run_id="context-run",
                    frame_index=3,
                    source_fps=60.0,
                    target_fps=60.0,
                    clock_source_timestamp_ns=100,
                    clock_monotonic_ns=clock_ns,
                    items=(SortingContext(snapshot, prediction),),
                )
            )
            self.assertTrue(receiver.socket.poll(1_000, zmq.POLLIN))
            received = receiver.receive_batch()

            self.assertEqual(received.run_id, "context-run")
            self.assertEqual(received.frame_index, 3)
            self.assertEqual(received.clock_monotonic_ns, clock_ns)
            self.assertEqual(received.items[0].track.bean_ref, bean_ref)
            self.assertEqual(received.items[0].track.state, snapshot.state)
            self.assertEqual(received.items[0].prediction, prediction)
            self.assertEqual(publisher.statistics()["contexts_sent"], 1)
            publisher.close()
            receiver.close()

    def test_prediction_must_match_its_track(self):
        with tempfile.TemporaryDirectory() as temporary:
            endpoint = f"ipc://{Path(temporary) / 'contexts.sock'}"
            publisher = ZeroMQSortingContextPublisher(endpoint)
            snapshot = track(BeanRef("context-run", 1), 3, 100, -25.0)
            other = track(BeanRef("context-run", 2), 3, 100, -25.0)
            prediction = TrajectoryPredictor(GateLayout(60.0)).predict(other)

            with self.assertRaises(SortingContextTransportError):
                publisher.send_batch(
                    run_id="context-run",
                    frame_index=3,
                    source_fps=60.0,
                    target_fps=60.0,
                    clock_source_timestamp_ns=100,
                    clock_monotonic_ns=time.monotonic_ns(),
                    items=(SortingContext(snapshot, prediction),),
                )
            publisher.close()


if __name__ == "__main__":
    unittest.main()
