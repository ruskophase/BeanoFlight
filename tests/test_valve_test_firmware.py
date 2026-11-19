"""Execute the actual firmware sequence logic using a native C compiler."""

import subprocess
import tempfile
import unittest
from pathlib import Path


class ValveTestFirmwareTests(unittest.TestCase):
    def test_pulse_boundaries_completion_cancel_and_restart(self):
        include = Path(__file__).parents[1] / "firmware/esp32_s2_actuator/main"
        source = r'''
#include <assert.h>
#include "valve_test.h"
int main(void) {
    valve_test_t test = {0};
    assert(valve_test_mask(&test, 0) == 0);
    valve_test_start(&test, 100, true);
    assert(test.autonomous);
    assert(valve_test_mask(&test, 99) == 0);
    for (uint64_t t = 0; t <= 1100000; t += 100) {
        uint32_t expected = t < 50000 ? 1 :
            t >= 500000 && t < 550000 ? 2 :
            t >= 1000000 && t < 1050000 ? 4 : 0;
        assert(valve_test_mask(&test, 100 + t) == expected);
        assert(test.active == (t < 1050000));
    }
    assert(!test.autonomous);
    for (unsigned channel = 0; channel < 3; ++channel) {
        valve_test_start(&test, 0, false);
        assert(valve_test_mask(&test, channel * 500000 + 1) == (1U << channel));
        valve_test_cancel(&test);
        assert(valve_test_mask(&test, 1000000) == 0);
    }
    valve_test_start(&test, 2000000, false);
    assert(valve_test_mask(&test, 2000000) == 1);
    assert(valve_test_mask(&test, 2550000) == 0); // Missed window is not replayed.
    assert(valve_test_mask(&test, 4000000) == 0);
    assert(!test.active);
}
'''
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "test.c").write_text(source)
            subprocess.run(
                ["cc", "-std=c11", "-Wall", "-Wextra", "-Werror", "-I", str(include),
                 str(root / "test.c"), "-o", str(root / "test")], check=True,
            )
            subprocess.run([str(root / "test")], check=True)
