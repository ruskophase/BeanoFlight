"""Tk health and activity monitor for the ESP32 gate actuator."""

from __future__ import annotations

import argparse
import queue
import tkinter as tk
from collections.abc import Sequence
from tkinter import messagebox, ttk

from .actuation_transport import DEFAULT_ACTUATION_ENDPOINT
from .esp32_actuator import (
    DEFAULT_ESP32_PORT,
    GATE_GPIO_MAP,
    ActuatorActivity,
    ESP32ActuatorService,
)
from .registry_service import DEFAULT_COMMAND_ENDPOINT


class ActuatorApp(tk.Tk):
    def __init__(
        self,
        *,
        registry_endpoint: str = DEFAULT_COMMAND_ENDPOINT,
        actuation_endpoint: str = DEFAULT_ACTUATION_ENDPOINT,
        serial_port: str = DEFAULT_ESP32_PORT,
        show_activity: bool = True,
    ) -> None:
        super().__init__(className="Beano Actuator")
        self.title("BeanoActuator — ESP32 Gate Controller")
        self.iconname("BeanoActuator")
        self.geometry("980x650")
        self.minsize(760, 520)
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.registry_endpoint = registry_endpoint
        self.actuation_endpoint = actuation_endpoint
        self.serial_port_var = tk.StringVar(value=serial_port)
        self.status_var = tk.StringVar(value="Starting…")
        self.counts_var = tk.StringVar(value="No plans received")
        self.show_activity_var = tk.BooleanVar(value=show_activity)
        self._activities: queue.Queue[ActuatorActivity] = queue.Queue(maxsize=256)
        self.service: ESP32ActuatorService | None = None
        self._build()
        self.after(100, self.start_service)
        self.after(100, self._poll)

    def _build(self) -> None:
        controls = ttk.LabelFrame(self, text="ESP32-S2 actuator", padding=10)
        controls.pack(fill=tk.X, padx=10, pady=10)
        ttk.Label(controls, text="USB serial path").grid(row=0, column=0, sticky=tk.W)
        ttk.Entry(
            controls,
            textvariable=self.serial_port_var,
            width=68,
        ).grid(row=1, column=0, sticky=tk.EW, padx=(0, 8))
        ttk.Button(controls, text="Reconnect", command=self.start_service).grid(
            row=1, column=1, padx=4
        )
        ttk.Button(controls, text="Test LEDs", command=self._test_leds).grid(
            row=1, column=2, padx=4
        )
        ttk.Checkbutton(
            controls,
            text="Show activity log",
            variable=self.show_activity_var,
        ).grid(row=2, column=0, sticky=tk.W, pady=(8, 0))
        controls.columnconfigure(0, weight=1)

        mapping = ttk.LabelFrame(self, text="Gate-to-GPIO mapping", padding=10)
        mapping.pack(fill=tk.X, padx=10, pady=(0, 10))
        for offset, (gate, gpio) in enumerate(GATE_GPIO_MAP.items()):
            ttk.Label(
                mapping,
                text=f"G{gate:+d} → GPIO{gpio}" if gate else f"G0 → GPIO{gpio}",
                width=14,
            ).grid(row=offset // 7, column=offset % 7, sticky=tk.W, pady=2)

        activity_frame = ttk.LabelFrame(
            self, text="Hardware scheduling and safety events", padding=8
        )
        activity_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 10))
        self.activity = tk.Text(activity_frame, state=tk.DISABLED, wrap=tk.NONE)
        self.activity.pack(fill=tk.BOTH, expand=True)

        footer = ttk.Frame(self, padding=(10, 0, 10, 10))
        footer.pack(fill=tk.X)
        ttk.Label(footer, textvariable=self.status_var).pack(side=tk.LEFT)
        ttk.Label(footer, textvariable=self.counts_var).pack(side=tk.RIGHT)

    def start_service(self) -> None:
        self.stop_service()
        self.service = ESP32ActuatorService(
            registry_endpoint=self.registry_endpoint,
            actuation_endpoint=self.actuation_endpoint,
            serial_port=self.serial_port_var.get().strip() or DEFAULT_ESP32_PORT,
            activity=self._post_activity,
        )
        self.service.start()
        self.status_var.set("Connecting to ESP32-S2…")

    def stop_service(self) -> None:
        service = self.service
        self.service = None
        if service is not None:
            service.close(drain=False)

    def _test_leds(self) -> None:
        if self.service is None or not self.service.request_led_test():
            messagebox.showwarning(
                "ESP32 LED test",
                "The ESP32 must be connected and clock-synchronized first.",
                parent=self,
            )

    def _post_activity(self, activity: ActuatorActivity) -> None:
        try:
            self._activities.put_nowait(activity)
        except queue.Full:
            try:
                self._activities.get_nowait()
            except queue.Empty:
                pass
            try:
                self._activities.put_nowait(activity)
            except queue.Full:
                pass

    def _poll(self) -> None:
        while True:
            try:
                item = self._activities.get_nowait()
            except queue.Empty:
                break
            if self.show_activity_var.get():
                message = item.kind
                if item.decision_id:
                    message += f" · {item.decision_id}"
                if item.gate_indices:
                    message += f" · gates {item.gate_indices}"
                if item.detail:
                    message += f" · {item.detail}"
                self.activity.configure(state=tk.NORMAL)
                self.activity.insert("1.0", message + "\n")
                if int(self.activity.index("end-1c").split(".")[0]) > 400:
                    self.activity.delete("400.0", tk.END)
                self.activity.configure(state=tk.DISABLED)
        service = self.service
        if service is not None:
            if service.startup_error:
                self.status_var.set(f"Error · {service.startup_error}")
            elif service.connected and service.synchronized:
                self.status_var.set(
                    f"ESP32 synchronized · USB RTT {(service.clock_rtt_ms or 0):.3f} ms"
                )
            elif service.connected:
                self.status_var.set("ESP32 connected · synchronizing clocks…")
            else:
                self.status_var.set("Waiting for ESP32-S2…")
            self.counts_var.set(
                f"plans {service.plans_received} · scheduled {service.plans_scheduled} · "
                f"rejected {service.plans_rejected} · successful {service.cycles_completed} · "
                f"failed {service.cycles_failed} · protocol errors {service.protocol_errors}"
            )
        self.after(100, self._poll)

    def _close(self) -> None:
        self.stop_service()
        self.destroy()


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="ESP32-S2 gate actuator GUI")
    result.add_argument("--registry", default=DEFAULT_COMMAND_ENDPOINT)
    result.add_argument("--plans", default=DEFAULT_ACTUATION_ENDPOINT)
    result.add_argument("--serial", default=DEFAULT_ESP32_PORT)
    result.add_argument("--no-activity-log", action="store_true")
    return result


def main(argv: Sequence[str] | None = None) -> None:
    arguments = parser().parse_args(argv)
    ActuatorApp(
        registry_endpoint=arguments.registry,
        actuation_endpoint=arguments.plans,
        serial_port=arguments.serial,
        show_activity=not arguments.no_activity_log,
    ).mainloop()


if __name__ == "__main__":
    main()
