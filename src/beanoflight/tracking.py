"""Physics-informed Kalman tracking and deterministic small-set assignment."""

from __future__ import annotations

import math
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from functools import cache

import numpy as np

from .events import EventBus
from .models import (
    BeanEvent,
    BeanRef,
    DetectionRejection,
    Observation,
    TrackSnapshot,
    TrackStatus,
)


@dataclass(frozen=True, slots=True)
class TrackerSettings:
    gravity_mm_s2: float = 9_810.0
    process_acceleration_sigma_mm_s2: float = 2_500.0
    measurement_sigma_mm: float = 0.22
    association_gate_sigma: float = 5.0
    appearance_weight: float = 0.18
    confirmation_hits: int = 2
    maximum_missed_frames: int = 2
    birth_zone_depth_mm: float = 26.0
    release_height_above_fov_mm: float = 20.0
    exit_margin_mm: float = 8.0
    left_birth_margin_px: int = 50
    right_birth_margin_px: int = 50
    require_top_birth: bool = True

    def validate(self) -> None:
        positive = (
            self.gravity_mm_s2,
            self.process_acceleration_sigma_mm_s2,
            self.measurement_sigma_mm,
            self.association_gate_sigma,
            self.birth_zone_depth_mm,
            self.exit_margin_mm,
        )
        if any(not math.isfinite(value) or value <= 0 for value in positive):
            raise ValueError("tracker scale and noise settings must be positive")
        if self.confirmation_hits < 1 or self.maximum_missed_frames < 0:
            raise ValueError("tracker hit/miss counts are invalid")
        if self.appearance_weight < 0:
            raise ValueError("appearance weight cannot be negative")
        if self.left_birth_margin_px < 0 or self.right_birth_margin_px < 0:
            raise ValueError("birth margins cannot be negative")


class _Track:
    def __init__(
        self,
        bean_ref: BeanRef,
        observation: Observation,
        settings: TrackerSettings,
        top_y_mm: float,
    ) -> None:
        x, y = observation.position_mm
        release_y = top_y_mm - settings.release_height_above_fov_mm
        distance = max(0.0, y - release_y)
        initial_vy = math.sqrt(2.0 * settings.gravity_mm_s2 * distance)
        position_variance = settings.measurement_sigma_mm**2
        self.bean_ref = bean_ref
        self.state = np.asarray((x, y, 0.0, initial_vy), dtype=np.float64)
        self.covariance = np.diag(
            (position_variance * 4.0, position_variance * 4.0, 500.0**2, 500.0**2)
        )
        self.timestamp_ns = observation.timestamp_ns
        self.hits = 1
        self.misses = 0
        self.status = (
            TrackStatus.CONFIRMED
            if settings.confirmation_hits <= 1
            else TrackStatus.TENTATIVE
        )
        self.history: list[Observation] = [observation]
        self.last_bbox_px = observation.detection.bbox_px
        self.last_area_px = observation.detection.area_px

    def predict(self, timestamp_ns: int, settings: TrackerSettings) -> None:
        if timestamp_ns < self.timestamp_ns:
            raise ValueError("track timestamps must be monotonic")
        dt = (timestamp_ns - self.timestamp_ns) / 1_000_000_000.0
        if dt == 0:
            return
        transition = np.asarray(
            ((1.0, 0.0, dt, 0.0), (0.0, 1.0, 0.0, dt), (0.0, 0.0, 1.0, 0.0), (0.0, 0.0, 0.0, 1.0)),
            dtype=np.float64,
        )
        self.state = transition @ self.state
        self.state[1] += 0.5 * settings.gravity_mm_s2 * dt * dt
        self.state[3] += settings.gravity_mm_s2 * dt
        acceleration_basis = np.asarray((0.5 * dt * dt, 0.5 * dt * dt, dt, dt))
        # Independent horizontal and vertical acceleration disturbances.
        horizontal = np.asarray((acceleration_basis[0], 0.0, acceleration_basis[2], 0.0))
        vertical = np.asarray((0.0, acceleration_basis[1], 0.0, acceleration_basis[3]))
        process = settings.process_acceleration_sigma_mm_s2**2 * (
            np.outer(horizontal, horizontal) + np.outer(vertical, vertical)
        )
        self.covariance = transition @ self.covariance @ transition.T + process
        self.timestamp_ns = timestamp_ns

    def innovation(
        self, observation: Observation, settings: TrackerSettings
    ) -> tuple[np.ndarray, np.ndarray, float]:
        measurement = np.asarray(observation.position_mm, dtype=np.float64)
        residual = measurement - self.state[:2]
        innovation_covariance = self.covariance[:2, :2] + np.eye(2) * (
            settings.measurement_sigma_mm**2
        )
        try:
            distance_squared = float(
                residual.T @ np.linalg.solve(innovation_covariance, residual)
            )
        except np.linalg.LinAlgError:
            distance_squared = math.inf
        return residual, innovation_covariance, distance_squared

    def match_cost(self, observation: Observation, settings: TrackerSettings) -> float:
        _residual, _covariance, distance_squared = self.innovation(observation, settings)
        gate_squared = settings.association_gate_sigma**2
        if not math.isfinite(distance_squared) or distance_squared > gate_squared:
            return math.inf
        area_ratio = max(observation.detection.area_px, 1) / max(self.last_area_px, 1)
        appearance = abs(math.log(area_ratio)) * settings.appearance_weight
        cost = distance_squared / gate_squared + appearance
        return cost if cost < 1.0 else math.inf

    def update(self, observation: Observation, settings: TrackerSettings) -> None:
        residual, innovation_covariance, _distance = self.innovation(observation, settings)
        gain = self.covariance[:, :2] @ np.linalg.inv(innovation_covariance)
        self.state = self.state + gain @ residual
        identity = np.eye(4)
        measurement_matrix = np.asarray(
            ((1.0, 0.0, 0.0, 0.0), (0.0, 1.0, 0.0, 0.0)), dtype=np.float64
        )
        # Joseph form protects symmetry and positive semidefiniteness.
        residual_operator = identity - gain @ measurement_matrix
        measurement_covariance = np.eye(2) * settings.measurement_sigma_mm**2
        self.covariance = (
            residual_operator @ self.covariance @ residual_operator.T
            + gain @ measurement_covariance @ gain.T
        )
        self.hits += 1
        self.misses = 0
        self.status = (
            TrackStatus.CONFIRMED
            if self.hits >= settings.confirmation_hits
            else TrackStatus.TENTATIVE
        )
        self.history.append(observation)
        self.last_bbox_px = observation.detection.bbox_px
        self.last_area_px = observation.detection.area_px

    def miss(self) -> None:
        self.misses += 1
        if self.status == TrackStatus.CONFIRMED:
            self.status = TrackStatus.OCCLUDED

    def snapshot(self) -> TrackSnapshot:
        return TrackSnapshot(
            bean_ref=self.bean_ref,
            status=self.status,
            timestamp_ns=self.timestamp_ns,
            state=tuple(float(value) for value in self.state),  # type: ignore[arg-type]
            covariance=tuple(
                tuple(float(value) for value in row) for row in self.covariance
            ),
            hits=self.hits,
            misses=self.misses,
            last_bbox_px=self.last_bbox_px,
            history=tuple(self.history),
        )


class TrackManager:
    def __init__(
        self,
        *,
        top_y_mm: float,
        bottom_y_mm: float,
        image_width_px: int = 1456,
        settings: TrackerSettings | None = None,
        run_id: str | None = None,
        events: EventBus | None = None,
    ) -> None:
        self.settings = settings or TrackerSettings()
        self.settings.validate()
        if bottom_y_mm <= top_y_mm:
            raise ValueError("FoV bottom must lie below its top")
        if image_width_px <= 0:
            raise ValueError("image width must be positive")
        if (
            self.settings.left_birth_margin_px + self.settings.right_birth_margin_px
            >= image_width_px
        ):
            raise ValueError("birth margins leave no usable image width")
        self.top_y_mm = float(top_y_mm)
        self.bottom_y_mm = float(bottom_y_mm)
        self.image_width_px = int(image_width_px)
        self.run_id = run_id or uuid.uuid4().hex
        self.events = events
        self._next_sequence = 1
        self._next_internal_sequence = -1
        self._active: dict[BeanRef, _Track] = {}
        self._suppressed: dict[BeanRef, tuple[_Track, str]] = {}
        self._pending_births: dict[BeanRef, _Track] = {}
        self.last_rejections: tuple[DetectionRejection, ...] = ()

    @property
    def active_count(self) -> int:
        return len(self._active)

    @property
    def suppressed_count(self) -> int:
        return len(self._suppressed)

    @property
    def pending_birth_count(self) -> int:
        return len(self._pending_births)

    def update(
        self, observations: Sequence[Observation], timestamp_ns: int
    ) -> tuple[TrackSnapshot, ...]:
        if any(observation.timestamp_ns != timestamp_ns for observation in observations):
            raise ValueError("all observations must use the frame timestamp")
        rejections: list[DetectionRejection] = []
        tracks = list(self._active.values())
        suppressed_items = list(self._suppressed.values())
        suppressed_tracks = [item[0] for item in suppressed_items]
        pending_tracks = list(self._pending_births.values())
        association_tracks = [*tracks, *suppressed_tracks, *pending_tracks]
        for track in association_tracks:
            track.predict(timestamp_ns, self.settings)
        costs = np.full(
            (len(association_tracks), len(observations)), math.inf, dtype=np.float64
        )
        for track_index, track in enumerate(association_tracks):
            for observation_index, observation in enumerate(observations):
                costs[track_index, observation_index] = track.match_cost(
                    observation, self.settings
                )
        matches = _optimal_assignment(costs)
        matched_association_tracks = {track_index for track_index, _ in matches}
        matched_observations = {observation_index for _, observation_index in matches}
        emitted: list[TrackSnapshot] = []
        for track_index, observation_index in matches:
            track = association_tracks[track_index]
            if track_index < len(tracks):
                previous_status = track.status
                track.update(observations[observation_index], self.settings)
                if (
                    previous_status == TrackStatus.TENTATIVE
                    and track.status == TrackStatus.CONFIRMED
                ):
                    self._publish("confirmed", track, timestamp_ns)
            elif track_index < len(tracks) + len(suppressed_tracks):
                suppressed_index = track_index - len(tracks)
                reason = suppressed_items[suppressed_index][1]
                track.update(observations[observation_index], self.settings)
                rejections.append(
                    DetectionRejection(
                        observations[observation_index],
                        f"continuation of {reason}",
                    )
                )
            else:
                observation = observations[observation_index]
                self._continue_pending_birth(track, observation, timestamp_ns, rejections)

        for track_index, track in enumerate(tracks):
            if track_index in matched_association_tracks:
                continue
            track.miss()
            out_of_view = track.state[1] > self.bottom_y_mm + self.settings.exit_margin_mm
            expired = track.misses > self.settings.maximum_missed_frames
            if out_of_view or expired:
                track.status = (
                    TrackStatus.CANCELLED
                    if track.hits < self.settings.confirmation_hits
                    else TrackStatus.EXITED
                )
                emitted.append(track.snapshot())
                self._publish(track.status.value, track, timestamp_ns)
                self._active.pop(track.bean_ref, None)

        for suppressed_index, track in enumerate(suppressed_tracks):
            association_index = len(tracks) + suppressed_index
            if association_index in matched_association_tracks:
                continue
            track.miss()
            out_of_view = track.state[1] > self.bottom_y_mm + self.settings.exit_margin_mm
            expired = track.misses > self.settings.maximum_missed_frames
            if out_of_view or expired:
                self._suppressed.pop(track.bean_ref, None)

        pending_offset = len(tracks) + len(suppressed_tracks)
        for pending_index, track in enumerate(pending_tracks):
            association_index = pending_offset + pending_index
            if association_index in matched_association_tracks:
                continue
            # A provisional top-edge candidate owns no public bean ID. If it
            # cannot be associated on the immediately following frame, discard
            # it so stale, biased motion cannot compete with a clean birth.
            self._pending_births.pop(track.bean_ref, None)

        for observation_index, observation in enumerate(observations):
            if observation_index in matched_observations:
                continue
            edge_reason = self._edge_rejection_reason(observation)
            if edge_reason is not None:
                rejections.append(DetectionRejection(observation, edge_reason))
                suppression_ref = self._new_internal_ref()
                suppression = _Track(
                    suppression_ref, observation, self.settings, self.top_y_mm
                )
                self._suppressed[suppression_ref] = (suppression, edge_reason)
                continue
            pending_reason = self._top_birth_pending_reason(observation)
            if pending_reason is not None:
                rejections.append(DetectionRejection(observation, pending_reason))
                pending_ref = self._new_internal_ref()
                self._pending_births[pending_ref] = _Track(
                    pending_ref, observation, self.settings, self.top_y_mm
                )
                continue
            if self.settings.require_top_birth and (
                observation.position_mm[1] > self.top_y_mm + self.settings.birth_zone_depth_mm
            ):
                rejections.append(
                    DetectionRejection(observation, "outside top birth region")
                )
                continue
            bean_ref = BeanRef(self.run_id, self._next_sequence)
            self._next_sequence += 1
            track = _Track(bean_ref, observation, self.settings, self.top_y_mm)
            self._active[bean_ref] = track
            self._publish("created", track, timestamp_ns)

        self.last_rejections = tuple(rejections)
        active_snapshots = [track.snapshot() for track in self._active.values()]
        return tuple(sorted((*active_snapshots, *emitted), key=lambda value: value.bean_ref))

    def _continue_pending_birth(
        self,
        candidate: _Track,
        observation: Observation,
        timestamp_ns: int,
        rejections: list[DetectionRejection],
    ) -> None:
        edge_reason = self._edge_rejection_reason(observation)
        if edge_reason is not None:
            self._pending_births.pop(candidate.bean_ref, None)
            suppression = _Track(
                candidate.bean_ref, observation, self.settings, self.top_y_mm
            )
            self._suppressed[candidate.bean_ref] = (suppression, edge_reason)
            rejections.append(DetectionRejection(observation, edge_reason))
            return
        pending_reason = self._top_birth_pending_reason(observation)
        if pending_reason is not None:
            candidate.update(observation, self.settings)
            rejections.append(DetectionRejection(observation, pending_reason))
            return
        self._pending_births.pop(candidate.bean_ref, None)
        if self.settings.require_top_birth and (
            observation.position_mm[1]
            > self.top_y_mm + self.settings.birth_zone_depth_mm
        ):
            rejections.append(
                DetectionRejection(observation, "outside top birth region")
            )
            return
        bean_ref = BeanRef(self.run_id, self._next_sequence)
        self._next_sequence += 1
        # Deliberately start from the first admissible observation. A centroid
        # measured while the bean or its inference crop was clipped can bias
        # the initial velocity enough to fragment the track on the next frame.
        track = _Track(bean_ref, observation, self.settings, self.top_y_mm)
        self._active[bean_ref] = track
        self._publish("created", track, timestamp_ns)

    def _new_internal_ref(self) -> BeanRef:
        bean_ref = BeanRef(self.run_id, self._next_internal_sequence)
        self._next_internal_sequence -= 1
        return bean_ref

    def _edge_rejection_reason(self, observation: Observation) -> str | None:
        x, _y, width, _height = observation.detection.bbox_px
        touches_left = x < self.settings.left_birth_margin_px
        touches_right = (
            x + width > self.image_width_px - self.settings.right_birth_margin_px
        )
        if touches_left and touches_right:
            return "left and right birth margins"
        if touches_left:
            return "left birth margin"
        if touches_right:
            return "right birth margin"
        return None

    def _top_birth_pending_reason(self, observation: Observation) -> str | None:
        _x, y, width, height = observation.detection.bbox_px
        _centroid_x, centroid_y = observation.detection.centroid_px
        complete_centred_crop_size = max(0, math.floor(centroid_y) * 2)
        if y <= 0 or complete_centred_crop_size < max(width, height):
            return "top entry pending (complete bean crop unavailable)"
        return None

    def _publish(self, kind: str, track: _Track, timestamp_ns: int) -> None:
        if self.events is None:
            return
        self.events.publish(
            BeanEvent(
                kind=kind,
                bean_ref=track.bean_ref,
                timestamp_ns=timestamp_ns,
                payload={"hits": track.hits, "status": track.status.value},
            )
        )


def _optimal_assignment(costs: np.ndarray) -> tuple[tuple[int, int], ...]:
    """Exact assignment for <=10 beans using detection-bitmask dynamic programming."""

    if costs.ndim != 2:
        raise ValueError("assignment costs must be a matrix")
    track_count, detection_count = costs.shape
    if detection_count > 16:
        raise ValueError("small-set assignment supports at most 16 detections")
    unmatched_cost = 1.0

    @cache
    def solve(track_index: int, used_mask: int) -> tuple[float, tuple[tuple[int, int], ...]]:
        if track_index == track_count:
            return 0.0, ()
        best_cost, best_matches = solve(track_index + 1, used_mask)
        best_cost += unmatched_cost
        for detection_index in range(detection_count):
            if used_mask & (1 << detection_index):
                continue
            match_cost = float(costs[track_index, detection_index])
            if not math.isfinite(match_cost):
                continue
            tail_cost, tail_matches = solve(
                track_index + 1, used_mask | (1 << detection_index)
            )
            candidate = match_cost + tail_cost
            if candidate < best_cost - 1e-12:
                best_cost = candidate
                best_matches = ((track_index, detection_index), *tail_matches)
        return best_cost, best_matches

    return solve(0, 0)[1]
