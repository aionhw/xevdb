"""Waveform diff — find where two datasets first diverge.

Golden-vs-DUT regression triage: for every signal common to both datasets
(matched by fullname), find the earliest time their value-in-effect differs.
Backend-agnostic — it only uses ``find_signals`` + ``window``, so it works on
SQLite files and (within the cluster's result window) on OpenSearch.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class Divergence:
    fullname: str
    t: int
    value_a: str | None
    value_b: str | None

    def to_dict(self) -> dict[str, Any]:
        return {"fullname": self.fullname, "t": self.t,
                "value_a": self.value_a, "value_b": self.value_b}


def _norm(v: str | None) -> str | None:
    """Normalize a value so leading-zero differences in pure-binary vectors
    aren't reported as divergences ('00010' == '10'); x/z/reals compared raw."""
    if v is None:
        return None
    if v and set(v) <= {"0", "1"}:
        return v.lstrip("0") or "0"
    return v


def first_divergence(
    a: list[tuple[int, str]], b: list[tuple[int, str]]
) -> tuple[int, str | None, str | None] | None:
    """First time the step-functions defined by change lists `a`/`b` differ.

    Both lists are (t, value) sorted ascending by t. Returns (t, va, vb) or None.
    """
    times = sorted({t for t, _ in a} | {t for t, _ in b})
    ia = ib = 0
    va = vb = None
    for t in times:
        while ia < len(a) and a[ia][0] <= t:
            va = a[ia][1]
            ia += 1
        while ib < len(b) and b[ib][0] <= t:
            vb = b[ib][1]
            ib += 1
        if _norm(va) != _norm(vb):
            return (t, va, vb)
    return None


def diff_datasets(
    backend_a: Any, session_a: Any, backend_b: Any, session_b: Any, *,
    pattern: str = "*", t0: int | None = None, t1: int | None = None,
    limit: int = 50, max_changes: int = 100_000,
) -> dict[str, Any]:
    """Diff two datasets. Returns divergences (earliest first) + coverage stats."""
    sigs_a = {s.fullname: s for s in backend_a.find_signals(session_a, pattern, 10**9)}
    sigs_b = {s.fullname: s for s in backend_b.find_signals(session_b, pattern, 10**9)}
    common = sorted(set(sigs_a) & set(sigs_b))

    divergences: list[Divergence] = []
    for fn in common:
        ca = backend_a.window(session_a, sigs_a[fn].sig_id, t0, t1, max_changes)
        cb = backend_b.window(session_b, sigs_b[fn].sig_id, t0, t1, max_changes)
        d = first_divergence(ca, cb)
        if d is not None:
            divergences.append(Divergence(fn, d[0], d[1], d[2]))

    divergences.sort(key=lambda d: (d.t, d.fullname))
    return {
        "divergences": divergences[:limit],
        "n_divergent": len(divergences),
        "n_common": len(common),
        "only_in_a": sorted(set(sigs_a) - set(sigs_b)),
        "only_in_b": sorted(set(sigs_b) - set(sigs_a)),
    }
