#!/usr/bin/env python3
"""Layer 2 core — calibration math + post-hoc calibrators (pure stdlib).

"P(Over) = 0.70" is only trustworthy if, across all the times the model says 0.70, the
Over actually hits ~70% of the time. This module MEASURES that and, when it fails,
CORRECTS it. It is the pure, offline-testable core; the `calibration.py` CLI pulls the
(prob, outcome) pairs from the shadow log and renders these numbers.

MEASUREMENT
  - reliability_diagram : predicted-vs-empirical frequency, bucketed.
  - brier_decomposition : Murphy's Brier = Reliability − Resolution + Uncertainty.
                          Reliability↓ good (calibration), Resolution↑ good
                          (discrimination), Uncertainty = irreducible base rate ō(1−ō).
  - ece / mce           : Expected / Maximum Calibration Error across buckets.

CORRECTION (post-hoc; fit on a HELD-OUT calibration set, never the training games)
  - TemperatureCalibrator : 1 param T on the logit — gentlest; preserves the argmax.
  - PlattCalibrator       : 2-param sigmoid (A, B) on the logit — works with little data.
  - IsotonicCalibrator    : monotonic PAV step map — most flexible, needs the most data.

NO third-party imports — only `math`. Deterministic and offline-testable.
"""

from __future__ import annotations

import math


def _clip(p: float, eps: float = 1e-6) -> float:
    return min(1.0 - eps, max(eps, p))


def _logit(p: float) -> float:
    p = _clip(p)
    return math.log(p / (1.0 - p))


def _sigmoid(z: float) -> float:
    if z >= 0:
        return 1.0 / (1.0 + math.exp(-z))
    e = math.exp(z)
    return e / (1.0 + e)


# ---------------------------------------------------------------------------
# Measurement
# ---------------------------------------------------------------------------


def _bins(pairs: list[tuple[float, int]], nbins: int) -> list[list[tuple[float, int]]]:
    bins: list[list[tuple[float, int]]] = [[] for _ in range(nbins)]
    for p, o in pairs:
        idx = min(nbins - 1, max(0, int(p * nbins)))
        bins[idx].append((p, o))
    return bins


def reliability_diagram(pairs: list[tuple[float, int]], nbins: int = 10) -> list[dict]:
    """Per-bucket {range, n, avg_pred, empirical, gap} over (prob, outcome) pairs.

    A perfectly calibrated model has avg_pred ≈ empirical (gap ≈ 0) in every bucket.
    Empty buckets are omitted.
    """
    out = []
    for i, b in enumerate(_bins(pairs, nbins)):
        if not b:
            continue
        avg_pred = sum(p for p, _ in b) / len(b)
        empirical = sum(o for _, o in b) / len(b)
        out.append({
            "range": f"{i / nbins:.2f}-{(i + 1) / nbins:.2f}",
            "n": len(b),
            "avg_pred": avg_pred,
            "empirical": empirical,
            "gap": empirical - avg_pred,
        })
    return out


def ece(pairs: list[tuple[float, int]], nbins: int = 10) -> float | None:
    """Expected Calibration Error: Σ (n_bin/N)·|empirical − avg_pred|. None if empty."""
    if not pairs:
        return None
    n = len(pairs)
    total = 0.0
    for b in _bins(pairs, nbins):
        if not b:
            continue
        avg_pred = sum(p for p, _ in b) / len(b)
        empirical = sum(o for _, o in b) / len(b)
        total += (len(b) / n) * abs(empirical - avg_pred)
    return total


def mce(pairs: list[tuple[float, int]], nbins: int = 10) -> float | None:
    """Maximum Calibration Error: max bucket |empirical − avg_pred|. None if empty."""
    if not pairs:
        return None
    worst = 0.0
    for b in _bins(pairs, nbins):
        if not b:
            continue
        avg_pred = sum(p for p, _ in b) / len(b)
        empirical = sum(o for _, o in b) / len(b)
        worst = max(worst, abs(empirical - avg_pred))
    return worst


def brier_decomposition(pairs: list[tuple[float, int]], nbins: int = 10) -> dict | None:
    """Murphy decomposition of the Brier score, bucketing forecasts into `nbins`.

        Brier ≈ Reliability − Resolution + Uncertainty
      - Uncertainty = ō(1−ō)                       (base-rate variance; irreducible)
      - Reliability = (1/N) Σ_b n_b (p̄_b − ō_b)²    (LOWER better — calibration)
      - Resolution  = (1/N) Σ_b n_b (ō_b − ō)²      (HIGHER better — discrimination)

    The identity is exact when every bucket's forecasts are constant (no within-bin
    spread); otherwise `recombined` differs from the raw `brier` by that within-bin
    variance. Both are returned so the gap is visible. None if empty.
    """
    if not pairs:
        return None
    n = len(pairs)
    base = sum(o for _, o in pairs) / n
    uncertainty = base * (1.0 - base)
    reliability = 0.0
    resolution = 0.0
    for b in _bins(pairs, nbins):
        if not b:
            continue
        nb = len(b)
        pbar = sum(p for p, _ in b) / nb
        obar = sum(o for _, o in b) / nb
        reliability += nb * (pbar - obar) ** 2
        resolution += nb * (obar - base) ** 2
    reliability /= n
    resolution /= n
    raw_brier = sum((p - o) ** 2 for p, o in pairs) / n
    return {
        "reliability": reliability,
        "resolution": resolution,
        "uncertainty": uncertainty,
        "brier": raw_brier,
        "recombined": reliability - resolution + uncertainty,
        "base_rate": base,
        "n": n,
        "nbins": nbins,
    }


# ---------------------------------------------------------------------------
# Post-hoc calibrators (fit on a held-out calibration set)
# ---------------------------------------------------------------------------


class TemperatureCalibrator:
    """Single-parameter temperature scaling on the logit: p_cal = σ(logit(p)/T).

    T = 1 is identity; T > 1 softens overconfidence, T < 1 sharpens. A monotonic
    rescale, so it never changes which side is favored — only the confidence. Fit by
    1-D golden-section minimization of log-loss.
    """

    def __init__(self, temperature: float = 1.0):
        self.temperature = temperature

    def fit(self, pairs: list[tuple[float, int]]) -> "TemperatureCalibrator":
        if not pairs:
            return self
        logits = [(_logit(p), o) for p, o in pairs]

        def loss(t: float) -> float:
            t = max(1e-3, t)
            s = 0.0
            for z, o in logits:
                p = _clip(_sigmoid(z / t))
                s -= o * math.log(p) + (1 - o) * math.log(1 - p)
            return s

        lo, hi = 0.05, 10.0
        gr = (math.sqrt(5) - 1) / 2
        c = hi - gr * (hi - lo)
        d = lo + gr * (hi - lo)
        for _ in range(100):
            if loss(c) < loss(d):
                hi = d
            else:
                lo = c
            c = hi - gr * (hi - lo)
            d = lo + gr * (hi - lo)
        self.temperature = (lo + hi) / 2.0
        return self

    def predict(self, p: float) -> float:
        return _sigmoid(_logit(p) / max(1e-3, self.temperature))

    def predict_all(self, ps: list[float]) -> list[float]:
        return [self.predict(p) for p in ps]


class PlattCalibrator:
    """Platt scaling: p_cal = σ(A·logit(p) + B), the 2-param logistic fit.

    Generalizes temperature scaling (the A-only, B=0 case) by also allowing a bias
    shift. Fit by gradient descent on log-loss. Good with limited data.
    """

    def __init__(self, a: float = 1.0, b: float = 0.0):
        self.a = a
        self.b = b

    def fit(self, pairs: list[tuple[float, int]], iters: int = 2000,
            lr: float = 0.05) -> "PlattCalibrator":
        if not pairs:
            return self
        xs = [(_logit(p), float(o)) for p, o in pairs]
        n = len(xs)
        a, b = 1.0, 0.0
        for _ in range(iters):
            ga = gb = 0.0
            for x, o in xs:
                p = _sigmoid(a * x + b)
                err = p - o
                ga += err * x
                gb += err
            a -= lr * ga / n
            b -= lr * gb / n
        self.a, self.b = a, b
        return self

    def predict(self, p: float) -> float:
        return _sigmoid(self.a * _logit(p) + self.b)

    def predict_all(self, ps: list[float]) -> list[float]:
        return [self.predict(p) for p in ps]


def _pav(ys: list[float], ws: list[float]) -> list[float]:
    """Pool-Adjacent-Violators: weighted least-squares isotonic (non-decreasing) fit.

    `ys` are the targets in x-sorted order; returns the fitted level per input point.
    O(n) via a block-merge stack tracking each block's weighted mean and membership.
    """
    blocks: list[dict] = []  # each: {"sum": Σ w·y, "w": Σ w, "n": count}
    for y, w in zip(ys, ws):
        blocks.append({"sum": y * w, "w": w, "n": 1})
        while len(blocks) > 1 and (blocks[-2]["sum"] / blocks[-2]["w"]) > (
                blocks[-1]["sum"] / blocks[-1]["w"]):
            b = blocks.pop()
            blocks[-1]["sum"] += b["sum"]
            blocks[-1]["w"] += b["w"]
            blocks[-1]["n"] += b["n"]
    out: list[float] = []
    for b in blocks:
        out.extend([b["sum"] / b["w"]] * b["n"])
    return out


class IsotonicCalibrator:
    """Isotonic (monotonic, non-parametric) calibration via Pool-Adjacent-Violators.

    Fits a non-decreasing map raw_prob -> calibrated_prob, then predicts by linear
    interpolation between knots. The most flexible calibrator, but it needs plenty of
    data (rule of thumb >~1000 samples) or it overfits the calibration set.
    """

    def __init__(self):
        self._x: list[float] = []   # sorted knot raw probs
        self._y: list[float] = []   # fitted calibrated probs at the knots

    def fit(self, pairs: list[tuple[float, int]]) -> "IsotonicCalibrator":
        if not pairs:
            return self
        ordered = sorted(pairs, key=lambda t: t[0])
        xs = [p for p, _ in ordered]
        ys = [float(o) for _, o in ordered]
        fitted = _pav(ys, [1.0] * len(xs))
        knot_x: list[float] = []
        knot_y: list[float] = []
        for x, yv in zip(xs, fitted):
            if knot_x and abs(knot_x[-1] - x) < 1e-12:
                knot_y[-1] = yv          # keep the last fitted level at a repeated x
            else:
                knot_x.append(x)
                knot_y.append(yv)
        self._x, self._y = knot_x, knot_y
        return self

    def predict(self, p: float) -> float:
        if not self._x:
            return p
        if p <= self._x[0]:
            return self._y[0]
        if p >= self._x[-1]:
            return self._y[-1]
        lo, hi = 0, len(self._x) - 1
        while hi - lo > 1:
            mid = (lo + hi) // 2
            if self._x[mid] <= p:
                lo = mid
            else:
                hi = mid
        x0, x1 = self._x[lo], self._x[hi]
        y0, y1 = self._y[lo], self._y[hi]
        if x1 - x0 < 1e-12:
            return y1
        return y0 + (y1 - y0) * (p - x0) / (x1 - x0)

    def predict_all(self, ps: list[float]) -> list[float]:
        return [self.predict(p) for p in ps]


CALIBRATORS = {
    "temperature": TemperatureCalibrator,
    "platt": PlattCalibrator,
    "isotonic": IsotonicCalibrator,
}


def fit_calibrator(method: str, pairs: list[tuple[float, int]]):
    """Construct + fit a calibrator by name ('temperature'|'platt'|'isotonic')."""
    if method not in CALIBRATORS:
        raise ValueError(f"unknown calibrator '{method}' (have {sorted(CALIBRATORS)})")
    return CALIBRATORS[method]().fit(pairs)


def calibration_metrics(pairs: list[tuple[float, int]], nbins: int = 10) -> dict:
    """Bundle the calibration metrics for a set of (prob, outcome) pairs."""
    return {
        "n": len(pairs),
        "ece": ece(pairs, nbins),
        "mce": mce(pairs, nbins),
        "brier_decomposition": brier_decomposition(pairs, nbins),
        "reliability_diagram": reliability_diagram(pairs, nbins),
    }
