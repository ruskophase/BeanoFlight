"""Physics-informed Kalman tracking and deterministic small-set assignment."""

from __future__ import annotations

import math
import uuid
from dataclasses import dataclass
from functools import lru_cache
from typing import Sequence

import numpy as np

from .events import EventBus
from .models import BeanEvent, BeanRef, Observation, TrackSnapshot, TrackStatus


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
        settings: TrackerSettings | None = None,
        run_id: str | None = None,
        events: EventBus | None = None,
    ) -> None:
        self.settings = settings or TrackerSettings()
        self.settings.validate()
        if bottom_y_mm <= top_y_mm:
            raise ValueError("FoV bottom must lie below its top")
        self.top_y_mm = float(top_y_mm)
        self.bottom_y_mm = float(bottom_y_mm)
        self.run_id = run_id or uuid.uuid4().hex
        self.events = events
        self._next_sequence = 1
        self._active: dict[BeanRef, _Track] = {}

    @property
    def active_count(self) -> int:
        return len(self._active)

    def update(
        self, observations: Sequence[Observation], timestamp_ns: int
    ) -> tuple[TrackSnapshot, ...]:
        if any(observation.timestamp_ns != timestamp_ns for observation in observations):
            raise ValueError("all observations must use the frame timestamp")
        tracks = list(self._active.values())
        for track in tracks:
            track.predict(timestamp_ns, self.settings)
        costs = np.full((len(tracks), len(observations)), math.inf, dtype=np.float64)
        for track_index, track in enumerate(tracks):
            for observation_index, observation in enumerate(observations):
                costs[track_index, observation_index] = track.match_cost(
                    observation, self.settings
                )
        matches = _optimal_assignment(costs)
        matched_tracks = {track_index for track_index, _ in matches}
        matched_observations = {observation_index for _, observation_index in matches}
        emitted: list[TrackSnapshot] = []
        for track_index, observation_index in matches:
            track = tracks[track_index]
            previous_status = track.status
            track.update(observations[observation_index], self.settings)
            if previous_status == TrackStatus.TENTATIVE and track.status == TrackStatus.CONFIRMED:
                self._publish("confirmed", track, timestamp_ns)

        for track_index, track in enumerate(tracks):
            if track_index in matched_tracks:
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

        for observation_index, observation in enumerate(observations):
            if observation_index in matched_observations:
                continue
            if self.settings.require_top_birth and (
                observation.position_mm[1] > self.top_y_mm + self.settings.birth_zone_depth_mm
            ):
                continue
            bean_ref = BeanRef(self.run_id, self._next_sequence)
            self._next_sequence += 1
            track = _Track(bean_ref, observation, self.settings, self.top_y_mm)
            self._active[bean_ref] = track
            self._publish("created", track, timestamp_ns)

        active_snapshots = [track.snapshot() for track in self._active.values()]
        return tuple(sorted((*active_snapshots, *emitted), key=lambda value: value.bean_ref))

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
    if max(track_count, detection_count) > 16:
        raise ValueError("small-set assignment supports at most 16 tracks/detections")
    unmatched_cost = 1.0

    @lru_cache(maxsize=None)
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
