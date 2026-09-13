"""Small-sample evaluation with explicit missingness and sample sizes."""
from __future__ import annotations

import math
from collections.abc import Iterable

import numpy as np


def finite(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def ranks(values: list[float]) -> list[float]:
    """Average ranks for ties; tied posts do not receive arbitrary ordering."""
    order = sorted(range(len(values)), key=values.__getitem__)
    result = [0.0] * len(values)
    start = 0
    while start < len(order):
        end = start + 1
        while end < len(order) and values[order[end]] == values[order[start]]:
            end += 1
        for i in order[start:end]:
            result[i] = (start + end - 1) / 2 + 1
        start = end
    return result


def correlation(x: list[float], y: list[float]) -> float | None:
    if len(x) < 3 or len(set(x)) < 2 or len(set(y)) < 2:
        return None
    return float(np.corrcoef(x, y)[0, 1])


def paired_metrics(predicted: Iterable[object], actual: Iterable[object]) -> dict:
    pairs = [(float(p), float(a)) for p, a in zip(predicted, actual, strict=True) if finite(p) and finite(a)]
    if not pairs:
        return {"n": 0, "mae": None, "median_absolute_error": None, "rmse": None,
                "pearson": None, "spearman": None, "pairwise_accuracy": None, "pairwise_n": 0}
    p, a = map(list, zip(*pairs, strict=True))
    errors = np.abs(np.asarray(p) - np.asarray(a))
    comparisons = [(p[i] - p[j], a[i] - a[j]) for i in range(len(a)) for j in range(i) if a[i] != a[j]]
    accuracy = sum(0.5 if dp == 0 else float(dp * da > 0) for dp, da in comparisons) / len(comparisons) if comparisons else None
    return {"n": len(pairs), "mae": float(errors.mean()), "median_absolute_error": float(np.median(errors)),
            "rmse": float(np.sqrt(np.mean(errors ** 2))), "pearson": correlation(p, a),
            "spearman": correlation(ranks(p), ranks(a)), "pairwise_accuracy": accuracy, "pairwise_n": len(comparisons)}


def residual_radius(residuals: list[float], level: float = 0.8) -> float | None:
    """Finite-sample split-conformal order statistic; None means an unbounded interval.

    Coverage still requires exchangeability. Historical platform drift may violate it.
    """
    if not 0 < level < 1:
        raise ValueError("interval level must be between zero and one")
    k = math.ceil((len(residuals) + 1) * level)
    return sorted(residuals)[k - 1] if residuals and k <= len(residuals) else None


def interval_metrics(rows: list[dict]) -> dict:
    eligible = [r for r in rows if all(finite(r.get(k)) for k in ("actual", "lower", "upper"))]
    return {"n": len(eligible), "coverage": sum(r["lower"] <= r["actual"] <= r["upper"] for r in eligible) / len(eligible) if eligible else None,
            "mean_width": float(np.mean([r["upper"] - r["lower"] for r in eligible])) if eligible else None,
            "unbounded_or_missing_n": len(rows) - len(eligible)}


def _union(intervals: list[tuple[float, float]]) -> list[tuple[float, float]]:
    result: list[tuple[float, float]] = []
    for start, end in sorted((a, b) for a, b in intervals if b > a):
        if result and start <= result[-1][1]:
            result[-1] = (result[-1][0], max(end, result[-1][1]))
        else:
            result.append((start, end))
    return result


def temporal_overlap(predicted: list[tuple[float, float]], observed: list[tuple[float, float]]) -> dict:
    p, o = _union(predicted), _union(observed)
    intersection = sum(max(0, min(b, d) - max(a, c)) for a, b in p for c, d in o)
    p_length, o_length = sum(b - a for a, b in p), sum(b - a for a, b in o)
    union = p_length + o_length - intersection
    return {"predicted_intervals": len(p), "observed_intervals": len(o), "intersection_ms": intersection,
            "iou": intersection / union if union else None,
            "observed_window_coverage": intersection / o_length if o_length else None,
            "predicted_window_precision": intersection / p_length if p_length else None}


def retention_drop_windows(points: list[dict], top_k: int = 1) -> list[dict]:
    """Observed largest per-second decline; this does not infer a curve from averages."""
    valid = sorted((p for p in points if finite(p.get("timestamp_ms")) and finite(p.get("retained_fraction"))), key=lambda p: p["timestamp_ms"])
    windows = []
    for left, right in zip(valid, valid[1:], strict=False):
        dt = right["timestamp_ms"] - left["timestamp_ms"]
        if dt <= 0:
            continue
        lost = max(0, left["retained_fraction"] - right["retained_fraction"])
        if lost:
            windows.append({"start_ms": left["timestamp_ms"], "end_ms": right["timestamp_ms"], "lost_fraction": lost,
                            "lost_fraction_per_second": lost * 1000 / dt})
    return sorted(windows, key=lambda w: w["lost_fraction_per_second"], reverse=True)[:top_k]
