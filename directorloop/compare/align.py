"""Align equivalent story beats of two edits of the same idea.

Identical timestamps rarely mean identical content: one edit may merge two sentences, drop one or pace them differently.
Units come from a monotonic dynamic-programming alignment over sentence beats that allows one-to-one, two-to-one and
one-to-two matches plus sentences present in only one edit (a Gale-Church style sentence alignment on word sequences).
Without speech on both sides, units fall back to equal fractions of each edit's duration and say so.
"""

from __future__ import annotations

import difflib
import re
from typing import Any

from .models import AlignedUnit

MIN_SIMILARITY = 0.35
SHARED_SIMILARITY = 0.85
MERGE_PENALTY = 0.5  # in matched words: a merge must match more words than a one-to-one match plus a gap


def tokens(text: str) -> list[str]:
    return re.findall(r"[a-z0-9']+", text.lower())


def text_similarity(a: str, b: str) -> float:
    ta, tb = tokens(a), tokens(b)
    if not ta or not tb:
        return 0.0
    return difflib.SequenceMatcher(None, ta, tb, autojunk=False).ratio()


def matched_tokens(a: str, b: str) -> int:
    ta, tb = tokens(a), tokens(b)
    if not ta or not tb:
        return 0
    return sum(m.size for m in difflib.SequenceMatcher(None, ta, tb, autojunk=False).get_matching_blocks())


def align_beats(a: list[Any], b: list[Any], min_similarity: float = MIN_SIMILARITY) -> list[AlignedUnit]:
    """a, b: sentence beats with id, text, start_ms, end_ms (TimelineBeat)."""
    if not any(tokens(x.text) for x in a) or not any(tokens(x.text) for x in b):
        return _time_aligned(a, b)
    n, m = len(a), len(b)
    neg = float("-inf")
    dp = [[neg] * (m + 1) for _ in range(n + 1)]
    back: list[list[tuple[int, int, float | None] | None]] = [[None] * (m + 1) for _ in range(n + 1)]
    dp[0][0] = 0.0
    for i in range(n + 1):
        for j in range(m + 1):
            if dp[i][j] == neg:
                continue
            for di, dj in ((1, 1), (2, 1), (1, 2), (1, 0), (0, 1)):
                ni, nj = i + di, j + dj
                if ni > n or nj > m:
                    continue
                sim: float | None = None
                gain = 0.0
                if di and dj:
                    ta = " ".join(x.text for x in a[i:ni])
                    tb = " ".join(x.text for x in b[j:nj])
                    sim = text_similarity(ta, tb)
                    if sim < min_similarity:
                        continue
                    # score matched words, not length: folding an unmatched sentence into a match must not pay
                    gain = matched_tokens(ta, tb) - (MERGE_PENALTY if (di, dj) != (1, 1) else 0.0)
                if dp[i][j] + gain > dp[ni][nj]:
                    dp[ni][nj] = dp[i][j] + gain
                    back[ni][nj] = (i, j, sim)
    steps: list[tuple[int, int, int, int, float | None]] = []
    i, j = n, m
    while (i, j) != (0, 0):
        prev = back[i][j]
        assert prev is not None, "alignment backtrack broke"
        pi, pj, sim = prev
        steps.append((pi, i, pj, j, sim))
        i, j = pi, pj
    units: list[AlignedUnit] = []
    for k, (i0, i1, j0, j1, sim) in enumerate(reversed(steps)):
        aa, bb = a[i0:i1], b[j0:j1]
        if aa and bb:
            kind = "shared" if (sim or 0) >= SHARED_SIMILARITY else "reworded"
        else:
            kind = "only_a" if aa else "only_b"
        units.append(AlignedUnit(
            index=k, kind=kind, a_beats=[x.id for x in aa], b_beats=[x.id for x in bb],
            a_start_ms=aa[0].start_ms if aa else None, a_end_ms=aa[-1].end_ms if aa else None,
            b_start_ms=bb[0].start_ms if bb else None, b_end_ms=bb[-1].end_ms if bb else None,
            text_a=" ".join(x.text for x in aa).strip(), text_b=" ".join(x.text for x in bb).strip(),
            similarity=round(sim, 3) if sim is not None else None,
        ))
    return units


def _time_aligned(a: list[Any], b: list[Any]) -> list[AlignedUnit]:
    if not a or not b:
        return []
    a_end, b_end = a[-1].end_ms, b[-1].end_ms
    k = max(1, min(4, round(max(a_end, b_end) / 3000)))
    out = []
    for i in range(k):
        out.append(AlignedUnit(index=i, kind="time_aligned", a_start_ms=a_end * i // k, a_end_ms=a_end * (i + 1) // k, b_start_ms=b_end * i // k,
                               b_end_ms=b_end * (i + 1) // k, text_a="", text_b="", similarity=None,
                               a_beats=[x.id for x in a if x.start_ms < a_end * (i + 1) // k and x.end_ms > a_end * i // k],
                               b_beats=[x.id for x in b if x.start_ms < b_end * (i + 1) // k and x.end_ms > b_end * i // k]))
    return out
