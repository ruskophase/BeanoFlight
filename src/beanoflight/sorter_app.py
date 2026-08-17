"""Tk policy controls and virtual gate display for BeanoSorter."""

from __future__ import annotations

import argparse
import queue
import threading
import tkinter as tk
from collections.abc import Sequence
from tkinter import messagebox, ttk

from .registry_service import DEFAULT_COMMAND_ENDPOINT
from .sorter import SorterActivity, SorterService, SorterSettings


class SorterApp(tk.Tk):
    def __init__(self, registry_endpoint: str) -> None:
        super().__init__(className="Beano Sorter")
        self.title("BeanoSorter — Virtual Gates")
        self.iconname("BeanoSorter")
        self.geometry("1150x720")
        self.minsize(900, 600)
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.registry_endpoint = registry_endpoint
        self.service: SorterService | None = None
        self._activities: queue.Queue[SorterActivity] = queue.Queue(maxsize=256)
        self._gate_items: dict[int, int] = {}
        self._displayed_gate_states: dict[int, bool] = {}
        self._activity_display_enabled = threading.Event()
        self._activity_display_enabled.set()

        defaults = SorterSettings()
        self.categories_var = tk.StringVar(value=",".join(defaults.reject_categories))
        self.confidence_var = tk.StringVar(value=str(defaults.minimum_confidence))
        self.probability_var = tk.StringVar(
            value=str(defaults.gate_probability_threshold)
        )
        self.lead_var = tk.StringVar(value=str(defaults.open_lead_ms))
        self.lag_var = tk.StringVar(value=str(defaults.close_lag_ms))
        self.notice_var = tk.StringVar(value=str(defaults.minimum_notice_ms))
        self.status_var = tk.StringVar(value="Stopped")
        self.counts_var = tk.StringVar(value="decisions 0 · actuations 0 · errors 0")
        self.animate_gates_var = tk.BooleanVar(value=True)
        self.show_activity_var = tk.BooleanVar(value=True)
        self._build()
        self.after(50, self._poll)
        self.after(100, self.start_service)

    def _build(self) -> None:
        controls = ttk.LabelFrame(self, text="Sorting policy", padding=10)
        controls.pack(fill=tk.X, padx=10, pady=10)
        fields = (
            ("Reject categories", self.categories_var, 34),
            ("Min confidence", self.confidence_var, 10),
            ("Gate probability", self.probability_var, 10),
            ("Open lead ms", self.lead_var, 10),
            ("Close lag ms", self.lag_var, 10),
            ("Min notice ms", self.notice_var, 10),
        )
        for column, (label, variable, width) in enumerate(fields):
            ttk.Label(controls, text=label).grid(row=0, column=column, sticky=tk.W)
            ttk.Entry(controls, textvariable=variable, width=width).grid(
                row=1, column=column, padx=(0, 8), sticky=tk.EW
            )
        ttk.Button(controls, text="Start / apply", command=self.start_service).grid(
            row=1, column=len(fields), padx=(8, 4)
        )
        ttk.Button(controls, text="Stop", command=self.stop_service).grid(
            row=1, column=len(fields) + 1
        )
        ttk.Checkbutton(
            controls,
            text="Animate virtual gates (uses extra CPU)",
            variable=self.animate_gates_var,
            command=self._display_options_changed,
        ).grid(row=2, column=0, columnspan=3, sticky=tk.W, pady=(8, 0))
        ttk.Checkbutton(
            controls,
            text="Show activity log",
            variable=self.show_activity_var,
            command=self._display_options_changed,
        ).grid(row=2, column=3, columnspan=3, sticky=tk.W, pady=(8, 0))

        gates = ttk.LabelFrame(self, text="Virtual sorting line", padding=10)
        gates.pack(fill=tk.X, padx=10, pady=(0, 10))
        self.canvas = tk.Canvas(
            gates, height=155, background="#171c22", highlightthickness=0
        )
        self.canvas.pack(fill=tk.X)
        self.canvas.bind("<Configure>", lambda _event: self._draw_gates())

        activity_frame = ttk.LabelFrame(self, text="Decisions and actuation", padding=8)
        activity_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 10))
        self.activity = tk.Text(activity_frame, state=tk.DISABLED, wrap=tk.NONE)
        self.activity.pack(fill=tk.BOTH, expand=True)

        footer = ttk.Frame(self, padding=(10, 0, 10, 10))
        footer.pack(fill=tk.X)
        ttk.Label(footer, textvariable=self.status_var).pack(side=tk.LEFT)
        ttk.Label(footer, textvariable=self.counts_var).pack(side=tk.RIGHT)

    def start_service(self) -> None:
        try:
            settings = SorterSettings(
                reject_categories=tuple(
                    item.strip()
                    for item in self.categories_var.get().split(",")
                    if item.strip()
                ),
                minimum_confidence=float(self.confidence_var.get()),
                gate_probability_threshold=float(self.probability_var.get()),
                open_lead_ms=float(self.lead_var.get()),
                close_lag_ms=float(self.lag_var.get()),
                minimum_notice_ms=float(self.notice_var.get()),
            )
            settings.validate()
        except ValueError as exc:
            messagebox.showerror("Sorter settings", str(exc), parent=self)
            return
        self.stop_service()
        self.service = SorterService(
            registry_endpoint=self.registry_endpoint,
            settings=settings,
            activity=self._post_activity,
        )
        self.service.start()
        self.status_var.set(f"Running · registry {self.registry_endpoint}")

    def stop_service(self) -> None:
        service = self.service
        self.service = None
        if service is not None:
            service.close()
        self.status_var.set("Stopped")
        self._paint_gate_states({})

    def _draw_gates(self) -> None:
        self.canvas.delete("all")
        self._gate_items.clear()
        width = max(1, self.canvas.winfo_width())
        indices = tuple(range(-10, 11))
        spacing = width / (len(indices) + 1)
        for offset, gate in enumerate(indices, start=1):
            x = round(offset * spacing)
            item = self.canvas.create_oval(
                x - 22,
                40,
                x + 22,
                84,
                fill="#111111",
                outline="#8b98a6",
                width=2,
            )
            self.canvas.create_text(
                x,
                112,
                text="G0" if gate == 0 else f"G{gate:+d}",
                fill="#dce3ea",
            )
            self._gate_items[gate] = item
        self._paint_gate_states(
            self._displayed_gate_states if self.animate_gates_var.get() else {}
        )

    def _post_activity(self, activity: SorterActivity) -> None:
        if not self._activity_display_enabled.is_set():
            return
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
            message = f"{item.kind:9} {item.bean_id}"
            if item.category:
                message += f" · {item.category}"
            if item.confidence is not None:
                message += f" {item.confidence:.1%}"
            if item.gate_indices:
                message += f" · gates {item.gate_indices}"
            if item.detail:
                message += f" · {item.detail}"
            if self._activity_display_enabled.is_set():
                self.activity.configure(state=tk.NORMAL)
                self.activity.insert("1.0", message + "\n")
                if int(self.activity.index("end-1c").split(".")[0]) > 400:
                    self.activity.delete("400.0", tk.END)
                self.activity.configure(state=tk.DISABLED)
        service = self.service
        if service is not None:
            self._update_gate_states(service.gate_states)
            self.counts_var.set(
                f"decisions {service.decisions} · actuations {service.actuations} · "
                f"errors {service.errors}"
            )
        self.after(50, self._poll)

    def _update_gate_states(self, states: dict[int, bool]) -> None:
        normalized = {gate: bool(active) for gate, active in states.items() if active}
        if (
            not self.animate_gates_var.get()
            or normalized == self._displayed_gate_states
        ):
            return
        self._paint_gate_states(normalized)

    def _paint_gate_states(self, states: dict[int, bool]) -> None:
        self._displayed_gate_states = dict(states)
        for gate, item in self._gate_items.items():
            self.canvas.itemconfigure(
                item,
                fill="#ed3038" if states.get(gate, False) else "#111111",
                outline="#ff9196" if states.get(gate, False) else "#8b98a6",
            )

    def _display_options_changed(self) -> None:
        if self.show_activity_var.get():
            self._activity_display_enabled.set()
        else:
            self._activity_display_enabled.clear()
        if self.animate_gates_var.get():
            states = {} if self.service is None else self.service.gate_states
            self._paint_gate_states(states)
        else:
            self._paint_gate_states({})

    def _close(self) -> None:
        self.stop_service()
        self.destroy()


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="BeanoFlight sorting simulation GUI")
    result.add_argument("--registry", default=DEFAULT_COMMAND_ENDPOINT)
    return result


def main(argv: Sequence[str] | None = None) -> None:
    arguments = parser().parse_args(argv)
    SorterApp(arguments.registry).mainloop()


if __name__ == "__main__":
    main()
