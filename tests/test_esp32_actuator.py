import time
import unittest

from beanoflight.actuation_transport import ActuationPlan
from beanoflight.esp32_actuator import (
    GATE_GPIO_MAP,
    ESP32ProtocolError,
    decode_protocol_line,
    encode_protocol_line,
    gate_indices_to_mask,
    host_to_source_ns,
)
from beanoflight.models import BeanRef


class ESP32ActuatorTests(unittest.TestCase):
    def test_gpio_map_covers_21_gates_without_usb_or_status_pins(self):
        self.assertEqual(tuple(GATE_GPIO_MAP), tuple(range(-10, 11)))
        self.assertEqual(len(set(GATE_GPIO_MAP.values())), 21)
        self.assertTrue({15, 19, 20}.isdisjoint(GATE_GPIO_MAP.values()))

    def test_gate_mask_uses_gate_minus_ten_as_low_bit(self):
        self.assertEqual(gate_indices_to_mask((-10, 0, 10)), 0x100401)

    def test_crc_protocol_round_trip_and_corruption_detection(self):
        line = encode_protocol_line("SCHEDULE", 7, 3, "00000400", 1000, 2000)
        self.assertEqual(
            decode_protocol_line(line),
            ("SCHEDULE", "7", "3", "00000400", "1000", "2000"),
        )
        with self.assertRaises(ESP32ProtocolError):
            decode_protocol_line(line.replace("1000", "1001"))

    def test_host_timestamp_maps_back_to_replay_source_clock(self):
        anchor_ns = time.monotonic_ns()
        plan = ActuationPlan(
            "decision-1",
            BeanRef("clock-run", 1),
            (0,),
            anchor_ns + 10_000_000,
            anchor_ns + 20_000_000,
            anchor_ns + 15_000_000,
            10_000_000,
            20_000_000,
            15_000_000,
            0,
            anchor_ns,
            500_000_000,
        )
        self.assertEqual(host_to_source_ns(plan, anchor_ns + 8_000_000), 4_000_000)


if __name__ == "__main__":
    unittest.main()
