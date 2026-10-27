"""Recorded-video background selection and provenance helpers."""

from __future__ import annotations

import random
from dataclasses import dataclass

DEFAULT_BACKGROUND_FRAMES_TEXT = "43,222,347"


@dataclass(frozen=True, slots=True)
class BackgroundProvenance:
    method: str
    frame_indices: tuple[int, ...]
    candidate_seed: int | None = None


def parse_background_frame_indices(
    value: str,
    *,
    required_count: int = 3,
    frame_count: int | None = None,
) -> tuple[int, ...]:
    """Parse distinct zero-based reference frames entered by a human."""

    try:
        result = tuple(int(item.strip()) for item in value.split(",") if item.strip())
    except ValueError as exc:
        raise ValueError("background frames must be comma-separated integers") from exc
    if len(result) != required_count or len(set(result)) != required_count:
        raise ValueError(
            f"exactly {required_count} distinct background frame indices are required"
        )
    if min(result, default=-1) < 0:
        raise ValueError("background frame indices must be non-negative")
    if frame_count is not None:
        invalid = tuple(index for index in result if index >= frame_count)
        if invalid:
            maximum = max(0, frame_count - 1)
            values = ", ".join(str(index) for index in invalid)
            raise ValueError(
                f"background frame indices outside recording: {values}; "
                f"valid range is 0 to {maximum}"
            )
    return result


def stratified_random_candidates(
    frame_count: int,
    target_count: int,
    *,
    candidates_per_stratum: int = 4,
    seed: int,
) -> tuple[int, ...]:
    """Offer random candidates in full-video passes over temporal strata.

    The first ``target_count`` items contain one candidate from each stratum.
    Later passes provide replacements for candidates the reviewer rejects.
    """

    if frame_count <= 0:
        raise ValueError("frame count must be positive")
    if target_count <= 0:
        raise ValueError("target count must be positive")
    if candidates_per_stratum <= 0:
        raise ValueError("candidates per stratum must be positive")
    stratum_count = min(frame_count, target_count)
    generator = random.Random(seed)
    strata: list[list[int]] = []
    for stratum in range(stratum_count):
        start = (stratum * frame_count) // stratum_count
        stop = ((stratum + 1) * frame_count) // stratum_count
        candidate_count = min(candidates_per_stratum, stop - start)
        strata.append(generator.sample(range(start, stop), candidate_count))
    return tuple(
        candidates[pass_index]
        for pass_index in range(candidates_per_stratum)
        for candidates in strata
        if pass_index < len(candidates)
    )
