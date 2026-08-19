#include <inttypes.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdarg.h>

#include "driver/gpio.h"
#include "driver/gptimer.h"
#include "esp_attr.h"
#include "esp_err.h"
#include "esp_random.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/semphr.h"
#include "freertos/task.h"

#define PROTOCOL_VERSION "beano-actuator-v1"
#define GATE_COUNT 21
#define MAX_PLANS 64
#define EVENT_QUEUE_LENGTH 128
#define TIMER_TICK_US 100
#define MINIMUM_NOTICE_US 500
#define MAXIMUM_PULSE_US 100000
#define WATCHDOG_US 500000
#define LINE_BYTES 512
#define STATUS_LED_GPIO 15

typedef enum {
    PLAN_UNUSED = 0,
    PLAN_SCHEDULED = 1,
    PLAN_OPEN = 2,
} plan_state_t;

typedef struct {
    uint32_t sequence;
    uint32_t gate_mask;
    uint64_t open_us;
    uint64_t close_us;
    plan_state_t state;
} gate_plan_t;

typedef enum {
    EVENT_OPEN = 1,
    EVENT_CLOSE = 2,
} event_kind_t;

typedef struct {
    event_kind_t kind;
    uint32_t sequence;
    uint32_t gate_mask;
    uint64_t timestamp_us;
} gate_event_t;

static const gpio_num_t gate_gpios[GATE_COUNT] = {
    1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11,
    12, 13, 14, 16, 17, 18, 21, 33, 34, 35,
};

static gate_plan_t plans[MAX_PLANS];
static uint8_t gate_references[GATE_COUNT];
static uint32_t active_gate_mask;
static portMUX_TYPE schedule_mux = portMUX_INITIALIZER_UNLOCKED;
static QueueHandle_t event_queue;
static SemaphoreHandle_t print_mutex;
static volatile uint64_t last_contact_us;
static volatile bool watchdog_tripped;
static volatile bool led_test_active;
static uint32_t boot_id;

static uint32_t crc32_bytes(const uint8_t *data, size_t length)
{
    uint32_t crc = 0xFFFFFFFFU;
    for (size_t index = 0; index < length; ++index) {
        crc ^= data[index];
        for (int bit = 0; bit < 8; ++bit) {
            uint32_t mask = (uint32_t)-(int32_t)(crc & 1U);
            crc = (crc >> 1U) ^ (0xEDB88320U & mask);
        }
    }
    return ~crc;
}

static void send_body(const char *body)
{
    uint32_t checksum = crc32_bytes((const uint8_t *)body, strlen(body));
    xSemaphoreTake(print_mutex, portMAX_DELAY);
    printf("%s,%08" PRIX32 "\n", body, checksum);
    fflush(stdout);
    xSemaphoreGive(print_mutex);
}

static void send_message(const char *format, ...)
{
    char body[384];
    va_list arguments;
    va_start(arguments, format);
    vsnprintf(body, sizeof(body), format, arguments);
    va_end(arguments);
    send_body(body);
}

static void set_gate_level_isr(int gate, bool active)
{
    gpio_set_level(gate_gpios[gate], active ? 1 : 0);
}

static void open_mask_isr(uint32_t mask)
{
    for (int gate = 0; gate < GATE_COUNT; ++gate) {
        if ((mask & (1U << gate)) == 0) {
            continue;
        }
        if (gate_references[gate] < UINT8_MAX) {
            gate_references[gate]++;
        }
        if (gate_references[gate] == 1) {
            active_gate_mask |= 1U << gate;
            set_gate_level_isr(gate, true);
        }
    }
}

static void close_mask_isr(uint32_t mask)
{
    for (int gate = 0; gate < GATE_COUNT; ++gate) {
        if ((mask & (1U << gate)) == 0) {
            continue;
        }
        if (gate_references[gate] > 0) {
            gate_references[gate]--;
        }
        if (gate_references[gate] == 0) {
            active_gate_mask &= ~(1U << gate);
            set_gate_level_isr(gate, false);
        }
    }
}

static bool IRAM_ATTR timer_alarm(
    gptimer_handle_t timer,
    const gptimer_alarm_event_data_t *event_data,
    void *context)
{
    (void)timer;
    (void)event_data;
    (void)context;
    uint64_t now_us = (uint64_t)esp_timer_get_time();
    BaseType_t wake = pdFALSE;
    portENTER_CRITICAL_ISR(&schedule_mux);
    for (int index = 0; index < MAX_PLANS; ++index) {
        gate_plan_t *plan = &plans[index];
        if (plan->state == PLAN_SCHEDULED && now_us >= plan->open_us) {
            open_mask_isr(plan->gate_mask);
            plan->state = PLAN_OPEN;
            gate_event_t event = {
                .kind = EVENT_OPEN,
                .sequence = plan->sequence,
                .gate_mask = plan->gate_mask,
                .timestamp_us = now_us,
            };
            xQueueSendFromISR(event_queue, &event, &wake);
        }
        if (plan->state == PLAN_OPEN && now_us >= plan->close_us) {
            close_mask_isr(plan->gate_mask);
            plan->state = PLAN_UNUSED;
            gate_event_t event = {
                .kind = EVENT_CLOSE,
                .sequence = plan->sequence,
                .gate_mask = plan->gate_mask,
                .timestamp_us = now_us,
            };
            xQueueSendFromISR(event_queue, &event, &wake);
        }
    }
    portEXIT_CRITICAL_ISR(&schedule_mux);
    return wake == pdTRUE;
}

static uint32_t force_all_off(bool *had_work)
{
    uint32_t previous;
    bool work;
    portENTER_CRITICAL(&schedule_mux);
    previous = active_gate_mask;
    work = previous != 0 || led_test_active;
    for (int index = 0; index < MAX_PLANS; ++index) {
        work = work || plans[index].state != PLAN_UNUSED;
        plans[index].state = PLAN_UNUSED;
    }
    memset(gate_references, 0, sizeof(gate_references));
    active_gate_mask = 0;
    led_test_active = false;
    for (int gate = 0; gate < GATE_COUNT; ++gate) {
        gpio_set_level(gate_gpios[gate], 0);
    }
    portEXIT_CRITICAL(&schedule_mux);
    if (had_work != NULL) {
        *had_work = work;
    }
    return previous;
}

static gate_plan_t *find_plan(uint32_t sequence)
{
    for (int index = 0; index < MAX_PLANS; ++index) {
        if (plans[index].state != PLAN_UNUSED && plans[index].sequence == sequence) {
            return &plans[index];
        }
    }
    return NULL;
}

static gate_plan_t *free_plan(void)
{
    for (int index = 0; index < MAX_PLANS; ++index) {
        if (plans[index].state == PLAN_UNUSED) {
            return &plans[index];
        }
    }
    return NULL;
}

static bool reserve_led_test(void)
{
    bool idle = true;
    portENTER_CRITICAL(&schedule_mux);
    if (active_gate_mask != 0 || led_test_active) {
        idle = false;
    }
    for (int index = 0; index < MAX_PLANS; ++index) {
        if (plans[index].state != PLAN_UNUSED) {
            idle = false;
            break;
        }
    }
    if (idle) {
        led_test_active = true;
    }
    portEXIT_CRITICAL(&schedule_mux);
    return idle;
}

static bool parse_u32(const char *text, int base, uint32_t *result)
{
    char *end = NULL;
    unsigned long value = strtoul(text, &end, base);
    if (end == text || *end != '\0' || value > UINT32_MAX) {
        return false;
    }
    *result = (uint32_t)value;
    return true;
}

static bool parse_u64(const char *text, uint64_t *result)
{
    char *end = NULL;
    unsigned long long value = strtoull(text, &end, 10);
    if (end == text || *end != '\0') {
        return false;
    }
    *result = (uint64_t)value;
    return true;
}

static int split_fields(char *body, char **fields, int maximum)
{
    int count = 0;
    char *save = NULL;
    char *token = strtok_r(body, ",", &save);
    while (token != NULL && count < maximum) {
        fields[count++] = token;
        token = strtok_r(NULL, ",", &save);
    }
    return count;
}

static bool validate_line(char *line, char **body_out)
{
    char *comma = strrchr(line, ',');
    if (comma == NULL || comma == line) {
        return false;
    }
    *comma = '\0';
    uint32_t supplied;
    if (!parse_u32(comma + 1, 16, &supplied)) {
        return false;
    }
    uint32_t actual = crc32_bytes((const uint8_t *)line, strlen(line));
    if (actual != supplied) {
        return false;
    }
    *body_out = line;
    return true;
}

static void event_task(void *argument)
{
    (void)argument;
    gate_event_t event;
    while (true) {
        if (xQueueReceive(event_queue, &event, portMAX_DELAY) == pdTRUE) {
            send_message(
                "%s,%" PRIu32 ",%08" PRIX32 ",%" PRIu64,
                event.kind == EVENT_OPEN ? "OPEN" : "CLOSE",
                event.sequence,
                event.gate_mask,
                event.timestamp_us);
        }
    }
}

static void watchdog_task(void *argument)
{
    (void)argument;
    while (true) {
        vTaskDelay(pdMS_TO_TICKS(25));
        uint64_t now_us = (uint64_t)esp_timer_get_time();
        if (!watchdog_tripped && now_us - last_contact_us > WATCHDOG_US) {
            bool notify_host;
            uint32_t previous = force_all_off(&notify_host);
            watchdog_tripped = true;
            // With no active/pending work there is nothing to report. Avoid
            // writing unsolicited CDC data while no host is connected because
            // the ESP32-S2 ROM console can block on a disconnected endpoint.
            if (notify_host) {
                send_message("WATCHDOG,%08" PRIX32 ",%" PRIu64, previous, now_us);
            }
        }
    }
}

static void led_test_task(void *argument)
{
    uint32_t interval_ms = (uint32_t)(uintptr_t)argument;
    for (int gate = 0; gate < GATE_COUNT; ++gate) {
        if (!led_test_active) {
            break;
        }
        gpio_set_level(gate_gpios[gate], 1);
        vTaskDelay(pdMS_TO_TICKS(interval_ms));
        gpio_set_level(gate_gpios[gate], 0);
    }
    led_test_active = false;
    vTaskDelete(NULL);
}

static void handle_command(char *body)
{
    char *fields[10];
    int count = split_fields(body, fields, 10);
    uint32_t request = 0;
    if (count < 2 || !parse_u32(fields[1], 10, &request)) {
        send_message("ERR,0,BAD_REQUEST,%" PRIu64, (uint64_t)esp_timer_get_time());
        return;
    }
    last_contact_us = (uint64_t)esp_timer_get_time();
    watchdog_tripped = false;

    if (strcmp(fields[0], "PING") == 0 && count == 3) {
        uint64_t host_us;
        if (!parse_u64(fields[2], &host_us)) {
            send_message("ERR,%" PRIu32 ",BAD_PING,%" PRIu64, request, last_contact_us);
            return;
        }
        send_message(
            "PONG,%" PRIu32 ",%" PRIu64 ",%" PRIu64 ",%s,%08" PRIX32,
            request,
            host_us,
            last_contact_us,
            PROTOCOL_VERSION,
            boot_id);
        return;
    }

    if (strcmp(fields[0], "SCHEDULE") == 0 && count == 6) {
        uint32_t sequence, mask;
        uint64_t open_us, close_us;
        if (!parse_u32(fields[2], 10, &sequence)
            || !parse_u32(fields[3], 16, &mask)
            || !parse_u64(fields[4], &open_us)
            || !parse_u64(fields[5], &close_us)
            || mask == 0
            || (mask & ~((1U << GATE_COUNT) - 1U)) != 0
            || open_us < last_contact_us + MINIMUM_NOTICE_US
            || close_us <= open_us
            || close_us - open_us > MAXIMUM_PULSE_US) {
            send_message("ERR,%" PRIu32 ",INVALID_PLAN,%" PRIu64, request, last_contact_us);
            return;
        }
        portENTER_CRITICAL(&schedule_mux);
        gate_plan_t *slot = NULL;
        if (!led_test_active) {
            slot = find_plan(sequence);
            if (slot == NULL) {
                slot = free_plan();
            }
        }
        if (slot != NULL) {
            slot->sequence = sequence;
            slot->gate_mask = mask;
            slot->open_us = open_us;
            slot->close_us = close_us;
            slot->state = PLAN_SCHEDULED;
        }
        portEXIT_CRITICAL(&schedule_mux);
        if (slot == NULL) {
            send_message("ERR,%" PRIu32 ",QUEUE_FULL,%" PRIu64, request, last_contact_us);
            return;
        }
        send_message(
            "ACK,%" PRIu32 ",SCHEDULE,%" PRIu32 ",%" PRIu64,
            request,
            sequence,
            last_contact_us);
        return;
    }

    if (strcmp(fields[0], "CANCEL") == 0 && count == 3) {
        uint32_t sequence;
        if (!parse_u32(fields[2], 10, &sequence)) {
            send_message("ERR,%" PRIu32 ",BAD_CANCEL,%" PRIu64, request, last_contact_us);
            return;
        }
        portENTER_CRITICAL(&schedule_mux);
        gate_plan_t *plan = find_plan(sequence);
        if (plan != NULL) {
            if (plan->state == PLAN_OPEN) {
                close_mask_isr(plan->gate_mask);
            }
            plan->state = PLAN_UNUSED;
        }
        portEXIT_CRITICAL(&schedule_mux);
        send_message(
            "ACK,%" PRIu32 ",CANCEL,%" PRIu32 ",%" PRIu64,
            request,
            sequence,
            last_contact_us);
        return;
    }

    if (strcmp(fields[0], "ALL_OFF") == 0 && count == 2) {
        force_all_off(NULL);
        send_message("ACK,%" PRIu32 ",ALL_OFF,0,%" PRIu64, request, last_contact_us);
        return;
    }

    if (strcmp(fields[0], "TEST") == 0 && count == 3) {
        uint32_t interval_ms;
        if (!parse_u32(fields[2], 10, &interval_ms)
            || interval_ms < 20 || interval_ms > 500
            || !reserve_led_test()) {
            send_message("ERR,%" PRIu32 ",BAD_TEST,%" PRIu64, request, last_contact_us);
            return;
        }
        if (xTaskCreate(
                led_test_task,
                "gate-led-test",
                2048,
                (void *)(uintptr_t)interval_ms,
                5,
                NULL) != pdPASS) {
            led_test_active = false;
            send_message("ERR,%" PRIu32 ",TEST_TASK,%" PRIu64, request, last_contact_us);
            return;
        }
        send_message("ACK,%" PRIu32 ",TEST,0,%" PRIu64, request, last_contact_us);
        return;
    }

    send_message("ERR,%" PRIu32 ",UNKNOWN_COMMAND,%" PRIu64, request, last_contact_us);
}

static void command_task(void *argument)
{
    (void)argument;
    char line[LINE_BYTES];
    size_t used = 0;
    while (true) {
        int character = getchar();
        if (character == EOF) {
            clearerr(stdin);
            vTaskDelay(pdMS_TO_TICKS(1));
            continue;
        }
        if (character == '\r') {
            continue;
        }
        if (character == '\n') {
            if (used > 0) {
                line[used] = '\0';
                char *body = NULL;
                if (validate_line(line, &body)) {
                    handle_command(body);
                } else {
                    send_message("ERR,0,BAD_CRC,%" PRIu64, (uint64_t)esp_timer_get_time());
                }
            }
            used = 0;
            continue;
        }
        if (used + 1 < sizeof(line)) {
            line[used++] = (char)character;
        } else {
            used = 0;
        }
    }
}

void app_main(void)
{
    setvbuf(stdin, NULL, _IONBF, 0);
    setvbuf(stdout, NULL, _IONBF, 0);
    print_mutex = xSemaphoreCreateMutex();
    event_queue = xQueueCreate(EVENT_QUEUE_LENGTH, sizeof(gate_event_t));
    if (print_mutex == NULL || event_queue == NULL) {
        abort();
    }

    uint64_t output_mask = 1ULL << STATUS_LED_GPIO;
    for (int gate = 0; gate < GATE_COUNT; ++gate) {
        output_mask |= 1ULL << gate_gpios[gate];
    }
    gpio_config_t output_config = {
        .pin_bit_mask = output_mask,
        .mode = GPIO_MODE_OUTPUT,
        .pull_up_en = GPIO_PULLUP_DISABLE,
        .pull_down_en = GPIO_PULLDOWN_ENABLE,
        .intr_type = GPIO_INTR_DISABLE,
    };
    ESP_ERROR_CHECK(gpio_config(&output_config));
    force_all_off(NULL);
    gpio_set_level(STATUS_LED_GPIO, 1);

    gptimer_handle_t timer = NULL;
    gptimer_config_t timer_config = {
        .clk_src = GPTIMER_CLK_SRC_DEFAULT,
        .direction = GPTIMER_COUNT_UP,
        .resolution_hz = 1000000,
    };
    ESP_ERROR_CHECK(gptimer_new_timer(&timer_config, &timer));
    gptimer_event_callbacks_t callbacks = {.on_alarm = timer_alarm};
    ESP_ERROR_CHECK(gptimer_register_event_callbacks(timer, &callbacks, NULL));
    ESP_ERROR_CHECK(gptimer_enable(timer));
    gptimer_alarm_config_t alarm = {
        .alarm_count = TIMER_TICK_US,
        .reload_count = 0,
        .flags.auto_reload_on_alarm = true,
    };
    ESP_ERROR_CHECK(gptimer_set_alarm_action(timer, &alarm));
    ESP_ERROR_CHECK(gptimer_start(timer));

    last_contact_us = (uint64_t)esp_timer_get_time();
    boot_id = esp_random();
    xTaskCreate(event_task, "gate-events", 3072, NULL, 18, NULL);
    xTaskCreate(watchdog_task, "gate-watchdog", 3072, NULL, 19, NULL);
    xTaskCreate(command_task, "usb-commands", 4096, NULL, 20, NULL);
}
