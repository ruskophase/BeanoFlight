"""Inspectable, deterministic OpenCV bean-segmentation pipeline."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, replace

import cv2
import numpy as np

from .models import Detection, PipelineStage
from .source import RawReplayFrame


class DetectorError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class DetectorSettings:
    processing_scale: float = 0.5
    blur_kernel: int = 5
    threshold: int = 22
    close_kernel: int = 5
    close_iterations: int = 1
    open_kernel: int = 3
    open_iterations: int = 1
    dilate_kernel: int = 3
    dilate_iterations: int = 0
    min_area_px: int = 2_000
    max_area_px: int = 40_000
    min_width_px: int = 50
    max_width_px: int = 260
    min_height_px: int = 50
    max_height_px: int = 260
    min_solidity: float = 0.55

    def validate(self) -> None:
        if not 0.25 <= self.processing_scale <= 1.0:
            raise DetectorError("processing_scale must be between 0.25 and 1.0")
        for name in ("blur_kernel", "close_kernel", "open_kernel", "dilate_kernel"):
            value = int(getattr(self, name))
            if value < 1 or value % 2 == 0:
                raise DetectorError(f"{name} must be a positive odd integer")
        if not 0 <= self.threshold <= 255:
            raise DetectorError("threshold must be between 0 and 255")
        for name in ("close_iterations", "open_iterations", "dilate_iterations"):
            if not 0 <= int(getattr(self, name)) <= 10:
                raise DetectorError(f"{name} must be between 0 and 10")
        for low, high in (
            (self.min_area_px, self.max_area_px),
            (self.min_width_px, self.max_width_px),
            (self.min_height_px, self.max_height_px),
        ):
            if low <= 0 or high <= low:
                raise DetectorError(
                    "component minimums must be positive and below maximums"
                )
        if not 0.0 <= self.min_solidity <= 1.0:
            raise DetectorError("minimum solidity must be between 0 and 1")

    def updated(self, **values: object) -> DetectorSettings:
        result = replace(self, **values)
        result.validate()
        return result


@dataclass(frozen=True, slots=True)
class DetectionResult:
    detections: tuple[Detection, ...]
    stages: tuple[PipelineStage, ...]


class BeanDetector:
    def __init__(self, settings: DetectorSettings | None = None) -> None:
        self.settings = settings or DetectorSettings()
        self.settings.validate()
        self._background_source: np.ndarray | None = None
        self._background_scale: float | None = None
        self._background_blurred: np.ndarray | None = None

    def detect(
        self,
        frame_bgr: np.ndarray,
        background_bgr: np.ndarray,
        *,
        inspect: bool = False,
    ) -> DetectionResult:
        _validate_images(frame_bgr, background_bgr)
        settings = self.settings
        scale = settings.processing_scale
        native_height, native_width = frame_bgr.shape[:2]
        if scale < 0.999:
            processing_size = (
                max(1, round(native_width * scale)),
                max(1, round(native_height * scale)),
            )
            processing_frame = cv2.resize(
                frame_bgr, processing_size, interpolation=cv2.INTER_AREA
            )
        else:
            processing_frame = frame_bgr
        stages: list[PipelineStage] = []

        def stage(
            key: str,
            name: str,
            image: np.ndarray,
            values: tuple[str, ...] = (),
            explanation: str = "",
        ) -> None:
            if inspect:
                displayed = image
                if image.shape[:2] != (native_height, native_width):
                    displayed = cv2.resize(
                        image,
                        (native_width, native_height),
                        interpolation=cv2.INTER_NEAREST,
                    )
                stages.append(
                    PipelineStage(key, name, displayed.copy(), values, explanation)
                )

        stage(
            "input",
            "1. Input frame",
            frame_bgr,
            (f"shape={frame_bgr.shape[1]}x{frame_bgr.shape[0]}", "encoding=8-bit BGR"),
            "Undistorted CamL frame supplied by BeanoFastCap.",
        )
        gray = cv2.cvtColor(processing_frame, cv2.COLOR_BGR2GRAY)
        stage(
            "grayscale",
            "2. Convert to grayscale",
            gray,
            (
                "operation=cv2.COLOR_BGR2GRAY",
                f"processing_scale={settings.processing_scale:.2f}",
            ),
            "Removes colour while retaining luminance contrast against the static scene.",
        )
        blur_size = (settings.blur_kernel, settings.blur_kernel)
        blurred = cv2.GaussianBlur(gray, blur_size, 0)
        if (
            self._background_source is not background_bgr
            or self._background_scale != scale
            or self._background_blurred is None
        ):
            processing_background = (
                cv2.resize(
                    background_bgr, processing_size, interpolation=cv2.INTER_AREA
                )
                if scale < 0.999
                else background_bgr
            )
            background_gray = cv2.cvtColor(processing_background, cv2.COLOR_BGR2GRAY)
            self._background_blurred = cv2.GaussianBlur(background_gray, blur_size, 0)
            self._background_source = background_bgr
            self._background_scale = scale
        background_blurred = self._background_blurred
        stage(
            "blur",
            "3. Gaussian blur",
            blurred,
            (
                f"kernel={settings.blur_kernel}x{settings.blur_kernel}",
                "sigma=OpenCV automatic",
            ),
            "Suppresses sensor noise before differencing.",
        )
        difference = cv2.absdiff(blurred, background_blurred)
        stage(
            "difference",
            "4. Absolute background difference",
            difference,
            ("operation=cv2.absdiff",),
            "Bright pixels differ from the selected clean background.",
        )
        _unused, thresholded = cv2.threshold(
            difference, settings.threshold, 255, cv2.THRESH_BINARY
        )
        stage(
            "threshold",
            "5. Fixed threshold",
            thresholded,
            (f"threshold={settings.threshold}", "maximum=255", "type=BINARY"),
            "Converts background difference into foreground candidates.",
        )
        closed = _morph(
            thresholded,
            cv2.MORPH_CLOSE,
            settings.close_kernel,
            settings.close_iterations,
        )
        stage(
            "close",
            "6. Morphological close",
            closed,
            (
                f"kernel={settings.close_kernel}x{settings.close_kernel} ellipse",
                f"iterations={settings.close_iterations}",
            ),
            "Fills small holes and joins narrow gaps within one bean silhouette.",
        )
        opened = _morph(
            closed, cv2.MORPH_OPEN, settings.open_kernel, settings.open_iterations
        )
        stage(
            "open",
            "7. Morphological open",
            opened,
            (
                f"kernel={settings.open_kernel}x{settings.open_kernel} ellipse",
                f"iterations={settings.open_iterations}",
            ),
            "Removes isolated foreground specks and thin protrusions.",
        )
        foreground = _morph(
            opened, cv2.MORPH_DILATE, settings.dilate_kernel, settings.dilate_iterations
        )
        stage(
            "dilate",
            "8. Optional dilation",
            foreground,
            (
                f"kernel={settings.dilate_kernel}x{settings.dilate_kernel} ellipse",
                f"iterations={settings.dilate_iterations}",
            ),
            "Expands surviving shapes; zero iterations leaves the opening unchanged.",
        )

        count, labels, stats, centroids = cv2.connectedComponentsWithStats(
            foreground, connectivity=8
        )
        candidate_view = cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR) if inspect else None
        final_view = frame_bgr.copy() if inspect else None
        detections: list[Detection] = []
        for label in range(1, count):
            x, y, width, height, area = (int(value) for value in stats[label])
            if candidate_view is not None:
                cv2.rectangle(
                    candidate_view,
                    (x, y),
                    (x + width - 1, y + height - 1),
                    (0, 210, 255),
                    1,
                )
            roi_labels = labels[y : y + height, x : x + width]
            component_mask = np.asarray(roi_labels == label, dtype=np.uint8) * 255
            contours, _hierarchy = cv2.findContours(
                component_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
            )
            contour_area = float(sum(cv2.contourArea(contour) for contour in contours))
            hull_points = (
                np.vstack(contours) if contours else np.empty((0, 1, 2), np.int32)
            )
            hull_area = (
                float(cv2.contourArea(cv2.convexHull(hull_points)))
                if len(hull_points)
                else 0.0
            )
            solidity = contour_area / hull_area if hull_area > 0 else 0.0
            native_x = max(0, round(x / scale))
            native_y = max(0, round(y / scale))
            native_width_component = max(1, round(width / scale))
            native_height_component = max(1, round(height / scale))
            native_area = max(1, round(area / (scale * scale)))
            accepted = (
                settings.min_area_px <= native_area <= settings.max_area_px
                and settings.min_width_px
                <= native_width_component
                <= settings.max_width_px
                and settings.min_height_px
                <= native_height_component
                <= settings.max_height_px
                and solidity >= settings.min_solidity
            )
            colour_roi = processing_frame[y : y + height, x : x + width]
            mean = cv2.mean(colour_roi, mask=component_mask)[:3]
            if not accepted:
                if final_view is not None:
                    cv2.rectangle(
                        final_view,
                        (native_x, native_y),
                        (
                            native_x + native_width_component - 1,
                            native_y + native_height_component - 1,
                        ),
                        (40, 70, 180),
                        1,
                    )
                continue
            centre = (
                float(centroids[label][0] / scale),
                float(centroids[label][1] / scale),
            )
            detection = Detection(
                centroid_px=centre,
                bbox_px=(
                    native_x,
                    native_y,
                    native_width_component,
                    native_height_component,
                ),
                area_px=native_area,
                solidity=float(solidity),
                mean_bgr=tuple(float(value) for value in mean),
            )
            detections.append(detection)
            if final_view is not None:
                cv2.rectangle(
                    final_view,
                    (native_x, native_y),
                    (
                        native_x + native_width_component - 1,
                        native_y + native_height_component - 1,
                    ),
                    (80, 245, 120),
                    2,
                )
                cv2.drawMarker(
                    final_view,
                    (round(centre[0]), round(centre[1])),
                    (40, 255, 255),
                    cv2.MARKER_CROSS,
                    13,
                    1,
                    cv2.LINE_AA,
                )
        detections.sort(key=lambda value: (value.centroid_px[1], value.centroid_px[0]))
        stage(
            "components",
            "9. Connected components",
            candidate_view if candidate_view is not None else foreground,
            ("connectivity=8", f"foreground_components={max(0, count - 1)}"),
            "Yellow boxes are all connected foreground components before size filtering.",
        )
        stage(
            "filtered",
            "10. Filtered bean detections",
            final_view if final_view is not None else foreground,
            (
                f"area={settings.min_area_px}..{settings.max_area_px}px",
                f"width={settings.min_width_px}..{settings.max_width_px}px",
                f"height={settings.min_height_px}..{settings.max_height_px}px",
                f"minimum_solidity={settings.min_solidity:.2f}",
                f"accepted={len(detections)}",
            ),
            "Green boxes pass every filter; muted red boxes were rejected.",
        )
        return DetectionResult(tuple(detections), tuple(stages))

    def inspect(
        self, frame_bgr: np.ndarray, background_bgr: np.ndarray
    ) -> DetectionResult:
        return self.detect(frame_bgr, background_bgr, inspect=True)


class RawGreenDetector:
    """Bean segmentation on the compact green plane of an RGGB RAW frame."""

    def __init__(self, settings: DetectorSettings | None = None) -> None:
        self.settings = settings or DetectorSettings()
        self.settings.validate()
        self._background_source: np.ndarray | None = None
        self._background_blurred: np.ndarray | None = None
        self._processing_size: tuple[int, int] | None = None

    def detect(
        self,
        frame: RawReplayFrame,
        background_gray: np.ndarray,
        *,
        inspect: bool = False,
    ) -> DetectionResult:
        if inspect:
            raise DetectorError(
                "the pipeline inspector uses calibrated Review frames, not RAW replay"
            )
        gray = frame.detection_gray
        if not isinstance(gray, np.ndarray) or gray.dtype != np.uint8 or gray.ndim != 2:
            raise DetectorError("RAW detection frame must contain an 8-bit green plane")
        if (
            not isinstance(background_gray, np.ndarray)
            or background_gray.dtype != np.uint8
            or background_gray.shape != gray.shape
        ):
            raise DetectorError("RAW background must match the frame green plane")
        settings = self.settings
        native_width, native_height = frame.native_size_px
        processing_size = (
            max(1, round(native_width * settings.processing_scale)),
            max(1, round(native_height * settings.processing_scale)),
        )
        if gray.shape[::-1] == processing_size:
            processing_gray = gray
        else:
            processing_gray = cv2.resize(
                gray, processing_size, interpolation=cv2.INTER_AREA
            )
        blur_size = (settings.blur_kernel, settings.blur_kernel)
        blurred = cv2.GaussianBlur(processing_gray, blur_size, 0)
        if (
            self._background_source is not background_gray
            or self._processing_size != processing_size
            or self._background_blurred is None
        ):
            processing_background = (
                background_gray
                if background_gray.shape[::-1] == processing_size
                else cv2.resize(
                    background_gray, processing_size, interpolation=cv2.INTER_AREA
                )
            )
            self._background_blurred = cv2.GaussianBlur(
                processing_background, blur_size, 0
            )
            self._background_source = background_gray
            self._processing_size = processing_size
        difference = cv2.absdiff(blurred, self._background_blurred)
        _unused, foreground = cv2.threshold(
            difference, settings.threshold, 255, cv2.THRESH_BINARY
        )
        foreground = _morph(
            foreground,
            cv2.MORPH_CLOSE,
            settings.close_kernel,
            settings.close_iterations,
        )
        foreground = _morph(
            foreground,
            cv2.MORPH_OPEN,
            settings.open_kernel,
            settings.open_iterations,
        )
        foreground = _morph(
            foreground,
            cv2.MORPH_DILATE,
            settings.dilate_kernel,
            settings.dilate_iterations,
        )
        count, labels, stats, centroids = cv2.connectedComponentsWithStats(
            foreground, connectivity=8
        )
        scale_x = processing_size[0] / native_width
        scale_y = processing_size[1] / native_height
        detections: list[Detection] = []
        for label in range(1, count):
            x, y, width, height, area = (int(value) for value in stats[label])
            native_x = max(0, round(x / scale_x))
            native_y = max(0, round(y / scale_y))
            native_width_component = max(1, round(width / scale_x))
            native_height_component = max(1, round(height / scale_y))
            native_area = max(1, round(area / (scale_x * scale_y)))
            if not (
                settings.min_area_px <= native_area <= settings.max_area_px
                and settings.min_width_px
                <= native_width_component
                <= settings.max_width_px
                and settings.min_height_px
                <= native_height_component
                <= settings.max_height_px
            ):
                continue
            roi_labels = labels[y : y + height, x : x + width]
            component_mask = np.asarray(roi_labels == label, dtype=np.uint8) * 255
            contours, _hierarchy = cv2.findContours(
                component_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
            )
            contour_area = float(sum(cv2.contourArea(item) for item in contours))
            hull_points = (
                np.vstack(contours) if contours else np.empty((0, 1, 2), np.int32)
            )
            hull_area = (
                float(cv2.contourArea(cv2.convexHull(hull_points)))
                if len(hull_points)
                else 0.0
            )
            solidity = contour_area / hull_area if hull_area > 0 else 0.0
            if solidity < settings.min_solidity:
                continue
            mean_gray = float(
                cv2.mean(
                    processing_gray[y : y + height, x : x + width],
                    mask=component_mask,
                )[0]
            )
            detections.append(
                Detection(
                    centroid_px=(
                        float(centroids[label][0] / scale_x),
                        float(centroids[label][1] / scale_y),
                    ),
                    bbox_px=(
                        native_x,
                        native_y,
                        native_width_component,
                        native_height_component,
                    ),
                    area_px=native_area,
                    solidity=float(solidity),
                    mean_bgr=(mean_gray, mean_gray, mean_gray),
                )
            )
        detections.sort(key=lambda value: (value.centroid_px[1], value.centroid_px[0]))
        return DetectionResult(tuple(detections), ())


def temporal_median_background(frames: Iterable[np.ndarray]) -> np.ndarray:
    selected = tuple(frames)
    if not selected:
        raise DetectorError("at least one frame is required for a background")
    reference_shape = selected[0].shape
    if any(
        frame.shape != reference_shape or frame.dtype != np.uint8 for frame in selected
    ):
        raise DetectorError("background frames must have matching uint8 shapes")
    # Native Beano colour frames are large. Work in row tiles so calculating the
    # median does not create another full-size frame stack.
    result = np.empty(reference_shape, dtype=np.uint8)
    tile_rows = 64
    for start in range(0, reference_shape[0], tile_rows):
        stop = min(reference_shape[0], start + tile_rows)
        tile = np.stack([frame[start:stop] for frame in selected], axis=0)
        result[start:stop] = np.median(tile, axis=0).astype(np.uint8)
    return result


def _morph(
    image: np.ndarray, operation: int, kernel_size: int, iterations: int
) -> np.ndarray:
    if iterations == 0:
        return image.copy()
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (kernel_size, kernel_size))
    return cv2.morphologyEx(image, operation, kernel, iterations=iterations)


def _validate_images(frame: np.ndarray, background: np.ndarray) -> None:
    for name, image in (("frame", frame), ("background", background)):
        if not isinstance(image, np.ndarray) or image.dtype != np.uint8:
            raise DetectorError(f"{name} must be a uint8 NumPy image")
        if image.ndim != 3 or image.shape[2] != 3:
            raise DetectorError(f"{name} must be a three-channel BGR image")
    if frame.shape != background.shape:
        raise DetectorError("frame and background dimensions differ")
