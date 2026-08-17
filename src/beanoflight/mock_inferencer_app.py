"""Tk monitor and controls for the asynchronous mock inferencer."""

from __future__ import annotations

import argparse
import queue
import threading
import tkinter as tk
from collections.abc import Sequence
from dataclasses import replace
from tkinter import messagebox, ttk

import cv2
from PIL import Image, ImageTk

from .inference_transport import DEFAULT_CROP_ENDPOINT
from .mock_inference import (
    MockInferenceActivity,
    MockInferencerService,
    MockInferenceSettings,
)
from .registry_service import DEFAULT_COMMAND_ENDPOINT

RESAMPLE = getattr(Image, "Resampling", Image).LANCZOS


def crop_preview_image(image_bgr) -> Image.Image:
    rgb = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2RGB)
    image = Image.fromarray(rgb)
    image.thumbnail((440, 440), RESAMPLE)
    return image


class MockInferencerApp(tk.Tk):
    def __init__(self, registry_endpoint: str, crop_endpoint: str) -> None:
        super().__init__(className="Mock Inferencer")
        self.title("Mock Inferencer")
        self.iconname("Mock Inferencer")
        self.geometry("1050x700")
        self.minsize(850, 580)
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.registry_endpoint = registry_endpoint
        self.crop_endpoint = crop_endpoint
        self.service: MockInferencerService | None = None
        self._activities: queue.Queue[MockInferenceActivity] = queue.Queue(maxsize=128)
        self._photo = None
        self._crop_display_enabled = threading.Event()
        self._crop_display_enabled.set()
        self._activity_display_enabled = threading.Event()
        self._activity_display_enabled.set()

        defaults = MockInferenceSettings()
        self.latency_var = tk.StringVar(value=str(defaults.latency_ms))
        self.jitter_var = tk.StringVar(value=str(defaults.jitter_ms))
        self.workers_var = tk.StringVar(value=str(defaults.worker_count))
        self.seed_var = tk.StringVar(value=str(defaults.seed))
        self.categories_var = tk.StringVar(value=",".join(defaults.categories))
        self.weights_var = tk.StringVar(
            value=",".join(str(value) for value in defaults.weights)
        )
        self.status_var = tk.StringVar(value="Stopped")
        self.counts_var = tk.StringVar(value="received 0 · completed 0 · dropped 0")
        self.show_crop_var = tk.BooleanVar(value=True)
        self.show_activity_var = tk.BooleanVar(value=True)
        self._build()
        self.after(50, self._poll)
        self.after(100, self.start_service)

    def _build(self) -> None:
        controls = ttk.LabelFrame(self, text="Simulation settings", padding=10)
        controls.pack(fill=tk.X, padx=10, pady=10)
        fields = (
            ("Latency ms", self.latency_var, 8),
            ("Jitter ms", self.jitter_var, 8),
            ("Workers", self.workers_var, 6),
            ("Seed", self.seed_var, 8),
            ("Categories", self.categories_var, 32),
            ("Weights", self.weights_var, 24),
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
            text="Show latest crop (uses extra CPU)",
            variable=self.show_crop_var,
            command=self._display_options_changed,
        ).grid(row=2, column=0, columnspan=3, sticky=tk.W, pady=(8, 0))
        ttk.Checkbutton(
            controls,
            text="Show activity log",
            variable=self.show_activity_var,
            command=self._display_options_changed,
        ).grid(row=2, column=3, columnspan=3, sticky=tk.W, pady=(8, 0))

        body = ttk.Panedwindow(self, orient=tk.HORIZONTAL)
        body.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 10))
        image_frame = ttk.LabelFrame(body, text="Latest lossless crop", padding=8)
        event_frame = ttk.LabelFrame(body, text="Activity", padding=8)
        body.add(image_frame, weight=2)
        body.add(event_frame, weight=3)
        self.image_label = ttk.Label(image_frame, anchor=tk.CENTER)
        self.image_label.pack(fill=tk.BOTH, expand=True)
        self.activity = tk.Text(event_frame, state=tk.DISABLED, wrap=tk.NONE)
        self.activity.pack(fill=tk.BOTH, expand=True)

        footer = ttk.Frame(self, padding=(10, 0, 10, 10))
        footer.pack(fill=tk.X)
        ttk.Label(footer, textvariable=self.status_var).pack(side=tk.LEFT)
        ttk.Label(footer, textvariable=self.counts_var).pack(side=tk.RIGHT)

    def start_service(self) -> None:
        try:
            categories = tuple(
                item.strip()
                for item in self.categories_var.get().split(",")
                if item.strip()
            )
            weights = tuple(float(item) for item in self.weights_var.get().split(","))
            settings = MockInferenceSettings(
                latency_ms=float(self.latency_var.get()),
                jitter_ms=float(self.jitter_var.get()),
                worker_count=int(self.workers_var.get()),
                seed=int(self.seed_var.get()),
                categories=categories,
                weights=weights,
            )
            settings.validate()
        except ValueError as exc:
            messagebox.showerror("Mock inferencer settings", str(exc), parent=self)
            return
        self.stop_service()
        self.service = MockInferencerService(
            registry_endpoint=self.registry_endpoint,
            crop_endpoint=self.crop_endpoint,
            settings=settings,
            activity=self._post_activity,
        )
        self.service.start()
        self.status_var.set(
            f"Starting · crops {self.crop_endpoint} · registry {self.registry_endpoint}"
        )

    def stop_service(self) -> None:
        service = self.service
        self.service = None
        if service is not None:
            service.close(drain=False)
        self.status_var.set("Stopped")

    def _post_activity(self, activity: MockInferenceActivity) -> None:
        if activity.crop is not None and not self._crop_display_enabled.is_set():
            activity = replace(activity, crop=None)
        if activity.crop is None and not self._activity_display_enabled.is_set():
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
            message = f"{item.kind:10} {item.bean_id} {item.category}"
            if item.confidence is not None:
                message += f" {item.confidence:.1%}"
            if item.detail:
                message += f" · {item.detail}"
            if item.crop is not None and self._crop_display_enabled.is_set():
                try:
                    self._show_crop(item.crop)
                except Exception as exc:  # noqa: BLE001 - keep activity polling alive
                    message += f" · preview error: {exc}"
            if self._activity_display_enabled.is_set():
                self.activity.configure(state=tk.NORMAL)
                self.activity.insert("1.0", message + "\n")
                if int(self.activity.index("end-1c").split(".")[0]) > 300:
                    self.activity.delete("300.0", tk.END)
                self.activity.configure(state=tk.DISABLED)
        service = self.service
        if service is not None:
            self.counts_var.set(
                f"received {service.received} · completed {service.completed} · "
                f"dropped {service.dropped} · queued {service._queue.qsize()}"
            )
            if service.startup_error:
                self.status_var.set(f"Error · {service.startup_error}")
            elif service.ready.is_set():
                self.status_var.set(
                    f"Running · crops {service.crop_endpoint} · registry {self.registry_endpoint}"
                )
        self.after(50, self._poll)

    def _show_crop(self, image_bgr) -> None:
        image = crop_preview_image(image_bgr)
        self._photo = ImageTk.PhotoImage(image)
        self.image_label.configure(image=self._photo, text="")

    def _display_options_changed(self) -> None:
        if self.show_crop_var.get():
            self._crop_display_enabled.set()
            self.image_label.configure(text="Waiting for a crop…")
        else:
            self._crop_display_enabled.clear()
            self._photo = None
            self.image_label.configure(image="", text="Crop preview disabled")
        if self.show_activity_var.get():
            self._activity_display_enabled.set()
        else:
            self._activity_display_enabled.clear()

    def _close(self) -> None:
        self.stop_service()
        self.destroy()


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="BeanoFlight mock inference GUI")
    result.add_argument("--registry", default=DEFAULT_COMMAND_ENDPOINT)
    result.add_argument("--crops", default=DEFAULT_CROP_ENDPOINT)
    return result


def main(argv: Sequence[str] | None = None) -> None:
    arguments = parser().parse_args(argv)
    MockInferencerApp(arguments.registry, arguments.crops).mainloop()


if __name__ == "__main__":
    main()
