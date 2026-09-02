import tempfile
import threading
import time
import unittest
from pathlib import Path

from beanoflight.actuation_transport import (
    ActuationPlan,
    ActuationTransportError,
    ZeroMQActuationPlanPublisher,
    ZeroMQActuationPlanReceiver,
    plan_from_dict,
    plan_to_dict,
)
from beanoflight.models import BeanRef


def actuation_plan() -> ActuationPlan:
    now_ns = time.monotonic_ns()
    return ActuationPlan(
        decision_id="decision-1",
        bean_ref=BeanRef("actuation-run", 1),
        gate_indices=(-1, 0),
        open_monotonic_ns=now_ns + 20_000_000,
        crossing_monotonic_ns=now_ns + 25_000_000,
        close_monotonic_ns=now_ns + 35_000_000,
        open_source_ns=20_000_000,
        crossing_source_ns=25_000_000,
        close_source_ns=35_000_000,
        run_clock_source_ns=0,
        run_clock_monotonic_ns=now_ns,
        run_clock_scale_ppb=1_000_000_000,
    )


class ActuationTransportTests(unittest.TestCase):
    def test_approved_plan_round_trips_with_admission_acknowledgement(self):
        with tempfile.TemporaryDirectory() as temporary:
            endpoint = f"ipc://{Path(temporary) / 'plans.sock'}"
            receiver = ZeroMQActuationPlanReceiver(endpoint)
            publisher = ZeroMQActuationPlanPublisher(endpoint, timeout_ms=100)
            received = []
            plan = actuation_plan()
            thread = threading.Thread(
                target=lambda: received.append(
                    receiver.receive(
                        timeout_ms=1_000,
                        accept=lambda plan: (True, "queued"),
                    )
                ),
                daemon=True,
            )
            thread.start()

            receipt = publisher.submit(plan)

            thread.join(1.0)
            self.assertFalse(thread.is_alive())
            self.assertTrue(receipt.accepted)
            self.assertEqual(receipt.detail, "queued")
            self.assertEqual(received, [plan])
            publisher.close()
            receiver.close()

    def test_plan_serialization_preserves_both_clock_domains(self):
        plan = actuation_plan()
        self.assertEqual(plan_from_dict(plan_to_dict(plan)), plan)

    def test_invalid_gate_is_rejected_before_transport(self):
        plan = actuation_plan()
        invalid = ActuationPlan(
            **{**plan_to_dict(plan), "bean_ref": plan.bean_ref, "gate_indices": (11,)}
        )
        with self.assertRaises(ActuationTransportError):
            invalid.validate()


if __name__ == "__main__":
    unittest.main()
