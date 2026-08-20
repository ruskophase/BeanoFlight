"""Tk monitor and controls for the asynchronous mock inferencer."""

from __future__ import annotations

import argparse
import queue
import signal
import threading
import tkinter as tk
from collections.abc import Sequence
from dataclasses import replace
from tkinter import messagebox, ttk

import cv2
from PIL import Image, ImageTk

from .classification_transport import DEFAULT_DIRECT_EVIDENCE_ENDPOINT
from .inference_transport import DEFAULT_CROP_ENDPOINT
from .mock_inference import (
    DEFAULT_STEREO_LATENCY_CURVE,
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
    def __init__(
        self,
        registry_endpoint: str,
        crop_endpoint: str,
        classification_endpoint: str,
        *,
        show_crop: bool = True,
        show_activity: bool = True,
    ) -> None:
        super().__init__(className="Mock Inferencer")
        self.title("Mock Inferencer")
        self.iconname("Mock Inferencer")
        self.geometry("1180x720")
        self.minsize(980, 600)
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.registry_endpoint = registry_endpoint
        self.crop_endpoint = crop_endpoint
        self.classification_endpoint = classification_endpoint
        self.service: MockInferencerService | None = None
        self._closing = False
        self._activities: queue.Queue[MockInferenceActivity] = queue.Queue(maxsize=128)
        self._photo = None
        self._crop_display_enabled = threading.Event()
        self._activity_display_enabled = threading.Event()

        defaults = MockInferenceSettings()
        self.views_var = tk.StringVar(value=str(defaults.views_per_bean))
        self.max_batch_var = tk.StringVar(value=str(defaults.max_batch_beans))
        self.result_deadline_var = tk.StringVar(value=str(defaults.result_deadline_ms))
        self.latency_curve_var = tk.StringVar(
            value=_format_latency_curve(defaults.latency_curve)
        )
        self.jitter_percent_var = tk.StringVar(
            value=str(defaults.jitter_fraction * 100)
        )
        self.tail_probability_percent_var = tk.StringVar(
            value=str(defaults.tail_probability * 100)
        )
        self.tail_min_var = tk.StringVar(value=str(defaults.tail_latency_min_ms))
        self.tail_max_var = tk.StringVar(value=str(defaults.tail_latency_max_ms))
        self.seed_var = tk.StringVar(value=str(defaults.seed))
        self.categories_var = tk.StringVar(value=",".join(defaults.categories))
        self.weights_var = tk.StringVar(
            value=",".join(str(value) for value in defaults.weights)
        )
        self.status_var = tk.StringVar(value="Stopped")
        self.counts_var = tk.StringVar(
            value="received 0 · completed 0 · batches 0 · dropped 0"
        )
        self.batch_stats_var = tk.StringVar(
            value="mean batch 0.0 · mean queue 0.0 ms · mean service 0.0 ms"
        )
        self.show_crop_var = tk.BooleanVar(value=show_crop)
        self.show_activity_var = tk.BooleanVar(value=show_activity)
        self._build()
        self._display_options_changed()
        self.after(50 if show_crop or show_activity else 500, self._poll)
        self.after(100, self.start_service)

    def _build(self) -> None:
        controls = ttk.LabelFrame(
            self, text="Conservative stereo ResNet18 simulation", padding=10
        )
        controls.pack(fill=tk.X, padx=10, pady=10)
        fields = (
            ("Max pairs/frame batch", self.max_batch_var, 10),
            ("Result SLA ms", self.result_deadline_var, 10),
            ("Views/bean", self.views_var, 8),
            ("Jitter %", self.jitter_percent_var, 8),
            ("Tail chance %", self.tail_probability_percent_var, 10),
            ("Tail min ms", self.tail_min_var, 9),
            ("Tail max ms", self.tail_max_var, 9),
        )
        for column, (label, variable, width) in enumerate(fields):
            ttk.Label(controls, text=label).grid(row=0, column=column, sticky=tk.W)
            ttk.Entry(controls, textvariable=variable, width=width).grid(
                row=1, column=column, padx=(0, 8), sticky=tk.EW
            )
        ttk.Label(controls, text="Latency curve (images:ms)").grid(
            row=2, column=0, columnspan=3, sticky=tk.W, pady=(8, 0)
        )
        ttk.Label(controls, text="Seed").grid(
            row=2, column=3, sticky=tk.W, pady=(8, 0)
        )
        ttk.Label(controls, text="Categories").grid(
            row=2, column=4, columnspan=2, sticky=tk.W, pady=(8, 0)
        )
        ttk.Label(controls, text="Weights").grid(
            row=2, column=6, columnspan=2, sticky=tk.W, pady=(8, 0)
        )
        ttk.Entry(controls, textvariable=self.latency_curve_var).grid(
            row=3, column=0, columnspan=3, padx=(0, 8), sticky=tk.EW
        )
        ttk.Entry(controls, textvariable=self.seed_var, width=8).grid(
            row=3, column=3, padx=(0, 8), sticky=tk.EW
        )
        ttk.Entry(controls, textvariable=self.categories_var).grid(
            row=3, column=4, columnspan=2, padx=(0, 8), sticky=tk.EW
        )
        ttk.Entry(controls, textvariable=self.weights_var).grid(
            row=3, column=6, columnspan=2, padx=(0, 8), sticky=tk.EW
        )
        ttk.Checkbutton(
            controls,
            text="Show latest crop (uses extra CPU)",
            variable=self.show_crop_var,
            command=self._display_options_changed,
        ).grid(row=4, column=0, columnspan=2, sticky=tk.W, pady=(10, 0))
        ttk.Checkbutton(
            controls,
            text="Show activity log",
            variable=self.show_activity_var,
            command=self._display_options_changed,
        ).grid(row=4, column=2, columnspan=2, sticky=tk.W, pady=(10, 0))
        ttk.Label(
            controls,
            text="CamL is transported today; CamR compute and fusion cost are simulated.",
        ).grid(row=4, column=4, columnspan=2, sticky=tk.W, pady=(10, 0))
        ttk.Button(controls, text="Start / apply", command=self.start_service).grid(
            row=4, column=6, padx=(8, 4), pady=(8, 0), sticky=tk.E
        )
        ttk.Button(controls, text="Stop", command=self.stop_service).grid(
            row=4, column=7, pady=(8, 0), sticky=tk.W
        )
        for column in range(8):
            controls.columnconfigure(column, weight=1)

        body = ttk.Panedwindow(self, orient=tk.HORIZONTAL)
        body.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 10))
        image_frame = ttk.LabelFrame(
            body, text="Latest transported crop (CamL)", padding=8
        )
        event_frame = ttk.LabelFrame(body, text="Activity", padding=8)
        body.add(image_frame, weight=2)
        body.add(event_frame, weight=3)
        self.image_label = ttk.Label(image_frame, anchor=tk.CENTER)
        self.image_label.pack(fill=tk.BOTH, expand=True)
        self.activity = tk.Text(event_frame, state=tk.DISABLED, wrap=tk.NONE)
        self.activity.pack(fill=tk.BOTH, expand=True)

        footer = ttk.Frame(self, padding=(10, 0, 10, 10))
        footer.pack(fill=tk.X)
        ttk.Label(footer, textvariable=self.status_var).pack(fill=tk.X, anchor=tk.W)
        ttk.Label(footer, textvariable=self.counts_var).pack(fill=tk.X, anchor=tk.W)
        ttk.Label(footer, textvariable=self.batch_stats_var).pack(
            fill=tk.X, anchor=tk.W
        )

    def start_service(self) -> None:
        try:
            categories = tuple(
                item.strip()
                for item in self.categories_var.get().split(",")
                if item.strip()
            )
            weights = tuple(float(item) for item in self.weights_var.get().split(","))
            settings = MockInferenceSettings(
                views_per_bean=int(self.views_var.get()),
                max_batch_beans=int(self.max_batch_var.get()),
                result_deadline_ms=float(self.result_deadline_var.get()),
                latency_curve=_parse_latency_curve(self.latency_curve_var.get()),
                jitter_fraction=float(self.jitter_percent_var.get()) / 100.0,
                tail_probability=(
                    float(self.tail_probability_percent_var.get()) / 100.0
                ),
                tail_latency_min_ms=float(self.tail_min_var.get()),
                tail_latency_max_ms=float(self.tail_max_var.get()),
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
            classification_endpoint=self.classification_endpoint,
            settings=settings,
            activity=self._post_activity,
        )
        self.service.start()
        self.status_var.set(
            f"Starting · crops {self.crop_endpoint} · direct results "
            f"{self.classification_endpoint}"
        )

    def stop_service(self) -> None:
        service = self.service
        self.service = None
        if service is not None:
            self.status_var.set("Stopping · draining Registry audits…")
            service.close(drain=True)
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
            if item.kind == "batch":
                message = f"{item.kind:10} {item.batch_id}"
            else:
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
            stats = service.statistics()
            self.counts_var.set(
                f"received {stats['received']} · completed {stats['completed']} · "
                f"batches {stats['batches']} · queued {stats['queued']} · "
                f"audits pending {stats['registry_audits_pending']} · "
                f"audit retries {stats['registry_completion_retries']} · "
                f"dropped {stats['dropped']} · direct {stats['direct_evidence_sent']} · "
                f"direct dropped {stats['direct_evidence_dropped']}"
            )
            self.batch_stats_var.set(
                f"mean batch {stats['mean_batch_size']:.1f} · "
                f"max batch {stats['max_batch_size']} · "
                f"mean queue {stats['mean_queue_ms']:.1f} ms · "
                f"service {stats['mean_service_ms']:.1f} ms · "
                f"SLA misses {stats['deadline_misses']} · "
                f"tails {stats['tail_batches']}"
            )
            if service.startup_error:
                self.status_var.set(f"Error · {service.startup_error}")
            elif service.ready.is_set():
                self.status_var.set(
                    "Running · source-frame logical stereo batching · "
                    f"crops {service.crop_endpoint} · direct results "
                    f"{service.classification_endpoint}"
                )
        delay_ms = (
            50
            if self._crop_display_enabled.is_set()
            or self._activity_display_enabled.is_set()
            else 500
        )
        self.after(delay_ms, self._poll)

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
        if self._closing:
            return
        self._closing = True
        self.stop_service()
        self.destroy()

    def request_close(self) -> None:
        """Schedule a graceful close from a POSIX signal handler."""

        if not self._closing:
            self.after_idle(self._close)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="BeanoFlight mock inference GUI")
    result.add_argument("--registry", default=DEFAULT_COMMAND_ENDPOINT)
    result.add_argument("--crops", default=DEFAULT_CROP_ENDPOINT)
    result.add_argument(
        "--classifications", default=DEFAULT_DIRECT_EVIDENCE_ENDPOINT
    )
    result.add_argument(
        "--no-crop-preview",
        action="store_true",
        help="start with crop conversion and display disabled",
    )
    result.add_argument(
        "--no-activity-log",
        action="store_true",
        help="start with inference activity rendering disabled",
    )
    return result


def _format_latency_curve(curve: tuple[tuple[int, float], ...]) -> str:
    return ",".join(f"{images}:{latency:g}" for images, latency in curve)


def _parse_latency_curve(value: str) -> tuple[tuple[int, float], ...]:
    try:
        curve = tuple(
            (int(images.strip()), float(latency.strip()))
            for item in value.split(",")
            if item.strip()
            for images, latency in (item.split(":", maxsplit=1),)
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "latency curve must use comma-separated image:milliseconds pairs"
        ) from exc
    if not curve:
        return DEFAULT_STEREO_LATENCY_CURVE
    return curve


def main(argv: Sequence[str] | None = None) -> None:
    arguments = parser().parse_args(argv)
    app = MockInferencerApp(
        arguments.registry,
        arguments.crops,
        arguments.classifications,
        show_crop=not arguments.no_crop_preview,
        show_activity=not arguments.no_activity_log,
    )

    def request_stop(_signum, _frame) -> None:
        app.request_close()

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)
    app.mainloop()


if __name__ == "__main__":
    main()
