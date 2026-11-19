import unittest
from unittest.mock import call, patch

from beanoflight.runtime_priority import (
    apply_latency_thread_profile,
    apply_performance_affinity,
    lower_current_thread_priority,
    performance_cpu_set,
)


class RuntimePriorityTests(unittest.TestCase):
    def test_six_cpu_profile_reserves_two_latency_cores(self):
        available = range(6)
        self.assertEqual(performance_cpu_set("general", available), {0, 1, 2, 3})
        self.assertEqual(performance_cpu_set("sorter", available), {4})
        self.assertEqual(performance_cpu_set("actuator", available), {5})

    def test_small_machine_keeps_its_existing_affinity(self):
        self.assertEqual(performance_cpu_set("actuator", {2, 3}), {2, 3})

    def test_performance_affinity_updates_every_existing_process_thread(self):
        with (
            patch(
                "beanoflight.runtime_priority.os.sched_getaffinity",
                return_value=set(range(6)),
            ),
            patch(
                "beanoflight.runtime_priority._process_task_ids",
                return_value=(100, 101, 102),
            ),
            patch(
                "beanoflight.runtime_priority.os.sched_setaffinity"
            ) as setaffinity,
        ):
            self.assertEqual(
                apply_performance_affinity("sorter", pid=100),
                {4},
            )

        self.assertEqual(
            setaffinity.call_args_list,
            [
                call(100, frozenset({4})),
                call(101, frozenset({4})),
                call(102, frozenset({4})),
            ],
        )

    def test_latency_profile_returns_previous_switch_interval(self):
        previous = apply_latency_thread_profile(switch_interval_ms=2.0)
        try:
            self.assertGreater(previous, 0)
        finally:
            apply_latency_thread_profile(switch_interval_ms=previous * 1_000)

    def test_audit_thread_priority_is_lowered_without_privilege(self):
        with (
            patch("beanoflight.runtime_priority.threading.get_native_id", return_value=7),
            patch("beanoflight.runtime_priority.os.getpriority", return_value=3),
            patch("beanoflight.runtime_priority.os.setpriority") as setpriority,
        ):
            self.assertTrue(lower_current_thread_priority(increment=10))

        self.assertEqual(setpriority.call_count, 1)
        self.assertEqual(setpriority.call_args.args[1:], (7, 13))


if __name__ == "__main__":
    unittest.main()
