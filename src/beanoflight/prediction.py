"""Sorting-line crossing and Gaussian gate probability calculations."""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass

import numpy as np

from .models import (
    CrossingPrediction,
    Gate,
    GateProbability,
    TrackSnapshot,
    TrackStatus,
)


@dataclass(frozen=True, slots=True)
class GateLayout:
    line_y_mm: float
    gate_width_mm: float = 5.0
    gate_count: int = 21
    centre_x_mm: float = 0.0
    actuation_probability_threshold: float = 0.35
    measured_gates: tuple[Gate, ...] = ()
    nozzle_map_sha256: str | None = None
    nozzle_map_path: str | None = None
    nozzle_map_json: str | None = None

    def __post_init__(self) -> None:
        if self.gate_width_mm <= 0:
            raise ValueError("gate width must be positive")
        if self.gate_count <= 0 or self.gate_count % 2 == 0:
            raise ValueError("gate count must be a positive odd number")
        if not 0.0 <= self.actuation_probability_threshold <= 1.0:
            raise ValueError("gate probability threshold must be between zero and one")

    @property
    def gates(self) -> tuple[Gate, ...]:
        if self.measured_gates:
            return self.measured_gates
        radius = self.gate_count // 2
        return tuple(
            Gate(
                index=index,
                left_mm=self.centre_x_mm + (index - 0.5) * self.gate_width_mm,
                right_mm=self.centre_x_mm + (index + 0.5) * self.gate_width_mm,
            )
            for index in range(-radius, radius + 1)
        )

    def to_dict(self) -> dict:
        return {
            "mode": "measured" if self.measured_gates else "virtual",
            "gates": [asdict(gate) for gate in self.gates],
            "line_y_mm": self.line_y_mm,
            "nozzle_map_sha256": self.nozzle_map_sha256,
            "nozzle_map_path": self.nozzle_map_path,
            "nozzle_map": None
            if self.nozzle_map_json is None
            else json.loads(self.nozzle_map_json),
            "zone_definition": "midpoints between measured centres; finite half-spacing outer boundaries"
            if self.measured_gates
            else "uniform virtual intervals",
        }


class TrajectoryPredictor:
    def __init__(
        self,
        layout: GateLayout,
        *,
        gravity_mm_s2: float = 9_810.0,
        process_acceleration_sigma_mm_s2: float = 2_500.0,
    ) -> None:
        self.layout = layout
        self.gravity_mm_s2 = gravity_mm_s2
        self.process_acceleration_sigma_mm_s2 = process_acceleration_sigma_mm_s2

    def predict(self, track: TrackSnapshot) -> CrossingPrediction | None:
        # EXITED means the bean has left the image, not the machine. Its final
        # propagated state is specifically what downstream sorting needs for
        # the line below the FoV. A one-hit tentative track can already supply
        # inference evidence, but not a trajectory used for an immutable sort.
        if track.status in {TrackStatus.TENTATIVE, TrackStatus.CANCELLED}:
            return None
        if self.layout.measured_gates:
            return self._predict_measured(track)
        x, y, vx, vy = track.state
        dt = _crossing_time(y, vy, self.layout.line_y_mm, self.gravity_mm_s2)
        if dt is None:
            return None
        x_mean = x + vx * dt
        covariance = np.asarray(track.covariance, dtype=np.float64)
        horizontal = np.asarray((1.0, 0.0, dt, 0.0))
        x_variance = float(horizontal @ covariance @ horizontal.T)
        x_variance += (0.5 * dt * dt * self.process_acceleration_sigma_mm_s2) ** 2
        x_std = math.sqrt(max(x_variance, 0.05**2))

        vertical = np.asarray((0.0, 1.0, 0.0, dt))
        y_variance = float(vertical @ covariance @ vertical.T)
        y_variance += (0.5 * dt * dt * self.process_acceleration_sigma_mm_s2) ** 2
        crossing_speed = max(abs(vy + self.gravity_mm_s2 * dt), 1.0)
        time_std_ms = 1_000.0 * math.sqrt(max(y_variance, 0.0)) / crossing_speed

        probabilities = tuple(
            GateProbability(
                gate=gate,
                probability=max(
                    0.0,
                    min(
                        1.0,
                        _normal_cdf(gate.right_mm, x_mean, x_std)
                        - _normal_cdf(gate.left_mm, x_mean, x_std),
                    ),
                ),
            )
            for gate in self.layout.gates
        )
        selected = tuple(
            item.gate.index
            for item in probabilities
            if item.probability >= self.layout.actuation_probability_threshold
        )
        return CrossingPrediction(
            bean_ref=track.bean_ref,
            line_y_mm=self.layout.line_y_mm,
            crossing_timestamp_ns=track.timestamp_ns + round(dt * 1_000_000_000),
            seconds_until_crossing=dt,
            x_mean_mm=x_mean,
            x_std_mm=x_std,
            time_std_ms=time_std_ms,
            gates=probabilities,
            selected_gate_indices=selected,
        )

    def _predict_measured(self, track: TrackSnapshot) -> CrossingPrediction | None:
        x, y, vx, vy = track.state
        covariance = np.asarray(track.covariance, dtype=np.float64)
        probabilities = []
        for gate in self.layout.gates:
            dt = _crossing_time(y, vy, gate.line_y_mm, self.gravity_mm_s2)
            # A nozzle already behind the falling bean must not be fired now.
            if gate.line_y_mm <= y or dt is None:
                probabilities.append(GateProbability(gate, 0.0))
                continue
            horizontal = np.asarray((1.0, 0.0, dt, 0.0))
            vertical = np.asarray((0.0, 1.0, 0.0, dt))
            process = (0.5 * dt * dt * self.process_acceleration_sigma_mm_s2) ** 2
            mean = x + vx * dt
            std = math.sqrt(
                max(float(horizontal @ covariance @ horizontal.T) + process, 0.05**2)
            )
            speed = max(abs(vy + self.gravity_mm_s2 * dt), 1.0)
            time_std = (
                1000
                * math.sqrt(
                    max(float(vertical @ covariance @ vertical.T) + process, 0.0)
                )
                / speed
            )
            probability = max(
                0.0,
                min(
                    1.0,
                    _normal_cdf(gate.right_mm, mean, std)
                    - _normal_cdf(gate.left_mm, mean, std),
                ),
            )
            probabilities.append(
                GateProbability(
                    gate,
                    probability,
                    track.timestamp_ns + round(dt * 1e9),
                    dt,
                    mean,
                    std,
                    time_std,
                )
            )
        future = [p for p in probabilities if p.crossing_timestamp_ns is not None]
        if not future:
            return None
        earliest = min(future, key=lambda p: p.crossing_timestamp_ns)
        return CrossingPrediction(
            track.bean_ref,
            earliest.gate.line_y_mm,
            earliest.crossing_timestamp_ns,
            earliest.seconds_until_crossing,
            earliest.x_mean_mm,
            earliest.x_std_mm,
            earliest.time_std_ms,
            tuple(probabilities),
            tuple(
                p.gate.index
                for p in future
                if p.probability >= self.layout.actuation_probability_threshold
            ),
            self.layout.nozzle_map_sha256,
        )


def _crossing_time(y: float, vy: float, line_y: float, gravity: float) -> float | None:
    distance = line_y - y
    if distance <= 0:
        return 0.0
    if gravity <= 0:
        return distance / vy if vy > 0 else None
    discriminant = vy * vy + 2.0 * gravity * distance
    if discriminant < 0:
        return None
    result = (-vy + math.sqrt(discriminant)) / gravity
    return result if result >= 0 and math.isfinite(result) else None


def _normal_cdf(value: float, mean: float, standard_deviation: float) -> float:
    z = (value - mean) / (standard_deviation * math.sqrt(2.0))
    return 0.5 * (1.0 + math.erf(z))
