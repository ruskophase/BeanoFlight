"""Exercise the WebGUI's real RPC client against BeanoActuator with fake USB."""

import importlib.util
import threading
import time
import unittest
import uuid
from pathlib import Path

import zmq

from beanoflight.esp32_actuator import (
    CONTROL_SCHEMA,
    ESP32ActuatorService,
    decode_protocol_line,
    encode_protocol_line,
)


class ValveWebBridgeTests(unittest.TestCase):
    def setUp(self):
        client_file = Path(__file__).parents[2] / "ESP_Web_controller/app/valve_controller.py"
        if not client_file.is_file():
            self.skipTest("WebGUI bridge integration requires the sibling ESP_Web_controller checkout")
        spec = importlib.util.spec_from_file_location("valve_web_client", client_file)
        self.client_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.client_module)
        endpoint = f"inproc://valve-test-{uuid.uuid4()}"
        self.service = ESP32ActuatorService(control_endpoint=endpoint)
        self.service.connected = self.service.synchronized = True
        self.client = self.client_module.ValveController(endpoint)
        self.writes = []
        self.reject = False
        service = self.service
        test = self

        class Serial:
            def write(self, line):
                fields = decode_protocol_line(line)
                test.writes.append(fields)
                response = (encode_protocol_line("ERR", fields[1], "BUSY", 100)
                            if test.reject else
                            encode_protocol_line("ACK", fields[1], fields[0], 0, 100))
                service._handle_line(response, time.monotonic_ns())

        def pump():
            while not service._stop.wait(0.005):
                service._send_control_requests(Serial())

        self.threads = [
            threading.Thread(target=service._control_loop),
            threading.Thread(target=pump),
        ]
        for thread in self.threads:
            thread.start()

    def tearDown(self):
        self.service._stop.set()
        for thread in self.threads:
            thread.join(2)
            self.assertFalse(thread.is_alive())

    def test_status_test_duplicate_rejection_and_stop(self):
        self.assertTrue(self.client.snapshot()["synchronized"])
        self.assertTrue(self.client.command("test")["testing"])
        self.assertEqual(self.writes[0][0], "TEST")
        self.assertEqual(len(self.writes[0]), 2)  # Timing belongs to firmware.
        with self.assertRaisesRegex(self.client_module.ValveControlError, "busy"):
            self.client.command("test")
        self.assertEqual(len(self.writes), 1)
        self.assertFalse(self.client.command("stop")["testing"])
        self.assertEqual(self.writes[-1][0], "ALL_OFF")

    def test_firmware_rejection_propagates_to_web_client(self):
        self.reject = True
        with self.assertRaisesRegex(self.client_module.ValveControlError, "BUSY"):
            self.client.command("test")
        self.assertEqual(len(self.writes), 1)

    def test_expired_rpc_never_reaches_usb(self):
        socket = zmq.Context.instance().socket(zmq.REQ)
        socket.setsockopt(zmq.LINGER, 0)
        try:
            socket.connect(self.service.control_endpoint)
            socket.send_json({"schema": CONTROL_SCHEMA, "action": "test", "deadline_ns": 0})
            self.assertTrue(socket.poll(1000))
            reply = socket.recv_json()
            self.assertFalse(reply["ok"])
            self.assertIn("expired", reply["error"])
            self.assertEqual(self.writes, [])
        finally:
            socket.close(0)

    def test_unavailable_service_times_out_without_retry(self):
        client = self.client_module.ValveController(f"inproc://absent-{uuid.uuid4()}")
        with self.assertRaisesRegex(self.client_module.ValveControlError, "not retried"):
            client._request("test", 20)
