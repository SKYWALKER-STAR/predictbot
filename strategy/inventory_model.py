"""Inventory quotes from Avellaneda--Stoikov and Gueant--Lehalle--Fernandez-Tapia.

Prices are collateral/share; sigma is absolute price/sqrt(second), k is
1/price, A is fills/second, gamma is 1/collateral. Inventory is measured in
fixed-size lots internally, so changing order size also changes risk.
This module has no exchange, settings, or third-party dependencies.
"""
from __future__ import annotations

import math
import logging
from collections import deque
from dataclasses import dataclass
from functools import lru_cache

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ModelQuotes:
    bid: float | None
    ask: float | None


def inventory_quotes(*, model: str, mid: float, inventory: float, lot_size: float,
                     max_inventory: float, gamma: float, sigma: float, k: float,
                     arrival_rate: float, horizon: float) -> ModelQuotes:
    values = (mid, inventory, lot_size, max_inventory, gamma, sigma, k, arrival_rate, horizon)
    if not all(math.isfinite(x) for x in values):
        raise ValueError("Non-finite model input")
    if not 0 < mid < 1 or min(lot_size, max_inventory, gamma, k, arrival_rate) <= 0:
        raise ValueError("Invalid price or nonpositive model parameter")
    if sigma < 0 or horizon < 0 or lot_size > max_inventory:
        raise ValueError("Invalid volatility, horizon, or inventory lot")
    # Paper prices are prices for one trading unit, here one lot of shares.
    q = inventory / lot_size
    s = mid * lot_size
    vol = sigma * lot_size
    decay = k / lot_size
    base = math.log1p(gamma / decay) / gamma
    if model == "as":
        # LimitOrderBook.pdf, equations (3.17)--(3.18), approximate solution.
        risk = gamma * vol * vol * horizon
        reservation = s - q * risk
        half = base + risk / 2
        bid, ask = (reservation - half) / lot_size, (reservation + half) / lot_size
        limit = max_inventory
    elif model == "glft":
        # inventory risk.pdf, Proposition 2 / Theorem 1, finite Q exact solution.
        bound = math.floor(max_inventory / lot_size)
        if bound > 200:
            raise ValueError("GLFT grid exceeds 401 states; increase lot size")
        limit = bound * lot_size
        q = max(-bound, min(bound, q))
        alpha = decay * gamma * vol * vol / 2
        eta = arrival_rate * math.exp(-(1 + decay / gamma) * math.log1p(gamma / decay))
        logs = _log_value_vector(bound, alpha, eta, horizon)

        def distance(x: float, direction: int) -> float:
            # Partial fills leave fractional lots: interpolate quote distances
            # between adjacent integer states (an execution adaptation).
            lo, hi = -bound, bound
            if direction == 1:
                hi -= 1
            else:
                lo += 1
            x = max(lo, min(hi, x))
            left = math.floor(x)
            right = min(hi, left + 1)
            def at(j: int) -> float:
                return base + (logs[j + bound] - logs[j + direction + bound]) / decay
            return at(left) + (x - left) * (at(right) - at(left))

        bid = (s - distance(q, 1)) / lot_size
        ask = (s + distance(q, -1)) / lot_size
    else:
        raise ValueError(f"Unknown inventory model: {model}")
    return ModelQuotes(bid if inventory + lot_size <= limit + 1e-9 else None,
                       ask if inventory - lot_size >= -limit - 1e-9 else None)


@lru_cache(maxsize=128)
def _log_value_vector(bound: int, alpha: float, eta: float, horizon: float) -> tuple[float, ...]:
    """Compute log(exp(-M*tau) @ ones), up to a common additive constant.

    Shift -M to a nonnegative tridiagonal matrix and evaluate its exponential
    by a positive Taylor series. Rescaling between steps prevents exponential
    overflow and avoids eigenvector cancellation at remote inventory states.
    No ODE time discretization is used; series relative tolerance is 1e-14.
    """
    diagonal = [alpha * q * q for q in range(-bound, bound + 1)]
    shift = max(diagonal)
    rate = shift + 2 * eta
    steps = max(1, math.ceil(rate * horizon / 20))
    if steps > 10000:
        raise ValueError("GLFT parameters exceed numerical work limit")
    dt = horizon / steps
    v = [1.0] * len(diagonal)
    for _ in range(steps):
        term = v[:]
        total = v[:]
        for n in range(1, 160):
            term = [dt / n * ((shift - diagonal[i]) * term[i]
                    + (eta * term[i - 1] if i else 0)
                    + (eta * term[i + 1] if i + 1 < len(v) else 0))
                    for i in range(len(v))]
            total = [a + b for a, b in zip(total, term)]
            if max(term) <= 1e-14 * max(total):
                break
        else:
            raise ValueError("GLFT matrix exponential did not converge")
        scale = max(total)
        v = [x / scale for x in total]
    if any(x <= 0 or not math.isfinite(x) for x in v):
        raise ValueError("GLFT value vector lost numerical positivity")
    return tuple(math.log(x) for x in v)


class PriceVariance:
    """Rolling realized variance per second from absolute midpoint changes."""

    def __init__(self, window: float, min_samples: int, fallback_sigma: float) -> None:
        self.samples: deque[tuple[float, float]] = deque()
        self.window, self.min_samples, self.fallback_sigma = window, min_samples, fallback_sigma

    def observe(self, timestamp: float, mid: float) -> float:
        if self.samples and timestamp <= self.samples[-1][0]:
            logger.debug("volatility observation ignored: timestamp=%s last_timestamp=%s",
                         timestamp, self.samples[-1][0])
            return self.sigma()
        self.samples.append((timestamp, mid))
        while len(self.samples) > 1 and self.samples[0][0] < timestamp - self.window:
            self.samples.popleft()
        return self.sigma()

    def sigma(self) -> float:
        if len(self.samples) < self.min_samples:
            logger.debug("volatility warmup: samples=%d required=%d sigma=%.8f",
                         len(self.samples), self.min_samples, self.fallback_sigma)
            return self.fallback_sigma
        points = list(self.samples)
        elapsed = points[-1][0] - points[0][0]
        if elapsed <= 0:
            return self.fallback_sigma
        variance = sum((b[1] - a[1]) ** 2 for a, b in zip(points, points[1:])) / elapsed
        logger.debug("volatility estimate: realized_variance_per_second=%.10f raw_sigma=%.8f "
                     "floor_applied=%s elapsed_seconds=%.3f", variance, math.sqrt(variance),
                     math.sqrt(variance) < self.fallback_sigma, elapsed)
        # Initial sigma also acts as a floor against quiet/stale markets.
        return max(self.fallback_sigma, math.sqrt(variance))
