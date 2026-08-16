"""Read-only Tk monitor for the authoritative BeanRegistry process."""

from __future__ import annotations

import argparse
import queue
import tkinter as tk
from collections.abc import Sequence
from tkinter import ttk

from .registry_monitor import RegistryMonitorSnapshot, RegistryMonitorWorker
from .registry_service import DEFAULT_COMMAND_ENDPOINT


class RegistryMonitorApp(tk.Tk):
    def __init__(self, registry_endpoint: str) -> None:
        super().__init__()
        self.title("BeanoFlight — BeanRegistry Monitor")
        self.geometry("1500x800")
        self.minsize(1050, 620)
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.registry_endpoint = registry_endpoint
        self._snapshots: queue.Queue[RegistryMonitorSnapshot] = queue.Queue(maxsize=2)
        self.status_var = tk.StringVar(value="Connecting…")
        self.session_var = tk.StringVar(value="No run session")
        self.cursor_var = tk.StringVar(value="event cursor 0")
        self._build()
        self.worker = RegistryMonitorWorker(
            self._post_snapshot, registry_endpoint=registry_endpoint
        )
        self.worker.start()
        self.after(100, self._poll)

    def _build(self) -> None:
        header = ttk.Frame(self, padding=10)
        header.pack(fill=tk.X)
        ttk.Label(header, textvariable=self.status_var).pack(side=tk.LEFT)
        ttk.Label(header, textvariable=self.session_var).pack(side=tk.LEFT, padx=20)
        ttk.Label(header, textvariable=self.cursor_var).pack(side=tk.RIGHT)

        body = ttk.Panedwindow(self, orient=tk.VERTICAL)
        body.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 10))
        records_frame = ttk.LabelFrame(body, text="Bean materialized state", padding=8)
        events_frame = ttk.LabelFrame(body, text="Significant activity", padding=8)
        body.add(records_frame, weight=3)
        body.add(events_frame, weight=2)

        columns = (
            "bean",
            "status",
            "revision",
            "position",
            "crossing",
            "inference",
            "classification",
            "decision",
            "actuation",
        )
        self.records = ttk.Treeview(
            records_frame, columns=columns, show="headings", height=14
        )
        widths = (150, 90, 70, 140, 170, 110, 190, 170, 110)
        for name, width in zip(columns, widths):
            self.records.heading(name, text=name.replace("_", " ").title())
            self.records.column(name, width=width, anchor=tk.W)
        self.records.pack(fill=tk.BOTH, expand=True)
        self.events = tk.Text(events_frame, state=tk.DISABLED, wrap=tk.NONE)
        self.events.pack(fill=tk.BOTH, expand=True)

    def _post_snapshot(self, snapshot: RegistryMonitorSnapshot) -> None:
        try:
            self._snapshots.put_nowait(snapshot)
        except queue.Full:
            try:
                self._snapshots.get_nowait()
            except queue.Empty:
                pass
            try:
                self._snapshots.put_nowait(snapshot)
            except queue.Full:
                pass

    def _poll(self) -> None:
        latest = None
        while True:
            try:
                latest = self._snapshots.get_nowait()
            except queue.Empty:
                break
        if latest is not None:
            self._render(latest)
        self.after(100, self._poll)

    def _render(self, snapshot: RegistryMonitorSnapshot) -> None:
        if not snapshot.connected:
            self.status_var.set(f"Disconnected · {snapshot.error}")
            return
        self.status_var.set(f"Connected · {self.registry_endpoint}")
        self.cursor_var.set(f"event cursor {snapshot.cursor}")
        if snapshot.sessions:
            session = snapshot.sessions[-1]
            self.session_var.set(
                f"run {session.run_id[:12]} · {session.state.value} · "
                f"{session.target_fps:g} fps"
            )
        else:
            self.session_var.set("No run session")
        existing = set(self.records.get_children())
        observed: set[str] = set()
        for record in snapshot.records:
            key = f"{record.bean_ref.run_id}:{record.bean_ref.sequence}"
            observed.add(key)
            prediction = record.prediction
            classification = next(
                (
                    item
                    for item in reversed(record.enrichments)
                    if item.kind == "classification"
                ),
                None,
            )
            category = ""
            if classification is not None:
                value = classification.value
                category = (
                    str(value.get("category", ""))
                    if isinstance(value, dict)
                    else str(value)
                )
                if classification.confidence is not None:
                    category += f" {classification.confidence:.1%}"
            decision = record.decision
            values = (
                str(record.bean_ref),
                record.status.value,
                record.revision,
                f"{record.track.x_mm:.1f}, {record.track.y_mm:.1f} mm",
                (
                    ""
                    if prediction is None
                    else f"x={prediction.x_mean_mm:.1f} @ {prediction.crossing_timestamp_ns}"
                ),
                record.inference_jobs[-1].status.value if record.inference_jobs else "",
                category,
                ""
                if decision is None
                else f"{decision.gate_indices} · {decision.reason}",
                ""
                if record.actuation is None
                else ("OK" if record.actuation.success else "FAIL"),
            )
            if key in existing:
                self.records.item(key, values=values)
            else:
                self.records.insert("", tk.END, iid=key, values=values)
        for key in existing - observed:
            self.records.delete(key)
        if snapshot.significant_events:
            self.events.configure(state=tk.NORMAL)
            for event in reversed(snapshot.significant_events):
                self.events.insert(
                    "1.0",
                    f"#{event.stream_sequence:06d} {event.kind:24} "
                    f"{event.bean_ref} rev {event.revision}\n",
                )
            if int(self.events.index("end-1c").split(".")[0]) > 500:
                self.events.delete("500.0", tk.END)
            self.events.configure(state=tk.DISABLED)

    def _close(self) -> None:
        self.worker.close()
        self.destroy()


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="BeanRegistry read-only monitor GUI")
    result.add_argument("--registry", default=DEFAULT_COMMAND_ENDPOINT)
    return result


def main(argv: Sequence[str] | None = None) -> None:
    arguments = parser().parse_args(argv)
    RegistryMonitorApp(arguments.registry).mainloop()


if __name__ == "__main__":
    main()
