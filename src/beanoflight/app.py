"""Tk review, OpenCV stage inspection, and asynchronous simulation GUI."""

from __future__ import annotations

import queue
import secrets
import threading
import tkinter as tk
from dataclasses import replace
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import cv2
from PIL import Image, ImageTk

from .analysis import AnalysisEngine, AnalysisRun, analyse_source, export_run_json
from .background import (
    DEFAULT_BACKGROUND_FRAMES_TEXT,
    BackgroundProvenance,
    parse_background_frame_indices,
    stratified_random_candidates,
)
from .calibration import (
    CalibrationError,
    MetricPlaneCalibration,
    find_pinkplane_homography,
)
from .crop import BeanCropSelector, CropSettings
from .detection import (
    BeanDetector,
    DetectorError,
    DetectorSettings,
    RawGreenDetector,
    temporal_median_background,
)
from .display import draw_birth_margins, render_analysis, render_pipeline_stage
from .inference_transport import DEFAULT_CROP_ENDPOINT
from .models import FrameAnalysis, PipelineStage
from .prediction import GateLayout
from .registry_service import DEFAULT_COMMAND_ENDPOINT
from .registry_zmq import ZeroMQRegistryClient
from .replay import CropDispatcher, ReplayRunner, ReplaySettings
from .sorting_context_transport import DEFAULT_SORTING_CONTEXT_ENDPOINT
from .source import (
    MMapRawVideoSource,
    ReplaySource,
    SourceError,
    find_raw_bundle,
    open_replay_source,
)
from .tracking import TrackerSettings

VIDEO_TYPES = [
    ("Matroska video", "*.mkv"),
    ("Video files", "*.mkv *.avi *.mov *.mp4 *.m4v *.webm"),
    ("All files", "*"),
]
RESAMPLE = getattr(Image, "Resampling", Image).BILINEAR


def background_key_action(keysym: str) -> str | None:
    key = keysym.lower()
    if key in {"u", "y"}:
        return "use"
    if key == "n":
        return "skip"
    return None


class ImagePane(ttk.Frame):
    def __init__(self, parent: tk.Misc) -> None:
        super().__init__(parent)
        self.canvas = tk.Canvas(
            self,
            background="#050709",
            highlightthickness=1,
            highlightbackground="#39424e",
            width=1120,
            height=770,
        )
        self.canvas.pack(fill=tk.BOTH, expand=True)
        self.canvas.bind("<Configure>", self._on_resize)
        self._image: Image.Image | None = None
        self._photo: ImageTk.PhotoImage | None = None
        self._after: str | None = None

    def set_bgr(self, image_bgr) -> None:
        self._image = Image.fromarray(cv2.cvtColor(image_bgr, cv2.COLOR_BGR2RGB))
        self._redraw()

    def clear(self) -> None:
        self._image = None
        self._photo = None
        self.canvas.delete("all")
        self.canvas.create_text(
            max(20, self.canvas.winfo_width() // 2),
            max(20, self.canvas.winfo_height() // 2),
            text="Open a BeanoFastCap CamL recording",
            fill="#aab3bd",
            font=("TkDefaultFont", 15),
        )

    def _on_resize(self, _event=None) -> None:
        if self._after is not None:
            self.after_cancel(self._after)
        self._after = self.after(30, self._redraw)

    def _redraw(self) -> None:
        self._after = None
        if self._image is None:
            self.clear()
            return
        width = max(2, self.canvas.winfo_width())
        height = max(2, self.canvas.winfo_height())
        scale = min(width / self._image.width, height / self._image.height)
        draw_size = (
            max(1, round(self._image.width * scale)),
            max(1, round(self._image.height * scale)),
        )
        resized = self._image.resize(draw_size, RESAMPLE)
        self._photo = ImageTk.PhotoImage(resized)
        self.canvas.delete("all")
        self.canvas.create_image(
            (width - draw_size[0]) // 2,
            (height - draw_size[1]) // 2,
            image=self._photo,
            anchor=tk.NW,
        )


class BackgroundSelectionDialog(tk.Toplevel):
    """Collect human-confirmed empty frames from stratified random candidates."""

    def __init__(
        self,
        parent: tk.Misc,
        source: ReplaySource,
        *,
        requested_frames: int = 3,
    ) -> None:
        super().__init__(parent)
        self.title("BeanoFlight — choose empty background frames")
        self.geometry("1280x850")
        self.minsize(900, 650)
        self.transient(parent)
        self.source = source
        self.target = min(max(1, requested_frames), source.metadata.frame_count)
        self.seed = secrets.randbits(63)
        self.candidates = stratified_random_candidates(
            source.metadata.frame_count,
            self.target,
            candidates_per_stratum=4,
            seed=self.seed,
        )
        self.position = 0
        self.accepted: list[int] = []
        self.result: tuple[tuple[int, ...], int] | None = None
        self.detail_var = tk.StringVar()
        self.protocol("WM_DELETE_WINDOW", self._cancel)

        heading = ttk.Frame(self, padding=(12, 10))
        heading.pack(fill=tk.X)
        ttk.Label(
            heading,
            text="Does this frame contain any bean or other foreground object?",
            style="Heading.TLabel",
        ).pack(anchor=tk.W)
        ttk.Label(
            heading,
            textvariable=self.detail_var,
            style="Muted.TLabel",
        ).pack(anchor=tk.W, pady=(4, 0))
        self.image_pane = ImagePane(self)
        self.image_pane.pack(fill=tk.BOTH, expand=True, padx=12)
        buttons = ttk.Frame(self, padding=12)
        buttons.pack(fill=tk.X)
        ttk.Button(
            buttons,
            text="Empty — use this frame [U/Y]",
            command=self._accept,
        ).pack(side=tk.LEFT, expand=True, fill=tk.X)
        ttk.Button(
            buttons,
            text="Contains foreground — skip [N]",
            command=self._reject,
        ).pack(side=tk.LEFT, expand=True, fill=tk.X, padx=8)
        ttk.Button(buttons, text="Cancel", command=self._cancel).pack(side=tk.RIGHT)
        self.bind("<KeyPress>", self._key_pressed)
        self.grab_set()
        self.after_idle(self.focus_force)
        self._show_candidate()

    def _key_pressed(self, event: tk.Event) -> None:
        action = background_key_action(str(event.keysym))
        if action == "use":
            self._accept()
        elif action == "skip":
            self._reject()

    def wait_for_result(self) -> tuple[tuple[int, ...], int] | None:
        self.wait_window()
        return self.result

    def _show_candidate(self) -> None:
        if self.position >= len(self.candidates):
            self._finish_exhausted()
            return
        index = self.candidates[self.position]
        try:
            frame = self.source.frame(index)
        except SourceError as exc:
            messagebox.showerror("Background selection", str(exc), parent=self)
            self._cancel()
            return
        stage = PipelineStage(
            "background_candidate",
            "Background candidate — human decision required",
            frame,
            (
                f"video_frame={index + 1} of {self.source.metadata.frame_count}",
                f"candidate={self.position + 1} of {len(self.candidates)}",
                f"confirmed_empty={len(self.accepted)} of {self.target}",
            ),
            "Accept only a frame containing no bean or other transient foreground object.",
        )
        self.image_pane.set_bgr(render_pipeline_stage(stage))
        self.detail_var.set(
            f"Candidate video frame {index + 1:,}. "
            f"Confirmed empty: {len(self.accepted)} / {self.target}. "
            "Keyboard shortcuts (upper or lower case): U/Y = use, N = do not use."
        )

    def _accept(self) -> None:
        if self.position >= len(self.candidates):
            return
        self.accepted.append(self.candidates[self.position])
        if len(self.accepted) >= self.target:
            self.result = (tuple(self.accepted), self.seed)
            self.destroy()
            return
        self.position += 1
        self._show_candidate()

    def _reject(self) -> None:
        if self.position >= len(self.candidates):
            return
        self.position += 1
        self._show_candidate()

    def _finish_exhausted(self) -> None:
        if self.accepted and messagebox.askyesno(
            "Background selection",
            f"Only {len(self.accepted)} empty frames were confirmed. Use those frames?",
            parent=self,
        ):
            self.result = (tuple(self.accepted), self.seed)
        self.destroy()

    def _cancel(self) -> None:
        self.result = None
        self.destroy()


class BeanoFlightApp(tk.Tk):
    def __init__(
        self,
        *,
        initial_path: Path | None = None,
        homography_path: Path | None = None,
        hole_pitch_mm: float = 9.16,
        sorting_offset_mm: float = 30.0,
        performance_mode: bool = False,
        sorting_context_endpoint: str = DEFAULT_SORTING_CONTEXT_ENDPOINT,
    ) -> None:
        super().__init__(className="BeanoFlight")
        self.title("BeanoFlight")
        self.iconname("BeanoFlight")
        self.geometry("1800x920")
        self.minsize(1350, 760)
        self.configure(background="#12161b")
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.hole_pitch_mm = hole_pitch_mm
        self.sorting_offset_mm = sorting_offset_mm
        self.explicit_homography = homography_path
        self.performance_mode = bool(performance_mode)
        self.sorting_context_endpoint = sorting_context_endpoint

        self.source: ReplaySource | None = None
        self.source_prefer_raw = False
        self.calibration: MetricPlaneCalibration | None = None
        self.background = None
        self.background_provenance = BackgroundProvenance("none", ())
        self.detector_settings = DetectorSettings()
        self.tracker_settings = TrackerSettings()
        self.run: AnalysisRun | None = None
        self.current_index = 0
        self.pipeline_stages: tuple[PipelineStage, ...] = ()
        self.stage_index = 0
        self._worker: threading.Thread | None = None
        self._generation = 0
        self._stop = threading.Event()
        self._pause = threading.Event()
        self._control_queue: queue.Queue[tuple] = queue.Queue()
        self._display_queue: queue.Queue[tuple[int, object, FrameAnalysis]] = (
            queue.Queue(maxsize=2)
        )
        self._play_after: str | None = None
        self._playing = False

        self.status_var = tk.StringVar(value="Open a CamL recording to begin.")
        self.source_var = tk.StringVar(value="No recording loaded")
        self.calibration_var = tk.StringVar(value="No metric calibration")
        self.mode_var = tk.StringVar(
            value="Simulation" if self.performance_mode else "Review"
        )
        self.inspector_var = tk.BooleanVar(value=False)
        self.stage_var = tk.StringVar(value="Pipeline inspector disabled")
        self.stage_explanation_var = tk.StringVar(value="")
        self.frame_var = tk.IntVar(value=0)
        self._setting_vars: dict[str, tk.StringVar] = {}
        self.left_margin_var = tk.StringVar(
            value=str(self.tracker_settings.left_birth_margin_px)
        )
        self.right_margin_var = tk.StringVar(
            value=str(self.tracker_settings.right_birth_margin_px)
        )
        self.target_fps_var = tk.StringVar(value="60")
        self.fast_raw_var = tk.BooleanVar(value=True)
        self.crop_processing_var = tk.StringVar(value="ml-fast")
        self.preview_enabled_var = tk.BooleanVar(value=False)
        self.prebuffer_enabled_var = tk.BooleanVar(value=True)
        self.prebuffer_frames_var = tk.StringVar(value="60")
        self.maximum_frames_var = tk.StringVar(value="1000")
        self.crop_size_var = tk.StringVar(value="224")
        self.crops_per_bean_var = tk.StringVar(value="2")
        self.adaptive_edge_resize_var = tk.BooleanVar(value=True)
        self.drop_stale_frames_var = tk.BooleanVar(value=True)
        self.maximum_frame_age_var = tk.StringVar(value="30")
        self.background_frames_var = tk.StringVar(
            value=DEFAULT_BACKGROUND_FRAMES_TEXT
        )
        self.registry_endpoint_var = tk.StringVar(value=DEFAULT_COMMAND_ENDPOINT)
        self.inference_endpoint_var = tk.StringVar(value=DEFAULT_CROP_ENDPOINT)

        self._configure_styles()
        self._build_layout()
        self.bind("<Left>", lambda _event: self.step_frame(-1))
        self.bind("<Right>", lambda _event: self.step_frame(1))
        self.bind("<space>", lambda _event: self.toggle_run())
        self.after(100 if self.performance_mode else 40, self._poll_workers)
        if initial_path is not None:
            self.after(100, lambda: self.load_path(initial_path))

    def _configure_styles(self) -> None:
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("TFrame", background="#171c22")
        style.configure("TLabel", background="#171c22", foreground="#dce3ea")
        style.configure("Muted.TLabel", foreground="#9faab5")
        style.configure("Heading.TLabel", font=("TkDefaultFont", 12, "bold"))
        style.configure("TCheckbutton", background="#171c22", foreground="#dce3ea")
        style.configure("TLabelframe", background="#171c22", foreground="#dce3ea")
        style.configure("TLabelframe.Label", background="#171c22", foreground="#dce3ea")

    def _build_layout(self) -> None:
        toolbar = ttk.Frame(self, padding=(10, 8))
        toolbar.pack(fill=tk.X)
        ttk.Button(toolbar, text="Open CamL video…", command=self.open_video).pack(
            side=tk.LEFT
        )
        ttk.Button(
            toolbar, text="Open recording folder…", command=self.open_folder
        ).pack(side=tk.LEFT, padx=(6, 0))
        ttk.Button(toolbar, text="Open RAW bundle…", command=self.open_raw_folder).pack(
            side=tk.LEFT, padx=(6, 0)
        )
        ttk.Button(
            toolbar, text="Load PinkPlane…", command=self.select_homography
        ).pack(side=tk.LEFT, padx=(6, 12))
        ttk.Label(toolbar, text="Mode").pack(side=tk.LEFT)
        mode = ttk.Combobox(
            toolbar,
            textvariable=self.mode_var,
            values=("Review", "Simulation"),
            width=10,
            state="readonly",
        )
        mode.pack(side=tk.LEFT, padx=(5, 12))
        mode.bind("<<ComboboxSelected>>", lambda _event: self._mode_changed())
        ttk.Button(toolbar, text="Analyse clip", command=self.analyse_clip).pack(
            side=tk.LEFT
        )
        self.run_button = ttk.Button(
            toolbar,
            text="Run" if self.performance_mode else "Play",
            command=self.toggle_run,
        )
        self.run_button.pack(side=tk.LEFT, padx=(6, 0))
        ttk.Button(toolbar, text="Stop", command=self.stop_work).pack(
            side=tk.LEFT, padx=(6, 0)
        )
        ttk.Button(toolbar, text="Export analysis…", command=self.export_analysis).pack(
            side=tk.RIGHT
        )

        details = ttk.Frame(self, padding=(10, 0, 10, 6))
        details.pack(fill=tk.X)
        ttk.Label(details, textvariable=self.source_var, style="Muted.TLabel").pack(
            side=tk.LEFT
        )
        ttk.Label(
            details, textvariable=self.calibration_var, style="Muted.TLabel"
        ).pack(side=tk.RIGHT)

        main = ttk.Panedwindow(self, orient=tk.HORIZONTAL)
        main.pack(fill=tk.BOTH, expand=True, padx=10)
        left = ttk.Frame(main)
        right = ttk.Frame(main, width=430)
        main.add(left, weight=5)
        main.add(right, weight=2)
        self.image_pane = ImagePane(left)
        self.image_pane.pack(fill=tk.BOTH, expand=True)

        navigation = ttk.Frame(left, padding=(0, 7))
        navigation.pack(fill=tk.X)
        ttk.Button(
            navigation, text="|<", width=4, command=lambda: self.set_frame(0)
        ).pack(side=tk.LEFT)
        ttk.Button(
            navigation, text="< Frame", command=lambda: self.step_frame(-1)
        ).pack(side=tk.LEFT, padx=(5, 0))
        ttk.Button(navigation, text="Frame >", command=lambda: self.step_frame(1)).pack(
            side=tk.LEFT, padx=(5, 8)
        )
        self.timeline = ttk.Scale(
            navigation,
            from_=0,
            to=1,
            variable=self.frame_var,
            command=self._timeline_changed,
        )
        self.timeline.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.frame_label = ttk.Label(navigation, text="0 / 0", width=18, anchor=tk.E)
        self.frame_label.pack(side=tk.RIGHT, padx=(8, 0))

        notebook = ttk.Notebook(right)
        notebook.pack(fill=tk.BOTH, expand=True)
        detector_tab = ttk.Frame(notebook, padding=10)
        inspector_tab = ttk.Frame(notebook, padding=10)
        tracks_tab = ttk.Frame(notebook, padding=10)
        simulation_tab = ttk.Frame(notebook, padding=10)
        notebook.add(detector_tab, text="Detector")
        notebook.add(inspector_tab, text="Pipeline steps")
        notebook.add(tracks_tab, text="Tracks & gates")
        notebook.add(simulation_tab, text="Simulation")
        self._build_detector_tab(detector_tab)
        self._build_inspector_tab(inspector_tab)
        self._build_tracks_tab(tracks_tab)
        self._build_simulation_tab(simulation_tab)

        status = ttk.Label(
            self,
            textvariable=self.status_var,
            anchor=tk.W,
            relief=tk.SUNKEN,
            padding=(8, 5),
        )
        status.pack(fill=tk.X, side=tk.BOTTOM)
        self.image_pane.clear()

    def _build_detector_tab(self, parent: ttk.Frame) -> None:
        ttk.Label(
            parent, text="Static-background detector", style="Heading.TLabel"
        ).grid(row=0, column=0, columnspan=3, sticky=tk.W, pady=(0, 8))
        controls = (
            (
                "processing_scale",
                "Processing scale",
                self.detector_settings.processing_scale,
                0.25,
                1.0,
                0.05,
            ),
            (
                "blur_kernel",
                "Gaussian blur kernel",
                self.detector_settings.blur_kernel,
                1,
                31,
                2,
            ),
            (
                "threshold",
                "Difference threshold",
                self.detector_settings.threshold,
                0,
                255,
                1,
            ),
            (
                "close_kernel",
                "Close kernel",
                self.detector_settings.close_kernel,
                1,
                31,
                2,
            ),
            (
                "close_iterations",
                "Close iterations",
                self.detector_settings.close_iterations,
                0,
                10,
                1,
            ),
            (
                "open_kernel",
                "Open kernel",
                self.detector_settings.open_kernel,
                1,
                31,
                2,
            ),
            (
                "open_iterations",
                "Open iterations",
                self.detector_settings.open_iterations,
                0,
                10,
                1,
            ),
            (
                "dilate_kernel",
                "Dilate kernel",
                self.detector_settings.dilate_kernel,
                1,
                31,
                2,
            ),
            (
                "dilate_iterations",
                "Dilate iterations",
                self.detector_settings.dilate_iterations,
                0,
                10,
                1,
            ),
            (
                "min_area_px",
                "Minimum area (px)",
                self.detector_settings.min_area_px,
                1,
                100_000,
                25,
            ),
            (
                "max_area_px",
                "Maximum area (px)",
                self.detector_settings.max_area_px,
                2,
                200_000,
                100,
            ),
            (
                "min_width_px",
                "Minimum width (px)",
                self.detector_settings.min_width_px,
                1,
                500,
                1,
            ),
            (
                "max_width_px",
                "Maximum width (px)",
                self.detector_settings.max_width_px,
                2,
                1000,
                5,
            ),
            (
                "min_height_px",
                "Minimum height (px)",
                self.detector_settings.min_height_px,
                1,
                500,
                1,
            ),
            (
                "max_height_px",
                "Maximum height (px)",
                self.detector_settings.max_height_px,
                2,
                1000,
                5,
            ),
            (
                "min_solidity",
                "Minimum solidity",
                self.detector_settings.min_solidity,
                0.0,
                1.0,
                0.01,
            ),
        )
        for row, (name, label, value, low, high, increment) in enumerate(
            controls, start=1
        ):
            variable = tk.StringVar(value=str(value))
            self._setting_vars[name] = variable
            ttk.Label(parent, text=label).grid(row=row, column=0, sticky=tk.W, pady=2)
            ttk.Spinbox(
                parent,
                textvariable=variable,
                from_=low,
                to=high,
                increment=increment,
                width=12,
            ).grid(row=row, column=1, sticky=tk.E, pady=2)
        button_row = len(controls) + 1
        ttk.Button(parent, text="Apply settings", command=self.apply_settings).grid(
            row=button_row, column=0, sticky=tk.EW, pady=(10, 4)
        )
        ttk.Button(parent, text="Reset defaults", command=self.reset_settings).grid(
            row=button_row, column=1, sticky=tk.EW, padx=(5, 0), pady=(10, 4)
        )
        ttk.Separator(parent).grid(
            row=button_row + 1, column=0, columnspan=2, sticky=tk.EW, pady=10
        )
        ttk.Button(
            parent,
            text="Use current frame as background",
            command=self.use_current_background,
        ).grid(row=button_row + 2, column=0, columnspan=2, sticky=tk.EW, pady=3)
        ttk.Label(parent, text="Background frames (3, zero-based)").grid(
            row=button_row + 3, column=0, columnspan=2, sticky=tk.W, pady=(8, 2)
        )
        ttk.Entry(parent, textvariable=self.background_frames_var).grid(
            row=button_row + 4, column=0, sticky=tk.EW, pady=3
        )
        ttk.Button(
            parent,
            text="Build entered frames",
            command=self.build_manual_background,
        ).grid(row=button_row + 4, column=1, sticky=tk.EW, padx=(5, 0), pady=3)
        ttk.Button(
            parent,
            text="Choose 3 empty frames for background…",
            command=self.build_guided_background,
        ).grid(row=button_row + 5, column=0, columnspan=2, sticky=tk.EW, pady=3)
        ttk.Label(
            parent,
            text=(
                "Changing a detector setting invalidates existing track IDs. "
                "Reanalyse the clip after the frozen-frame mask looks correct."
            ),
            wraplength=360,
            style="Muted.TLabel",
        ).grid(row=button_row + 6, column=0, columnspan=2, sticky=tk.W, pady=(12, 0))
        parent.columnconfigure(0, weight=1)

    def _build_inspector_tab(self, parent: ttk.Frame) -> None:
        ttk.Checkbutton(
            parent,
            text="Inspect frozen frame step-by-step",
            variable=self.inspector_var,
            command=self.toggle_inspector,
        ).pack(anchor=tk.W)
        ttk.Separator(parent).pack(fill=tk.X, pady=10)
        ttk.Label(
            parent, textvariable=self.stage_var, style="Heading.TLabel", wraplength=370
        ).pack(anchor=tk.W)
        ttk.Label(
            parent,
            textvariable=self.stage_explanation_var,
            wraplength=370,
            style="Muted.TLabel",
        ).pack(anchor=tk.W, pady=(6, 12))
        row = ttk.Frame(parent)
        row.pack(fill=tk.X)
        ttk.Button(
            row, text="< Previous step", command=lambda: self.step_stage(-1)
        ).pack(side=tk.LEFT, expand=True, fill=tk.X)
        ttk.Button(row, text="Next step >", command=lambda: self.step_stage(1)).pack(
            side=tk.LEFT, expand=True, fill=tk.X, padx=(6, 0)
        )
        ttk.Label(
            parent,
            text=(
                "The caption drawn over every diagnostic image names the OpenCV stage "
                "and lists the exact settings used to produce it. The caption is display-only."
            ),
            wraplength=370,
            style="Muted.TLabel",
        ).pack(anchor=tk.W, pady=(14, 0))

    def _build_tracks_tab(self, parent: ttk.Frame) -> None:
        margins = ttk.LabelFrame(parent, text="New-track side margins", padding=8)
        margins.pack(fill=tk.X, pady=(0, 10))
        ttk.Label(margins, text="Left margin (px)").grid(row=0, column=0, sticky=tk.W)
        ttk.Spinbox(
            margins,
            textvariable=self.left_margin_var,
            from_=0,
            to=700,
            increment=1,
            width=8,
        ).grid(row=0, column=1, padx=(6, 12))
        ttk.Label(margins, text="Right margin (px)").grid(row=0, column=2, sticky=tk.W)
        ttk.Spinbox(
            margins,
            textvariable=self.right_margin_var,
            from_=0,
            to=700,
            increment=1,
            width=8,
        ).grid(row=0, column=3, padx=(6, 0))
        ttk.Button(
            margins, text="Apply margins", command=self.apply_tracking_margins
        ).grid(row=1, column=0, columnspan=4, sticky=tk.EW, pady=(8, 0))
        ttk.Label(
            margins,
            text=(
                "A first bounding box touching either shaded margin is displayed as "
                "EDGE-REJECTED and receives no bean ID."
            ),
            wraplength=380,
            style="Muted.TLabel",
        ).grid(row=2, column=0, columnspan=4, sticky=tk.W, pady=(7, 0))
        columns = ("id", "status", "x", "y", "vx", "vy", "gate", "prob", "eta")
        self.track_tree = ttk.Treeview(
            parent, columns=columns, show="headings", height=14
        )
        headings = {
            "id": "ID",
            "status": "State",
            "x": "x mm",
            "y": "y mm",
            "vx": "vx",
            "vy": "vy",
            "gate": "Gate",
            "prob": "P",
            "eta": "ETA",
        }
        widths = {
            "id": 62,
            "status": 68,
            "x": 52,
            "y": 52,
            "vx": 55,
            "vy": 55,
            "gate": 48,
            "prob": 42,
            "eta": 52,
        }
        for column in columns:
            self.track_tree.heading(column, text=headings[column])
            self.track_tree.column(
                column, width=widths[column], anchor=tk.CENTER, stretch=False
            )
        self.track_tree.pack(fill=tk.BOTH, expand=True)
        self.performance_var = tk.StringVar(value="No analysis")
        ttk.Label(
            parent,
            textvariable=self.performance_var,
            wraplength=380,
            style="Muted.TLabel",
        ).pack(anchor=tk.W, pady=(10, 0))

    def _build_simulation_tab(self, parent: ttk.Frame) -> None:
        ttk.Label(
            parent, text="Asynchronous system replay", style="Heading.TLabel"
        ).grid(row=0, column=0, columnspan=2, sticky=tk.W, pady=(0, 10))
        if self.performance_mode:
            ttk.Label(
                parent,
                text=(
                    "Launcher performance profile active: mmap RAW and prebuffering "
                    "on; live playback off."
                ),
                wraplength=370,
                style="Muted.TLabel",
            ).grid(row=15, column=0, columnspan=2, sticky=tk.W, pady=(12, 0))
        ttk.Label(parent, text="Target processing FPS").grid(
            row=1, column=0, sticky=tk.W, pady=3
        )
        ttk.Combobox(
            parent,
            textvariable=self.target_fps_var,
            values=("1", "5", "15", "30", "45", "60", "Unlimited"),
            width=18,
        ).grid(row=1, column=1, sticky=tk.EW, pady=3)
        ttk.Checkbutton(
            parent,
            text="Use memory-mapped RAW fast path",
            variable=self.fast_raw_var,
        ).grid(row=2, column=0, columnspan=2, sticky=tk.W, pady=5)
        ttk.Label(parent, text="RAW crop processing").grid(
            row=3, column=0, sticky=tk.W, pady=3
        )
        ttk.Combobox(
            parent,
            textvariable=self.crop_processing_var,
            values=("ml-fast", "calibrated"),
            state="readonly",
            width=18,
        ).grid(row=3, column=1, sticky=tk.EW, pady=3)
        ttk.Checkbutton(
            parent,
            text="Show live playback (uses extra CPU)",
            variable=self.preview_enabled_var,
        ).grid(row=4, column=0, columnspan=2, sticky=tk.W, pady=5)
        ttk.Checkbutton(
            parent,
            text="Prebuffer mapped/decoded frames before playback",
            variable=self.prebuffer_enabled_var,
        ).grid(row=5, column=0, columnspan=2, sticky=tk.W, pady=5)
        ttk.Label(parent, text="Replay prebuffer (frames)").grid(
            row=6, column=0, sticky=tk.W, pady=3
        )
        ttk.Spinbox(
            parent,
            textvariable=self.prebuffer_frames_var,
            from_=10,
            to=120,
            width=18,
        ).grid(row=6, column=1, sticky=tk.EW, pady=3)
        ttk.Label(parent, text="Maximum replay frames").grid(
            row=7, column=0, sticky=tk.W, pady=3
        )
        ttk.Spinbox(
            parent,
            textvariable=self.maximum_frames_var,
            from_=1,
            to=1000,
            width=18,
        ).grid(row=7, column=1, sticky=tk.EW, pady=3)
        ttk.Label(parent, text="Square crop size (px)").grid(
            row=8, column=0, sticky=tk.W, pady=3
        )
        ttk.Spinbox(
            parent,
            textvariable=self.crop_size_var,
            from_=32,
            to=1024,
            increment=2,
            width=18,
        ).grid(row=8, column=1, sticky=tk.EW, pady=3)
        ttk.Label(parent, text="Inference samples per bean").grid(
            row=9, column=0, sticky=tk.W, pady=3
        )
        ttk.Spinbox(
            parent,
            textvariable=self.crops_per_bean_var,
            from_=1,
            to=5,
            width=18,
        ).grid(row=9, column=1, sticky=tk.EW, pady=3)
        ttk.Checkbutton(
            parent,
            text="Resize smaller complete crops near frame edge",
            variable=self.adaptive_edge_resize_var,
        ).grid(row=10, column=0, columnspan=2, sticky=tk.W, pady=5)
        ttk.Checkbutton(
            parent,
            text="Drop stale replay frames (live-stream behaviour)",
            variable=self.drop_stale_frames_var,
        ).grid(row=11, column=0, columnspan=2, sticky=tk.W, pady=5)
        ttk.Label(parent, text="Maximum frame age (ms)").grid(
            row=12, column=0, sticky=tk.W, pady=3
        )
        ttk.Spinbox(
            parent,
            textvariable=self.maximum_frame_age_var,
            from_=5,
            to=250,
            width=18,
        ).grid(row=12, column=1, sticky=tk.EW, pady=3)
        ttk.Label(parent, text="Registry command endpoint").grid(
            row=13, column=0, sticky=tk.W, pady=3
        )
        ttk.Entry(parent, textvariable=self.registry_endpoint_var).grid(
            row=13, column=1, sticky=tk.EW, pady=3
        )
        ttk.Label(parent, text="Inference crop endpoint").grid(
            row=14, column=0, sticky=tk.W, pady=3
        )
        ttk.Entry(parent, textvariable=self.inference_endpoint_var).grid(
            row=14, column=1, sticky=tk.EW, pady=3
        )
        ttk.Separator(parent).grid(
            row=15, column=0, columnspan=2, sticky=tk.EW, pady=12
        )
        ttk.Label(
            parent,
            text=(
                "Simulation streams results without retaining the clip. The RAW fast "
                "path buffers compact green planes and colour-processes only crops. "
                "Crops are sent to the external inferencer. Unlimited uses a logical "
                "rather than wall-clock valve schedule."
            ),
            wraplength=390,
            style="Muted.TLabel",
        ).grid(row=16, column=0, columnspan=2, sticky=tk.W)
        parent.columnconfigure(1, weight=1)

    def open_video(self) -> None:
        selected = filedialog.askopenfilename(
            title="Open CamL video", filetypes=VIDEO_TYPES
        )
        if selected:
            self.load_path(Path(selected))

    def open_folder(self) -> None:
        selected = filedialog.askdirectory(title="Open BeanoFastCap recording folder")
        if selected:
            self.load_path(Path(selected))

    def open_raw_folder(self) -> None:
        selected = filedialog.askdirectory(title="Open BeanoFastCap RAW bundle")
        if selected:
            self.load_path(Path(selected), prefer_raw=True)

    def load_path(self, path: Path, *, prefer_raw: bool = False) -> None:
        self.stop_work()
        self._generation += 1
        old_source = self.source
        try:
            source = open_replay_source(path, prefer_raw=prefer_raw, cache_frames=6)
        except SourceError as exc:
            messagebox.showerror("Open recording", str(exc), parent=self)
            return
        if old_source is not None:
            old_source.close()
        self.source = source
        self.source_prefer_raw = source.source_kind == "raw-bundle"
        self.fast_raw_var.set(find_raw_bundle(source.path) is not None)
        self.current_index = 0
        self.run = None
        self.background = source.frame(0).copy()
        self.background_provenance = BackgroundProvenance("temporary first frame", (0,))
        background_status = (
            "Frame 1 is the temporary background; enter three clean frame indices "
            "or use guided selection."
        )
        try:
            indices = self._entered_background_indices()
            self._set_background_from_indices(
                indices,
                method="manually entered temporal median",
            )
            background_status = (
                "Background automatically built from zero-based frames "
                f"{', '.join(str(index) for index in indices)}."
            )
        except (ValueError, SourceError, DetectorError) as exc:
            background_status += f" Default entry was not applied: {exc}"
        self.timeline.configure(to=max(1, source.metadata.frame_count - 1))
        self.frame_var.set(0)
        timestamp_label = (
            "exact FastCap timestamps"
            if source.metadata.exact_timestamps
            else "nominal FPS timestamps"
        )
        self.source_var.set(
            f"{source.path.name} — {source.metadata.width}×{source.metadata.height}, "
            f"{source.metadata.frame_count:,} frames at {source.metadata.fps:.3f} FPS, {timestamp_label}"
        )
        homography = self.explicit_homography or find_pinkplane_homography(source.path)
        self.calibration = None
        if homography is not None:
            self._load_calibration(homography)
        else:
            self.calibration_var.set(
                "No homography.json found — detector inspection only"
            )
        self.status_var.set(background_status)
        self._refresh_display()

    def select_homography(self) -> None:
        selected = filedialog.askopenfilename(
            title="Open PinkPlane v2 homography",
            filetypes=(("JSON calibration", "*.json"), ("All files", "*")),
        )
        if selected:
            self.explicit_homography = Path(selected)
            self._load_calibration(Path(selected))
            self._invalidate_run("Metric calibration changed; reanalyse the clip.")
            self._refresh_display()

    def _load_calibration(self, path: Path) -> None:
        if self.source is None:
            return
        try:
            calibration = MetricPlaneCalibration.from_pinkplane(
                path,
                image_size_px=(self.source.metadata.width, self.source.metadata.height),
                hole_pitch_mm=self.hole_pitch_mm,
            )
        except CalibrationError as exc:
            self.calibration = None
            self.calibration_var.set(f"Metric calibration rejected: {exc}")
            messagebox.showwarning("Metric calibration", str(exc), parent=self)
            return
        self.calibration = calibration
        self.calibration_var.set(
            f"metric plane RMS {calibration.rms_error_mm:.3f} mm; "
            f"sorting line y={calibration.sorting_line_y(self.sorting_offset_mm):.2f} mm"
        )

    def apply_settings(self) -> None:
        try:
            integer_names = {
                "blur_kernel",
                "threshold",
                "close_kernel",
                "close_iterations",
                "open_kernel",
                "open_iterations",
                "dilate_kernel",
                "dilate_iterations",
                "min_area_px",
                "max_area_px",
                "min_width_px",
                "max_width_px",
                "min_height_px",
                "max_height_px",
            }
            values = {
                name: int(variable.get())
                if name in integer_names
                else float(variable.get())
                for name, variable in self._setting_vars.items()
            }
            settings = DetectorSettings(**values)
            settings.validate()
        except (ValueError, DetectorError) as exc:
            messagebox.showerror("Detector settings", str(exc), parent=self)
            return
        self.detector_settings = settings
        self._invalidate_run("Detector settings changed; track IDs require reanalysis.")
        self.inspector_var.set(True)
        self._refresh_inspector()

    def reset_settings(self) -> None:
        defaults = DetectorSettings()
        for name, variable in self._setting_vars.items():
            variable.set(str(getattr(defaults, name)))
        self.apply_settings()

    def use_current_background(self) -> None:
        if self.source is None:
            return
        self.background = self.source.frame(self.current_index).copy()
        self.background_provenance = BackgroundProvenance(
            "human-selected single frame", (self.current_index,)
        )
        self._invalidate_run(
            f"Frame {self.current_index + 1} selected as background; track IDs require reanalysis."
        )
        self._refresh_display()

    def _entered_background_indices(self) -> tuple[int, ...]:
        if self.source is None:
            raise ValueError("open a recording before selecting background frames")
        return parse_background_frame_indices(
            self.background_frames_var.get(),
            frame_count=self.source.metadata.frame_count,
        )

    def _set_background_from_indices(
        self,
        indices: tuple[int, ...],
        *,
        method: str,
        candidate_seed: int | None = None,
    ) -> None:
        if self.source is None:
            raise ValueError("open a recording before selecting background frames")
        frames = [self.source.frame(index) for index in indices]
        self.background = temporal_median_background(frames)
        self.background_provenance = BackgroundProvenance(
            method,
            indices,
            candidate_seed,
        )

    def build_manual_background(self) -> None:
        if self.source is None:
            messagebox.showinfo(
                "Background model", "Open a recording first.", parent=self
            )
            return
        self.stop_work()
        try:
            indices = self._entered_background_indices()
            self._set_background_from_indices(
                indices,
                method="manually entered temporal median",
            )
        except (ValueError, SourceError, DetectorError) as exc:
            messagebox.showerror("Background model", str(exc), parent=self)
            return
        self._invalidate_run(
            "Background built from zero-based frames "
            f"{', '.join(str(index) for index in indices)}; "
            "track IDs require reanalysis."
        )
        self._refresh_display()

    def build_guided_background(self) -> None:
        if self.source is None:
            return
        self.stop_work()
        result = BackgroundSelectionDialog(
            self, self.source, requested_frames=3
        ).wait_for_result()
        if result is None:
            self.status_var.set(
                "Background selection cancelled; existing background retained."
            )
            return
        indices, seed = result
        try:
            self._set_background_from_indices(
                indices,
                method="human-confirmed stratified temporal median",
                candidate_seed=seed,
            )
        except (SourceError, DetectorError) as exc:
            messagebox.showerror("Background model", str(exc), parent=self)
            return
        self.background_frames_var.set(",".join(str(index) for index in indices))
        self._invalidate_run(
            f"Background built from {len(indices)} confirmed-empty frames; "
            "track IDs require reanalysis."
        )
        self._refresh_display()

    def apply_tracking_margins(self) -> None:
        try:
            left = int(self.left_margin_var.get())
            right = int(self.right_margin_var.get())
            settings = replace(
                self.tracker_settings,
                left_birth_margin_px=left,
                right_birth_margin_px=right,
            )
            settings.validate()
            if self.source is not None and left + right >= self.source.metadata.width:
                raise ValueError("left and right margins leave no usable image width")
        except ValueError as exc:
            messagebox.showerror("Tracking margins", str(exc), parent=self)
            return
        self.tracker_settings = settings
        self._invalidate_run("Birth margins changed; track IDs require reanalysis.")
        self._refresh_display()

    def toggle_inspector(self) -> None:
        if self.inspector_var.get():
            self._refresh_inspector()
        else:
            self.pipeline_stages = ()
            self.stage_var.set("Pipeline inspector disabled")
            self.stage_explanation_var.set("")
            self._refresh_display()

    def _refresh_inspector(self) -> None:
        if self.source is None or self.background is None:
            return
        try:
            result = BeanDetector(self.detector_settings).inspect(
                self.source.frame(self.current_index), self.background
            )
        except (SourceError, DetectorError) as exc:
            self.status_var.set(str(exc))
            return
        self.pipeline_stages = result.stages
        self.stage_index = min(self.stage_index, max(0, len(result.stages) - 1))
        self._show_stage()

    def step_stage(self, delta: int) -> None:
        if not self.pipeline_stages:
            self.inspector_var.set(True)
            self._refresh_inspector()
            return
        self.stage_index = max(
            0, min(len(self.pipeline_stages) - 1, self.stage_index + delta)
        )
        self._show_stage()

    def _show_stage(self) -> None:
        if not self.pipeline_stages:
            return
        stage = self.pipeline_stages[self.stage_index]
        self.stage_var.set(
            f"Step {self.stage_index + 1} of {len(self.pipeline_stages)} — {stage.name}"
        )
        self.stage_explanation_var.set(stage.explanation)
        rendered = render_pipeline_stage(stage)
        if stage.key in {"input", "components", "filtered"}:
            draw_birth_margins(
                rendered,
                self.tracker_settings.left_birth_margin_px,
                self.tracker_settings.right_birth_margin_px,
            )
        self.image_pane.set_bgr(rendered)
        self._update_frame_label()

    def analyse_clip(self) -> None:
        if not self._ready_for_analysis():
            return
        if self._worker is not None and self._worker.is_alive():
            self.status_var.set("An analysis is already running.")
            return
        self.stop_work()
        self._stop.clear()
        settings = self.detector_settings
        background = self.background.copy()
        calibration = self.calibration
        source_path = self.source.path
        prefer_raw = self.source_prefer_raw
        generation = self._generation
        tracker_settings = self.tracker_settings
        background_provenance = self.background_provenance

        def worker() -> None:
            source = None
            try:
                source = open_replay_source(
                    source_path, prefer_raw=prefer_raw, cache_frames=1
                )
                layout = GateLayout(calibration.sorting_line_y(self.sorting_offset_mm))
                engine = AnalysisEngine(
                    calibration,
                    BeanDetector(settings),
                    background,
                    tracker_settings=tracker_settings,
                    gate_layout=layout,
                )

                def progress(done: int, total: int, frame: FrameAnalysis) -> None:
                    if done == 1 or done % 10 == 0 or done == total:
                        self._control_queue.put(
                            ("progress", generation, done, total, frame.processing_ms)
                        )

                run = analyse_source(
                    source,
                    engine,
                    stop=self._stop,
                    progress=progress,
                    background_provenance=background_provenance,
                )
                self._control_queue.put(("done", generation, run))
            except Exception as exc:  # noqa: BLE001 - worker reports GUI-safe errors
                self._control_queue.put(("error", generation, str(exc)))
            finally:
                if source is not None:
                    source.close()

        self._worker = threading.Thread(
            target=worker, name="beanoflight-review", daemon=True
        )
        self._worker.start()
        self.status_var.set("Sequential analysis started…")

    def _start_simulation(self) -> None:
        if not self._ready_for_analysis():
            return
        if self._worker is not None and self._worker.is_alive():
            return
        try:
            background_indices = self._entered_background_indices()
            if self.background_provenance.frame_indices != background_indices:
                self._set_background_from_indices(
                    background_indices,
                    method="manually entered temporal median",
                )
        except (ValueError, SourceError, DetectorError) as exc:
            messagebox.showerror("Background model", str(exc), parent=self)
            return
        try:
            fps_text = self.target_fps_var.get().strip().lower()
            target_fps = 0.0 if fps_text == "unlimited" else float(fps_text)
            crop_settings = CropSettings(
                size_px=int(self.crop_size_var.get()),
                max_crops_per_bean=int(self.crops_per_bean_var.get()),
                adaptive_edge_resize=self.adaptive_edge_resize_var.get(),
            )
            crop_settings.validate()
            crop_processing = self.crop_processing_var.get()
            if crop_processing not in {"ml-fast", "calibrated"}:
                raise ValueError("RAW crop processing must be ml-fast or calibrated")
            replay_settings = ReplaySettings(
                target_fps=target_fps,
                preview_enabled=self.preview_enabled_var.get(),
                prebuffer_frames=(
                    int(self.prebuffer_frames_var.get())
                    if self.prebuffer_enabled_var.get()
                    else 0
                ),
                maximum_frames=int(self.maximum_frames_var.get()),
                drop_stale_frames=self.drop_stale_frames_var.get(),
                maximum_frame_age_ms=float(self.maximum_frame_age_var.get()),
            )
            replay_settings.validate()
        except ValueError as exc:
            messagebox.showerror("Simulation settings", str(exc), parent=self)
            return
        use_fast_raw = self.fast_raw_var.get()
        raw_bundle = find_raw_bundle(self.source.path) if use_fast_raw else None
        if use_fast_raw and raw_bundle is None:
            messagebox.showerror(
                "Simulation input",
                "The memory-mapped fast path needs a complete recording bundle "
                "with CamL RAW frames. Turn it off to replay the calibrated video.",
                parent=self,
            )
            return
        self.stop_work()
        self._stop.clear()
        self._pause.clear()
        self.run = None
        settings = self.detector_settings
        background = self.background.copy()
        calibration = self.calibration
        source_path = self.source.path
        prefer_raw = self.source_prefer_raw
        generation = self._generation
        tracker_settings = self.tracker_settings
        registry_endpoint = self.registry_endpoint_var.get().strip()
        inference_endpoint = self.inference_endpoint_var.get().strip()
        background_indices = self.background_provenance.frame_indices

        def worker() -> None:
            source = None
            registry = None
            try:
                if raw_bundle is not None:
                    source = MMapRawVideoSource(
                        raw_bundle,
                        crop_processing=crop_processing,
                    )
                    simulation_background = source.build_background(background_indices)
                    detector = RawGreenDetector(settings)

                    def positions_mapper(points):
                        return calibration.pixels_to_mm(
                            source.undistort_points(points)
                        )

                    deferred_crop_extractor = source.prepare_crop
                else:
                    source = open_replay_source(
                        source_path, prefer_raw=prefer_raw, cache_frames=1
                    )
                    simulation_background = background
                    detector = BeanDetector(settings)
                    positions_mapper = None
                    deferred_crop_extractor = None
                registry = ZeroMQRegistryClient(registry_endpoint, timeout_ms=2_000)
                registry.ping()
                layout = GateLayout(calibration.sorting_line_y(self.sorting_offset_mm))
                engine = AnalysisEngine(
                    calibration,
                    detector,
                    simulation_background,
                    tracker_settings=tracker_settings,
                    gate_layout=layout,
                    registry=registry,
                    positions_mapper=positions_mapper,
                )
                selector = BeanCropSelector(
                    crop_settings,
                    deferred_extractor=deferred_crop_extractor,
                )
                dispatcher = CropDispatcher(
                    registry_endpoint,
                    inference_endpoint,
                    capacity=replay_settings.crop_queue_capacity,
                )
                runner = ReplayRunner(
                    source,
                    engine,
                    registry,
                    settings=replay_settings,
                    crop_selector=selector,
                    crop_dispatcher=dispatcher,
                    sorting_context_endpoint=self.sorting_context_endpoint,
                    profile_metadata={
                        "name": (
                            "launcher-performance"
                            if self.performance_mode
                            else "interactive"
                        ),
                        "launcher_performance_mode": self.performance_mode,
                        "live_playback": replay_settings.preview_enabled,
                        "crop_processing": (
                            source.crop_processing_profile
                            if isinstance(source, MMapRawVideoSource)
                            else "calibrated-video"
                        ),
                        "background": {
                            "method": self.background_provenance.method,
                            "frame_indices": list(
                                self.background_provenance.frame_indices
                            ),
                            "candidate_seed": (
                                self.background_provenance.candidate_seed
                            ),
                        },
                    },
                )

                def preview(frame, result: FrameAnalysis) -> None:
                    item = (generation, frame, result)
                    try:
                        self._display_queue.put_nowait(item)
                    except queue.Full:
                        try:
                            self._display_queue.get_nowait()
                        except queue.Empty:
                            pass
                        try:
                            self._display_queue.put_nowait(item)
                        except queue.Full:
                            pass

                def progress(value) -> None:
                    if value.frame_index == 0 or value.frame_index % 10 == 0:
                        self._control_queue.put(("replay_progress", generation, value))

                def prebuffer_progress(buffered: int, target: int) -> None:
                    self._control_queue.put(
                        ("prebuffer_progress", generation, buffered, target)
                    )

                summary = runner.run(
                    stop=self._stop,
                    paused=self._pause,
                    on_preview=preview,
                    on_progress=progress,
                    on_prebuffer=prebuffer_progress,
                )
                self._control_queue.put(("replay_done", generation, summary))
            except Exception as exc:  # noqa: BLE001 - worker reports GUI-safe errors
                self._control_queue.put(("error", generation, str(exc)))
            finally:
                if registry is not None:
                    registry.close()
                if source is not None:
                    source.close()

        self._worker = threading.Thread(
            target=worker, name="beanoflight-simulation", daemon=True
        )
        self._worker.start()
        self._playing = True
        self.run_button.configure(text="Pause")
        rate = "unlimited" if replay_settings.target_fps <= 0 else f"{target_fps:g} FPS"
        buffer_status = (
            f"prebuffering {replay_settings.prebuffer_frames} frames"
            if replay_settings.prebuffer_frames
            else "streaming decode"
        )
        source_status = (
            "memory-mapped RAW green-plane input"
            if raw_bundle is not None
            else "calibrated video input"
        )
        self.status_var.set(
            f"Simulation starting at {rate}; {source_status}; {buffer_status}; "
            "live playback "
            f"{'enabled' if replay_settings.preview_enabled else 'disabled'}…"
        )

    def _ready_for_analysis(self) -> bool:
        if self.source is None or self.background is None:
            messagebox.showinfo("Analysis", "Open a recording first.", parent=self)
            return False
        if self.calibration is None:
            messagebox.showinfo(
                "Analysis",
                "Load the recording's PinkPlane v2 homography first.",
                parent=self,
            )
            return False
        return True

    def toggle_run(self) -> None:
        if self.mode_var.get() == "Simulation":
            if self._worker is not None and self._worker.is_alive():
                if self._pause.is_set():
                    self._pause.clear()
                    self._playing = True
                    self.run_button.configure(text="Pause")
                    self.status_var.set("Simulation resumed…")
                else:
                    self._pause.set()
                    self._playing = False
                    self.run_button.configure(text="Resume")
                    self.status_var.set("Pausing simulation…")
            else:
                self._start_simulation()
            return
        if self.run is None or not self.run.frames:
            self.analyse_clip()
            return
        self._playing = not self._playing
        self.run_button.configure(text="Pause" if self._playing else "Play")
        if self._playing:
            self._schedule_review_playback()
        elif self._play_after is not None:
            self.after_cancel(self._play_after)
            self._play_after = None

    def _schedule_review_playback(self) -> None:
        if not self._playing or self.source is None:
            return
        if (
            self.current_index
            >= min(self.source.metadata.frame_count, len(self.run.frames)) - 1
        ):
            self.current_index = 0
        else:
            self.current_index += 1
        self.frame_var.set(self.current_index)
        self._refresh_display()
        delay = max(1, round(1_000.0 / self.source.metadata.fps))
        self._play_after = self.after(delay, self._schedule_review_playback)

    def stop_work(self) -> None:
        self._stop.set()
        self._pause.clear()
        self._playing = False
        self.run_button.configure(
            text="Run" if self.mode_var.get() == "Simulation" else "Play"
        )
        if self._play_after is not None:
            self.after_cancel(self._play_after)
            self._play_after = None

    def _poll_workers(self) -> None:
        try:
            while True:
                generation, frame, analysis = self._display_queue.get_nowait()
                if generation != self._generation:
                    continue
                self.current_index = analysis.frame_index
                self.frame_var.set(self.current_index)
                if self.calibration is not None:
                    layout = GateLayout(
                        self.calibration.sorting_line_y(self.sorting_offset_mm)
                    )
                    self.image_pane.set_bgr(
                        render_analysis(
                            frame,
                            analysis,
                            self.calibration,
                            layout,
                            left_birth_margin_px=(
                                self.tracker_settings.left_birth_margin_px
                            ),
                            right_birth_margin_px=(
                                self.tracker_settings.right_birth_margin_px
                            ),
                        )
                    )
                self._update_track_table(analysis)
                self._update_frame_label()
        except queue.Empty:
            pass
        try:
            while True:
                message = self._control_queue.get_nowait()
                kind = message[0]
                generation = message[1]
                if generation != self._generation:
                    continue
                if kind == "progress":
                    _kind, _generation, done, total, milliseconds = message
                    self.status_var.set(
                        f"Analysing frame {done:,} of {total:,}; latest {milliseconds:.2f} ms"
                    )
                elif kind == "done":
                    self.run = message[2]
                    self._worker = None
                    self._playing = False
                    self.run_button.configure(
                        text="Run" if self.mode_var.get() == "Simulation" else "Play"
                    )
                    self.performance_var.set(
                        f"{len(self.run.frames):,} frames; mean {self.run.mean_processing_ms:.2f} ms; "
                        f"p95 {self.run.p95_processing_ms:.2f} ms"
                    )
                    self.status_var.set(
                        "Analysis complete. Step freely through frames; IDs and predictions are stable."
                    )
                    self._refresh_display()
                elif kind == "replay_progress":
                    value = message[2]
                    self.status_var.set(
                        f"Simulation frame {value.frame_index + 1:,} / "
                        f"{value.frame_count:,} · {value.achieved_fps:.1f} FPS · "
                        f"age {value.frame_age_ms:.1f} ms · skipped "
                        f"{value.frames_skipped} · "
                        f"read {value.source_read_ms:.2f} ms · analyse "
                        f"{value.processing_ms:.2f} ms · crops {value.crops_submitted} "
                        f"({value.crops_dropped} dropped)"
                    )
                elif kind == "prebuffer_progress":
                    _kind, _generation, buffered, target = message
                    self.status_var.set(
                        f"Prebuffering decoded frames {buffered:,} / {target:,}…"
                    )
                elif kind == "replay_done":
                    summary = message[2]
                    self._worker = None
                    self._playing = False
                    self.run_button.configure(text="Run")
                    self.performance_var.set(
                        f"{summary.frames_processed:,} streamed frames; "
                        f"{summary.achieved_fps:.1f} processed FPS; "
                        f"{summary.source_timeline_fps:.1f} timeline FPS; source mean "
                        f"{summary.mean_source_read_ms:.2f} ms; analysis mean "
                        f"{summary.mean_processing_ms:.2f} ms; analysis max "
                        f"{summary.max_processing_ms:.2f} ms; "
                        f"prebuffer {summary.prebuffered_frames} frames in "
                        f"{summary.prebuffer_seconds:.2f} s; "
                        f"{summary.missed_deadlines} missed deadlines; "
                        f"{summary.frames_skipped} stale frames skipped"
                    )
                    self.status_var.set(
                        f"Simulation complete · run {summary.run_id[:12]} · "
                        f"crops {summary.crops_submitted}, dropped {summary.crops_dropped}"
                    )
                elif kind == "error":
                    self._worker = None
                    self._playing = False
                    self.run_button.configure(text="Play")
                    self.status_var.set(f"Analysis failed: {message[2]}")
                    messagebox.showerror(
                        "BeanoFlight analysis", message[2], parent=self
                    )
        except queue.Empty:
            pass
        self.after(100 if self.performance_mode else 30, self._poll_workers)

    def set_frame(self, index: int) -> None:
        if self.source is None:
            return
        self.current_index = max(
            0, min(self.source.metadata.frame_count - 1, int(index))
        )
        self.frame_var.set(self.current_index)
        self._refresh_display()

    def step_frame(self, delta: int) -> None:
        self.set_frame(self.current_index + delta)

    def _timeline_changed(self, value: str) -> None:
        if self.source is None:
            return
        index = max(0, min(self.source.metadata.frame_count - 1, round(float(value))))
        if index != self.current_index:
            self.current_index = index
            self._refresh_display()

    def _refresh_display(self) -> None:
        if self.source is None:
            self.image_pane.clear()
            return
        if self.inspector_var.get():
            self._refresh_inspector()
            return
        try:
            frame = self.source.frame(self.current_index)
        except SourceError as exc:
            self.status_var.set(str(exc))
            return
        if (
            self.run is not None
            and self.current_index < len(self.run.frames)
            and self.calibration is not None
        ):
            analysis = self.run.frames[self.current_index]
            layout = GateLayout(self.calibration.sorting_line_y(self.sorting_offset_mm))
            self.image_pane.set_bgr(
                render_analysis(
                    frame,
                    analysis,
                    self.calibration,
                    layout,
                    left_birth_margin_px=self.tracker_settings.left_birth_margin_px,
                    right_birth_margin_px=self.tracker_settings.right_birth_margin_px,
                )
            )
            self._update_track_table(analysis)
        else:
            stage = PipelineStage(
                "unanalysed",
                "Unanalysed frame",
                frame,
                ("No current sequential track result",),
                "Analyse the clip to assign stable bean IDs.",
            )
            rendered = render_pipeline_stage(stage)
            draw_birth_margins(
                rendered,
                self.tracker_settings.left_birth_margin_px,
                self.tracker_settings.right_birth_margin_px,
            )
            self.image_pane.set_bgr(rendered)
            self._update_track_table(None)
        self._update_frame_label()

    def _update_track_table(self, analysis: FrameAnalysis | None) -> None:
        for item in self.track_tree.get_children():
            self.track_tree.delete(item)
        if analysis is None:
            return
        predictions = {item.bean_ref: item for item in analysis.predictions}
        for rejection in analysis.rejections:
            x_mm, y_mm = rejection.observation.position_mm
            self.track_tree.insert(
                "",
                tk.END,
                values=(
                    "—",
                    "rejected",
                    f"{x_mm:.1f}",
                    f"{y_mm:.1f}",
                    "—",
                    "—",
                    "TOP-PENDING"
                    if rejection.reason.startswith("top entry pending")
                    else "EDGE"
                    if "margin" in rejection.reason
                    else "BIRTH",
                    "—",
                    "—",
                ),
            )
        for track in analysis.tracks:
            prediction = predictions.get(track.bean_ref)
            if prediction is not None:
                best = max(prediction.gates, key=lambda item: item.probability)
                gate, probability, eta = (
                    best.gate.label,
                    f"{best.probability:.0%}",
                    f"{prediction.seconds_until_crossing * 1000:.0f}ms",
                )
            else:
                gate = probability = eta = "—"
            self.track_tree.insert(
                "",
                tk.END,
                values=(
                    f"{track.bean_ref.sequence:06d}",
                    track.status.value,
                    f"{track.state[0]:.1f}",
                    f"{track.state[1]:.1f}",
                    f"{track.state[2]:.0f}",
                    f"{track.state[3]:.0f}",
                    gate,
                    probability,
                    eta,
                ),
            )

    def _update_frame_label(self) -> None:
        total = self.source.metadata.frame_count if self.source is not None else 0
        self.frame_label.configure(
            text=f"{self.current_index + 1 if total else 0:,} / {total:,}"
        )

    def _invalidate_run(self, message: str) -> None:
        self.stop_work()
        self._generation += 1
        self.run = None
        self.performance_var.set("Analysis stale — reanalyse after tuning")
        self.status_var.set(message)

    def _mode_changed(self) -> None:
        self.stop_work()
        self.run_button.configure(
            text="Run" if self.mode_var.get() == "Simulation" else "Play"
        )
        self.status_var.set(
            "Simulation streams to BeanRegistry and the external inferencer."
            if self.mode_var.get() == "Simulation"
            else "Review mode: analyse once, then step or play with stable results."
        )

    def export_analysis(self) -> None:
        if self.run is None:
            messagebox.showinfo("Export analysis", "Analyse a clip first.", parent=self)
            return
        selected = filedialog.asksaveasfilename(
            title="Export BeanoFlight analysis",
            defaultextension=".json",
            initialfile="beanoflight-analysis.json",
            filetypes=(("JSON", "*.json"),),
        )
        if not selected:
            return
        try:
            export_run_json(self.run, Path(selected))
        except OSError as exc:
            messagebox.showerror("Export analysis", str(exc), parent=self)
            return
        self.status_var.set(f"Analysis exported to {selected}")

    def _close(self) -> None:
        self.stop_work()
        if self.source is not None:
            self.source.close()
        self.destroy()
