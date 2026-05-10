"""Pure math utilities for CRS pillar calculations.

All functions here are pure (no I/O, no globals) so pillar logic can be
unit-tested without mocking network calls.
"""

from __future__ import annotations

import math
from typing import Iterable, Sequence

import numpy as np


def sigmoid(x: float) -> float:
    """Numerically stable sigmoid."""
    if x >= 0:
        z = math.exp(-x)
        return 1.0 / (1.0 + z)
    z = math.exp(x)
    return z / (1.0 + z)


def itakura_saito_divergence(p: float, q: float) -> float:
    """Itakura-Saito divergence between two positive scalars.

    IS(p||q) = (p/q) - log(p/q) - 1
    Returns a signed value: sign carries direction (p>q => positive).
    """
    if p <= 0.0 or q <= 0.0:
        raise ValueError("itakura_saito_divergence requires strictly positive inputs")
    ratio = p / q
    magnitude = ratio - math.log(ratio) - 1.0
    return math.copysign(magnitude, p - q)


def gaussian_kernel(x: np.ndarray, center: float, bandwidth: float) -> np.ndarray:
    """Evaluate a Gaussian kernel at points `x` centered at `center`."""
    if bandwidth <= 0:
        raise ValueError("bandwidth must be > 0")
    z = (x - center) / bandwidth
    return np.exp(-0.5 * z * z)


def hawkes_intensity(
    event_times: Sequence[float],
    now: float,
    base_rate: float,
    decay: float,
) -> float:
    """Simple univariate Hawkes self-exciting intensity.

    lambda(t) = mu + sum_{t_i < t} exp(-decay * (t - t_i))
    Times are seconds (epoch or relative). Decay must be > 0.
    """
    if decay <= 0:
        raise ValueError("decay must be > 0")
    intensity = base_rate
    for t_i in event_times:
        dt = now - t_i
        if dt < 0:
            continue
        intensity += math.exp(-decay * dt)
    return intensity


def ewma(values: Iterable[float], half_life: float) -> float:
    """Exponentially weighted moving average given half-life (in samples)."""
    vals = list(values)
    if not vals:
        return 0.0
    if half_life <= 0:
        return float(vals[-1])
    alpha = 1.0 - math.exp(-math.log(2.0) / half_life)
    out = float(vals[0])
    for v in vals[1:]:
        out = alpha * float(v) + (1.0 - alpha) * out
    return out


def numerical_derivative(series: Sequence[float], dt: float) -> float:
    """Second-derivative estimate at the right edge of `series`.

    Uses a centered 3-point stencil over the last three samples.
    """
    if len(series) < 3 or dt <= 0:
        return 0.0
    a, b, c = series[-3], series[-2], series[-1]
    return (c - 2.0 * b + a) / (dt * dt)


def clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))
