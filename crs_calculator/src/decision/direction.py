"""Direction inference from gate consensus."""

from __future__ import annotations

from dataclasses import dataclass

from ..pillars.fbpd import FBPDResult
from ..pillars.lrm import LRMResult
from ..pillars.mp import MPResult


@dataclass
class DirectionVote:
    direction: str        # "LONG" or "SHORT"
    conviction: float     # 0..1
    long_votes: float
    short_votes: float


def infer_direction(
    current_price: float,
    lrm: LRMResult,
    fbpd: FBPDResult,
    mp: MPResult,
) -> DirectionVote:
    long_votes = 0.0
    short_votes = 0.0

    if lrm.cluster_price is not None:
        if lrm.cluster_price > current_price:
            long_votes += lrm.score
        else:
            short_votes += lrm.score

    # Positive IS divergence (perp > fair) => mean-revert DOWN => SHORT.
    if fbpd.available:
        weight = abs(fbpd.score) * 100.0
        if fbpd.score > 0:
            short_votes += weight
        else:
            long_votes += weight

    if mp.components.cvd_velocity > 0:
        long_votes += 0.3
    elif mp.components.cvd_velocity < 0:
        short_votes += 0.3

    if mp.components.taker_aggression > 0:
        long_votes += 0.1 * abs(mp.components.taker_aggression)
    else:
        short_votes += 0.1 * abs(mp.components.taker_aggression)

    total = long_votes + short_votes
    if total <= 0:
        return DirectionVote("LONG", 0.0, 0.0, 0.0)
    direction = "LONG" if long_votes >= short_votes else "SHORT"
    conviction = abs(long_votes - short_votes) / total
    return DirectionVote(direction, conviction, long_votes, short_votes)
