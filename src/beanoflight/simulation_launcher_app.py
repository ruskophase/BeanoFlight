"""Convenience launcher for independently running simulation processes."""

from __future__ import annotations

import argparse
import subprocess
import sys
import tkinter as tk
from collections.abc import Sequence
from pathlib import Path
from tkinter import filedialog, ttk

from .inference_transport import DEFAULT_CROP_ENDPOINT
from .registry_service import (
    DEFAULT_COMMAND_ENDPOINT,
    DEFAULT_EVENT_ENDPOINT,
    ipc_endpoint_has_listener,
    registry_processes_for_database,
)
from .registry_zmq import ZeroMQRegistryClient

REGISTRY_ABSENT = "absent"
REGISTRY_CONFLICT = "conflict"
REGISTRY_HEALTHY = "healthy"
REGISTRY_UNRESPONSIVE = "unresponsive"


def registry_endpoint_state(
    endpoint: str = DEFAULT_COMMAND_ENDPOINT,
    *,
    database: Path | None = None,
    timeout_ms: int = 250,
) -> str:
    """Classify the endpoint without allowing a second service to replace it."""

    client = ZeroMQRegistryClient(endpoint, timeout_ms=max(1, int(timeout_ms)))
    try:
        response = client.ping()
        if response.get("service") == "BeanRegistry":
            active_database = str(response.get("database", "")).strip()
            if (
                database is not None
                and active_database
                and Path(active_database).resolve() != database.expanduser().resolve()
            ):
                return REGISTRY_CONFLICT
            return REGISTRY_HEALTHY
    except Exception:  # noqa: BLE001 - transport failure becomes state
        occupied = ipc_endpoint_has_listener(endpoint) or (
            database is not None and bool(registry_processes_for_database(database))
        )
        return REGISTRY_UNRESPONSIVE if occupied else REGISTRY_ABSENT
    finally:
        client.close()
    occupied = ipc_endpoint_has_listener(endpoint) or (
        database is not None and bool(registry_processes_for_database(database))
    )
    return REGISTRY_UNRESPONSIVE if occupied else REGISTRY_ABSENT


class SimulationLauncherApp(tk.Tk):
    def __init__(self, initial_recording: Path | None = None) -> None:
        super().__init__(className="BeanoFlight Simulation")
        self.title("BeanoFlight Simulation Launcher")
        self.iconname("BeanoFlight Simulation")
        self.geometry("800x500")
        self.minsize(700, 430)
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.recording_var = tk.StringVar(
            value="" if initial_recording is None else str(initial_recording)
        )
        self.database_var = tk.StringVar(value="beanoflight-simulation.db")
        self.status_var = tk.StringVar(value="All components stopped")
        self._processes: dict[str, subprocess.Popen] = {}
        self._external_registry = False
        self._registry_blocked = ""
        self._build()
        self.after(500, self._poll)

    def _build(self) -> None:
        form = ttk.LabelFrame(self, text="Simulation", padding=12)
        form.pack(fill=tk.X, padx=12, pady=12)
        ttk.Label(form, text="Recording bundle or MKV").grid(
            row=0, column=0, sticky=tk.W
        )
        ttk.Entry(form, textvariable=self.recording_var).grid(
            row=1, column=0, sticky=tk.EW, padx=(0, 8)
        )
        ttk.Button(form, text="Browse…", command=self._browse_recording).grid(
            row=1, column=1
        )
        ttk.Label(form, text="Registry database").grid(
            row=2, column=0, sticky=tk.W, pady=(10, 0)
        )
        ttk.Entry(form, textvariable=self.database_var).grid(
            row=3, column=0, sticky=tk.EW, padx=(0, 8)
        )
        form.columnconfigure(0, weight=1)

        buttons = ttk.LabelFrame(self, text="Independent processes", padding=12)
        buttons.pack(fill=tk.BOTH, expand=True, padx=12, pady=(0, 12))
        components = (
            ("registry", "1. BeanRegistry", self._start_registry),
            ("monitor", "2. Registry Monitor", self._start_monitor),
            ("inferencer", "3. Mock Inferencer", self._start_inferencer),
            ("sorter", "4. BeanoSorter", self._start_sorter),
            ("flight", "5. BeanoFlight", self._start_flight),
        )
        for row, (_key, label, command) in enumerate(components):
            ttk.Button(buttons, text=f"Start {label}", command=command).grid(
                row=row, column=0, sticky=tk.EW, pady=3
            )
        ttk.Button(buttons, text="Start all", command=self.start_all).grid(
            row=0, column=1, sticky=tk.EW, padx=(12, 0), pady=3
        )
        ttk.Button(buttons, text="Stop all", command=self.stop_all).grid(
            row=1, column=1, sticky=tk.EW, padx=(12, 0), pady=3
        )
        ttk.Label(
            buttons,
            text=(
                "Each button launches a separate operating-system process. Closing this "
                "launcher does not silently discard the registry database."
            ),
            wraplength=330,
        ).grid(row=3, column=1, rowspan=2, sticky=tk.NW, padx=(12, 0), pady=6)
        buttons.columnconfigure(0, weight=1)
        buttons.columnconfigure(1, weight=1)
        ttk.Label(self, textvariable=self.status_var, padding=12).pack(fill=tk.X)

    def _browse_recording(self) -> None:
        selected = filedialog.askdirectory(title="Select FastCap recording bundle")
        if selected:
            self.recording_var.set(selected)

    def _launch(self, key: str, module: str, *arguments: str) -> bool:
        existing = self._processes.get(key)
        if existing is not None and existing.poll() is None:
            return True
        self._processes[key] = subprocess.Popen(
            [sys.executable, "-m", module, *arguments],
            start_new_session=True,
        )
        return True

    def _start_registry(self) -> bool:
        existing = self._processes.get("registry")
        if existing is not None and existing.poll() is None:
            return True
        state = registry_endpoint_state(
            database=Path(
                self.database_var.get().strip() or "beanoflight-simulation.db"
            )
        )
        if state == REGISTRY_HEALTHY:
            self._external_registry = True
            self._registry_blocked = ""
            self.status_var.set("Using the existing healthy BeanRegistry service")
            return True
        if state == REGISTRY_CONFLICT:
            self._external_registry = False
            self._registry_blocked = (
                "Registry endpoint is serving a different database. Stop that "
                "registry or select its database before starting this simulation."
            )
            self.status_var.set(self._registry_blocked)
            return False
        if state == REGISTRY_UNRESPONSIVE:
            self._external_registry = False
            self._registry_blocked = (
                "Registry endpoint is occupied but not answering. Stop the old "
                "registry process before starting another."
            )
            self.status_var.set(self._registry_blocked)
            return False
        self._external_registry = False
        self._registry_blocked = ""
        self.status_var.set("Starting BeanRegistry…")
        return self._launch(
            "registry",
            "beanoflight.registry_service",
            "--database",
            self.database_var.get(),
            "--commands",
            DEFAULT_COMMAND_ENDPOINT,
            "--events",
            DEFAULT_EVENT_ENDPOINT,
        )

    def _start_monitor(self) -> None:
        self._launch(
            "monitor",
            "beanoflight.registry_monitor_app",
            "--registry",
            DEFAULT_COMMAND_ENDPOINT,
        )

    def _start_inferencer(self) -> None:
        self._launch(
            "inferencer",
            "beanoflight.mock_inferencer_app",
            "--registry",
            DEFAULT_COMMAND_ENDPOINT,
            "--crops",
            DEFAULT_CROP_ENDPOINT,
        )

    def _start_sorter(self) -> None:
        self._launch(
            "sorter",
            "beanoflight.sorter_app",
            "--registry",
            DEFAULT_COMMAND_ENDPOINT,
        )

    def _start_flight(self) -> None:
        arguments = []
        if self.recording_var.get().strip():
            arguments.append(self.recording_var.get().strip())
        self._launch("flight", "beanoflight.cli", *arguments)

    def start_all(self) -> None:
        if not self._start_registry():
            return
        self.after(350, self._start_monitor)
        self.after(500, self._start_inferencer)
        self.after(650, self._start_sorter)
        self.after(800, self._start_flight)

    def stop_all(self) -> None:
        processes = tuple(self._processes.values())
        for process in processes:
            if process.poll() is None:
                process.terminate()
        for process in processes:
            try:
                process.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2.0)
        self._processes.clear()
        self.status_var.set(
            "All launcher-owned components stopped; existing registry left running"
            if self._external_registry
            else "All launcher-owned components stopped"
        )

    def _poll(self) -> None:
        running = tuple(
            key for key, process in self._processes.items() if process.poll() is None
        )
        exited = tuple(
            f"{key} (exit {process.returncode})"
            for key, process in self._processes.items()
            if process.poll() is not None and process.returncode
        )
        visible = [*running]
        if self._external_registry:
            visible.insert(0, "registry (existing)")
        if self._registry_blocked:
            self.status_var.set(self._registry_blocked)
        elif visible:
            suffix = f"; failed: {', '.join(exited)}" if exited else ""
            self.status_var.set("Running: " + ", ".join(visible) + suffix)
        elif exited:
            self.status_var.set("Failed: " + ", ".join(exited))
        else:
            self.status_var.set("All components stopped")
        self.after(500, self._poll)

    def _close(self) -> None:
        # Processes are deliberately independent; use Stop all when termination is wanted.
        self.destroy()


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="Launch BeanoFlight simulation processes"
    )
    result.add_argument("recording", nargs="?", type=Path)
    return result


def main(argv: Sequence[str] | None = None) -> None:
    arguments = parser().parse_args(argv)
    SimulationLauncherApp(arguments.recording).mainloop()


if __name__ == "__main__":
    main()
