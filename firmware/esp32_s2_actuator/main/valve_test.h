#pragma once

#include <stdbool.h>
#include <stdint.h>

#define VALVE_TEST_PULSE_US 50000ULL
#define VALVE_TEST_SPACING_US 500000ULL
#define VALVE_TEST_CHANNELS 3U
#define VALVE_TEST_DURATION_US (2 * VALVE_TEST_SPACING_US + VALVE_TEST_PULSE_US)

typedef struct {
    bool active;
    bool autonomous;
    uint64_t start_us;
} valve_test_t;

static inline void valve_test_start(valve_test_t *test, uint64_t now_us, bool autonomous)
{
    test->start_us = now_us;
    test->autonomous = autonomous;
    test->active = true;
}

static inline void valve_test_cancel(valve_test_t *test)
{
    test->active = false;
    test->autonomous = false;
}

// Called under the scheduler lock. Late scans never extend a pulse or replay
// an already missed window. No task sleeps, USB writes, or dynamic allocation.
static inline uint32_t valve_test_mask(valve_test_t *test, uint64_t now_us)
{
    if (!test->active || now_us < test->start_us) {
        return 0;
    }
    uint64_t elapsed = now_us - test->start_us;
    if (elapsed >= VALVE_TEST_DURATION_US) {
        valve_test_cancel(test);
        return 0;
    }
    unsigned channel = elapsed / VALVE_TEST_SPACING_US;
    return elapsed % VALVE_TEST_SPACING_US < VALVE_TEST_PULSE_US
        ? (1U << channel) : 0;
}
