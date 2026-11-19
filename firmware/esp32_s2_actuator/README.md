# Beano ESP32-S2 actuator firmware

This ESP-IDF application turns a LOLIN/WEMOS ESP32-S2 Mini into a hardware-timed
three-channel scheduler for the populated SX11 valve-driver breakout. The Jetson
sends future absolute schedules over native USB CDC. A 1 MHz GPTimer interrupt
checks the fixed schedule table every 100 us and
changes GPIO outputs with per-gate reference counts, so overlapping bean plans
cannot close a shared gate prematurely.

Safety behaviour:

- CRC32 protects every newline-delimited command and event.
- Schedules less than 200 us ahead, longer than 100 ms, malformed, or outside
  the three-bit channel mask are rejected. The 200 us admission floor spans two
  100 us scheduler scans; mechanical valve lead time remains a separate,
  longer host policy.
- BeanoActuator applies a 300 us host-admission floor before the board's
  200 us firmware floor, leaving approximately one scheduler scan for native
  USB transfer and table insertion.
- A 500 ms communication watchdog cancels pending work and forces every output
  low. The bounded boot test is exempt so it can finish without a USB host;
  manual tests retain the watchdog.
- ALL_OFF, CANCEL, duplicate plan acknowledgements, a bounded 64-plan table,
  and a fixed valve-channel test are supported.
- Wi-Fi, Bluetooth and radio PHY components are excluded by a minimal build
  with explicit dependencies; CMake rejects any build that adds them back.
  No radio is initialized. The ESP32-S2 has no Bluetooth radio hardware.
  Power management is unused; the CPU runs at 240 MHz.

The GPIO mapping is shared with beanoflight.esp32_actuator.GATE_GPIO_MAP.
GPIO19/GPIO20 remain reserved for native USB, and GPIO15 remains the board's
built-in status LED.

## Populated breakout channels

Left and right are defined **as viewed by CamL**. Each populated channel has an
LED indicator and a FET valve switch. No valves are connected at this stage.
Outputs retain active-high switching (HIGH on, LOW off); the breakout's actual
FET polarity and valve drive requirements must be verified before valve use.

| Valve | CamL position | Logical gate | GPIO | USB mask bit |
| --- | --- | ---: | ---: | ---: |
| -V1 | Left | -1 | 1 | 0 |
| V0 | Centre | 0 | 2 | 1 |
| +V1 | Right | +1 | 3 | 2 |

Only GPIO1–3 are configured as channel outputs. GPIO15 remains the status LED.
All three channels are initialized LOW, then the following sequence runs once
on **every boot/reset**, even with no USB host connected. It switches both LEDs
and FETs. Valves are currently disconnected for bench testing.

| Channel | On from sequence start | Off |
| --- | ---: | ---: |
| -V1 / GPIO1 | 0 ms | 50 ms |
| V0 / GPIO2 | 500 ms | 550 ms |
| +V1 / GPIO3 | 1,000 ms | 1,050 ms |

Pulse starts are 500 ms apart, leaving 450 ms off between pulses. The existing
100 us hardware-timer scan controls the edges. All outputs remain LOW after
completion. `ALL_OFF` cancels the sequence immediately, including its remaining
pulses; it never resumes without a new test command or board reset.

The CRC-protected `TEST,<request>` command runs exactly the same sequence.
There are no timing arguments. It replies with `ACK` when started or `ERR BUSY`
if a test or sorting plan already occupies the board. Sorting schedules are
also rejected during the test. `PING` replies include the current output mask
and test-active flag. USB console writes are not used to advance the sequence.

The WebGUI's **Valve channel test** panel uses BeanoActuator's local control
endpoint (`tcp://127.0.0.1:5592`). BeanoActuator remains the only process opening
this USB device. The WebGUI reports a successful start only after the firmware
ACK; failed or timed-out start requests are never automatically retried.

The USB identity is `beano-actuator-v3-3valve`: update host and firmware together.
The previous 21-channel firmware uses a different mapping and is rejected by
the updated host. USB masks are now 1, 2 and 4, or 7 for all three channels.
The host rejects plans for unpopulated logical gates before queueing them.
Measured nozzle maps retain their existing logical channel convention:
`valve_channel` 9, 10 and 11 select -V1, V0 and +V1 respectively; these map
fields are not GPIO numbers or the new USB mask bits.

## Build and flash

Use ESP-IDF 6.0.2 or a compatible release:

```bash
source /path/to/esp-idf/export.sh
cd firmware/esp32_s2_actuator
idf.py set-target esp32s2
idf.py build
idf.py -p /dev/serial/by-path/platform-3610000.usb-usb-0:2.1:1.0 flash
```

The path above is the stable path of the development board used for this build;
confirm it with `ls -l /dev/serial/by-path` before flashing another machine.
Native USB uses GPIO19/GPIO20 and exposes the firmware console as a CDC device.
After flashing, start `beano-actuator` or use the launcher's **Start all**. Wait
until BeanoActuator reports `ESP32 synchronized`, then use
**Test outputs (50 ms, 500 ms apart)** to chase the three populated channels.

For WebGUI-only operation, run `beano-actuator --headless --serial <valve-board-port>`
in place of the normal BeanoActuator window. Do not run both instances together.
The WebGUI still connects separately to the motor/illumination controller.
`BEANO_VALVE_CONTROL_ENDPOINT` can override the local bridge endpoint in both
processes; keep it on loopback. The API provides status, fixed test and stop only.
