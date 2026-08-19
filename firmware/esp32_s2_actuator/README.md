# Beano ESP32-S2 actuator firmware

This ESP-IDF application turns a LOLIN/WEMOS ESP32-S2 Mini into a hardware-timed
21-gate scheduler. The Jetson sends future absolute schedules over native USB
CDC. A 1 MHz GPTimer interrupt checks the fixed schedule table every 100 us and
changes GPIO outputs with per-gate reference counts, so overlapping bean plans
cannot close a shared gate prematurely.

Safety behaviour:

- CRC32 protects every newline-delimited command and event.
- Schedules less than 500 us ahead, longer than 100 ms, malformed, or outside
  the 21-bit gate mask are rejected.
- A 500 ms communication watchdog cancels pending work and forces every output
  low.
- ALL_OFF, CANCEL, duplicate plan acknowledgements, a bounded 64-plan table,
  and an LED chase test are supported.
- Wi-Fi and power management are unused; the CPU runs at 240 MHz.

The GPIO mapping is shared with beanoflight.esp32_actuator.GATE_GPIO_MAP.
GPIO19/GPIO20 remain reserved for native USB, and GPIO15 remains the board's
built-in status LED.

## Indicator wiring

All gate outputs are active high. For each gate, connect the GPIO to a separate
series resistor (1 kΩ is a conservative starting value), connect that resistor
to the LED anode, and connect the LED cathode to GND. Use a common ground. Do not
connect an LED without a resistor, and never connect a solenoid or valve coil
directly to an ESP32 pin.

| Gate | GPIO | Gate | GPIO | Gate | GPIO |
| ---: | ---: | ---: | ---: | ---: | ---: |
| G-10 | 1 | G-3 | 8 | G+4 | 16 |
| G-9 | 2 | G-2 | 9 | G+5 | 17 |
| G-8 | 3 | G-1 | 10 | G+6 | 18 |
| G-7 | 4 | G0 | 11 | G+7 | 21 |
| G-6 | 5 | G+1 | 12 | G+8 | 33 |
| G-5 | 6 | G+2 | 13 | G+9 | 34 |
| G-4 | 7 | G+3 | 14 | G+10 | 35 |

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
until BeanoActuator reports `ESP32 synchronized`, then use **Test LEDs** to chase
the 21 outputs in gate order.
