import time
import unittest
from unittest.mock import Mock

from beanoflight.actuation_transport import ActuationPlan
from beanoflight.esp32_actuator import (
    GATE_GPIO_MAP,
    FIRMWARE_PROTOCOL,
    ESP32ActuatorService,
    ESP32ProtocolError,
    decode_protocol_line,
    encode_protocol_line,
    gate_indices_to_mask,
    host_to_source_ns,
)
from beanoflight.models import BeanRef


class ESP32ActuatorTests(unittest.TestCase):
    def test_manual_test_waits_for_board_ack_and_blocks_overlaps(self):
        service = ESP32ActuatorService()
        service.connected = service.synchronized = True
        request = service._queue_control("TEST")
        self.assertFalse(request.done.is_set())
        self.assertFalse(service.request_led_test())
        self.assertFalse(service._accept_plan(self._future_plan((0,)))[0])
        serial = Mock()
        service._send_control_requests(serial)
        fields = decode_protocol_line(serial.write.call_args.args[0])
        self.assertEqual(fields, ("TEST", "1"))
        self.assertFalse(request.done.is_set())
        service._handle_line(encode_protocol_line("ACK", 1, "TEST", 0, 100), 100000)
        self.assertTrue(request.done.is_set())
        self.assertFalse(request.error)
        self.assertTrue(service.control_status()["testing"])
        service._handle_line(
            encode_protocol_line("PONG", 2, 1100, 1000, FIRMWARE_PROTOCOL, "boot", "00000000", 0),
            1200000,
        )
        self.assertFalse(service.control_status()["testing"])

    def test_firmware_busy_is_reported_to_test_caller(self):
        service = ESP32ActuatorService()
        service.connected = service.synchronized = True
        request = service._queue_control("TEST")
        service._send_control_requests(Mock())
        service._handle_line(encode_protocol_line("ERR", 1, "BUSY", 100), 100000)
        self.assertTrue(request.done.is_set())
        self.assertIn("BUSY", request.error)

    def test_expired_test_is_not_sent_later(self):
        service = ESP32ActuatorService()
        service.connected = service.synchronized = True
        request = service._queue_control("TEST")
        request.deadline_ns = 0
        serial = Mock()
        service._send_control_requests(serial)
        serial.write.assert_not_called()
        self.assertTrue(request.done.is_set())
        self.assertIn("expired", request.error)

    def test_stop_cancels_queued_test_before_any_output_is_energized(self):
        service = ESP32ActuatorService()
        service.connected = service.synchronized = True
        test = service._queue_control("TEST")
        stop = service._queue_control("ALL_OFF")
        serial = Mock()
        service._send_control_requests(serial)
        self.assertIn("cancelled", test.error)
        serial.write.assert_called_once()
        self.assertEqual(decode_protocol_line(serial.write.call_args.args[0]), ("ALL_OFF", "1"))
        service._handle_line(encode_protocol_line("ACK", 1, "ALL_OFF", 0, 100), 100000)
        self.assertTrue(stop.done.is_set())
        self.assertFalse(service.control_status()["testing"])

    def test_sorting_queue_blocks_manual_test_and_stop_clears_queue(self):
        service = ESP32ActuatorService()
        service.connected = service.synchronized = True
        self.assertTrue(service._accept_plan(self._future_plan((0,)))[0])
        self.assertFalse(service.request_led_test())
        service._queue_control("ALL_OFF")
        service._send_control_requests(Mock())
        self.assertTrue(service._plans.empty())
        self.assertEqual(service._results.qsize(), 1)

    def test_disconnect_discards_test_instead_of_replaying_after_reconnect(self):
        service = ESP32ActuatorService()
        service.connected = service.synchronized = True
        request = service._queue_control("TEST")
        service._fail_controls("Disconnected")
        self.assertEqual(request.error, "Disconnected")
        self.assertTrue(service._controls.empty())

    def test_populated_channels_follow_caml_left_to_right(self):
        self.assertEqual(GATE_GPIO_MAP, {-1: 1, 0: 2, 1: 3})

    def test_gate_mask_encodes_only_three_populated_channels(self):
        for gate, mask in ((-1, 1), (0, 2), (1, 4)):
            with self.subTest(gate=gate):
                self.assertEqual(gate_indices_to_mask((gate,)), mask)
        self.assertEqual(gate_indices_to_mask((-1, 0, 1)), 7)
        for gate in (-10, -2, 2, 10):
            with self.subTest(gate=gate), self.assertRaises(ESP32ProtocolError):
                gate_indices_to_mask((gate,))

    def test_gate_states_decode_compact_board_mask(self):
        service = ESP32ActuatorService()
        service._gate_mask = 0b101
        self.assertEqual(service.gate_states, {-1: True, 0: False, 1: True})

    def test_old_or_unidentified_firmware_cannot_synchronize(self):
        for fields in ((1, 1000, 900), (1, 1000, 900, "beano-actuator-v1", "1234")):
            with self.subTest(fields=fields):
                service = ESP32ActuatorService()
                with self.assertRaises(ESP32ProtocolError):
                    service._handle_line(encode_protocol_line("PONG", *fields), 1_100_000)
                self.assertFalse(service.synchronized)
                self.assertEqual(len(service._clock_samples), 0)

    def test_matching_firmware_synchronizes(self):
        service = ESP32ActuatorService()
        for request in range(3):
            service._handle_line(
                encode_protocol_line("PONG", request, 1000, 900, FIRMWARE_PROTOCOL, "1234", "00000000", 0),
                1_100_000,
            )
        self.assertTrue(service.synchronized)

    def test_admission_rejects_unpopulated_channel_before_queueing(self):
        service = ESP32ActuatorService()
        service.connected = service.synchronized = True
        plan = self._future_plan((0, 2))
        accepted, detail = service._accept_plan(plan)
        self.assertFalse(accepted)
        self.assertIn("gate 2 has no ESP32 GPIO", detail)
        self.assertTrue(service._plans.empty())
        self.assertEqual(service.plans_rejected, 1)

    def test_scheduled_channels_reach_board_as_compact_mask(self):
        for gates, mask in (
            ((-1,), "00000001"),
            ((0,), "00000002"),
            ((1,), "00000004"),
            ((-1, 0, 1), "00000007"),
        ):
            with self.subTest(gates=gates):
                service = ESP32ActuatorService()
                service.connected = service.synchronized = True
                service._clock_offset_ns = 0
                self.assertTrue(service._accept_plan(self._future_plan(gates))[0])
                serial = Mock()
                self.assertEqual(service._send_queued_plans(serial), 1)
                fields = decode_protocol_line(serial.write.call_args.args[0])
                self.assertEqual(fields[0], "SCHEDULE")
                self.assertEqual(fields[3], mask)

    @staticmethod
    def _future_plan(gates):
        now_ns = time.monotonic_ns()
        return ActuationPlan(
            decision_id="decision-1",
            bean_ref=BeanRef("test-run", 1),
            gate_indices=gates,
            open_monotonic_ns=now_ns + 1_000_000_000,
            close_monotonic_ns=now_ns + 1_010_000_000,
            crossing_monotonic_ns=now_ns + 1_005_000_000,
            open_source_ns=1_000_000_000,
            close_source_ns=1_010_000_000,
            crossing_source_ns=1_005_000_000,
            run_clock_source_ns=0,
            run_clock_monotonic_ns=now_ns,
            run_clock_scale_ppb=1_000_000_000,
        )

    def test_crc_protocol_round_trip_and_corruption_detection(self):
        line = encode_protocol_line("SCHEDULE", 7, 3, "00000002", 1000, 2000)
        self.assertEqual(
            decode_protocol_line(line),
            ("SCHEDULE", "7", "3", "00000002", "1000", "2000"),
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
