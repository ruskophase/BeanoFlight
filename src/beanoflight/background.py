"""Recorded-video background selection and provenance helpers."""

from __future__ import annotations

import random
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class BackgroundProvenance:
    method: str
    frame_indices: tuple[int, ...]
    candidate_seed: int | None = None


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
