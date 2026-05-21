"""X/Z tracing for xevdb — find, time-line, and propagation-trace the
unknown (`x`) and high-impedance (`z`) states in an ingested VCD.

A `.xevdb` already stores every value change as a string in
`changes.value`. A value is *X/Z-dirty* when it contains any of
`x X z Z`. This module turns that raw fact into the questions a
debugger actually asks:

* **overview**  — how widespread is the X/Z, and when does it start?
* **first**     — which signals go X/Z *earliest*? (root-cause set)
* **timeline**  — for one signal, the enter/leave intervals.
* **at**        — every signal sitting in X/Z at an instant.
* **propagate** — given a seed signal, the downstream signals that
                  turn X/Z *after* it — propagation candidates.

Pure SQL over the existing schema; no new tables. Read-only.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any, Sequence

# A value is X/Z-dirty if it carries any of these characters.
_XZ_CHARS = ("x", "X", "z", "Z")

# SQL fragment: TRUE when `changes.value` carries an X or Z bit.
_XZ_PRED = (
    "(value LIKE '%x%' OR value LIKE '%X%' "
    " OR value LIKE '%z%' OR value LIKE '%Z%')"
)


def is_xz(value: str | None) -> bool:
    """True when a VCD value string carries any X or Z bit."""
    if value is None:
        return False
    return any(c in value for c in _XZ_CHARS)


def _xz_kind(value: str) -> str:
    """Classify a dirty value: 'x', 'z', or 'xz' (both present)."""
    has_x = "x" in value or "X" in value
    has_z = "z" in value or "Z" in value
    if has_x and has_z:
        return "xz"
    return "x" if has_x else "z"


# ---------------------------------------------------------------------------
# overview
# ---------------------------------------------------------------------------

@dataclass
class XzOverview:
    total_signals: int
    xz_signals: int
    xz_changes: int
    total_changes: int
    first_xz_t: int | None
    last_xz_t: int | None
    first_xz_signals: list[str]   # signals tied for the earliest X/Z time


def overview(con) -> XzOverview:
    total_signals = con.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
    total_changes = con.execute("SELECT COUNT(*) FROM changes").fetchone()[0]
    xz_changes = con.execute(
        f"SELECT COUNT(*) FROM changes WHERE {_XZ_PRED}"
    ).fetchone()[0]
    xz_signals = con.execute(
        f"SELECT COUNT(DISTINCT sig_id) FROM changes WHERE {_XZ_PRED}"
    ).fetchone()[0]
    row = con.execute(
        f"SELECT MIN(t), MAX(t) FROM changes WHERE {_XZ_PRED}"
    ).fetchone()
    first_t, last_t = (row[0], row[1]) if row else (None, None)
    first_sigs: list[str] = []
    if first_t is not None:
        first_sigs = [
            r[0] for r in con.execute(
                f"SELECT DISTINCT s.fullname FROM changes c "
                f"JOIN signals s ON s.id = c.sig_id "
                f"WHERE c.t = ? AND {_XZ_PRED.replace('value', 'c.value')} "
                f"ORDER BY s.fullname LIMIT 50",
                [first_t],
            )
        ]
    return XzOverview(
        total_signals=total_signals,
        xz_signals=xz_signals,
        xz_changes=xz_changes,
        total_changes=total_changes,
        first_xz_t=first_t,
        last_xz_t=last_t,
        first_xz_signals=first_sigs,
    )


# ---------------------------------------------------------------------------
# first — earliest signals to go X/Z (root-cause candidates)
# ---------------------------------------------------------------------------

@dataclass
class XzFirst:
    fullname: str
    width: int
    kind: str           # VCD signal kind (wire/reg)
    first_xz_t: int     # earliest time this signal carried X/Z
    first_value: str    # the dirty value at that time
    xz_kind: str        # 'x' | 'z' | 'xz'
    xz_change_count: int  # how many X/Z transitions over the whole run


def first(con, limit: int = 50) -> list[XzFirst]:
    """Signals ranked by the time they FIRST went X/Z (ascending).

    The earliest entries are the most likely root causes — everything
    downstream inherits X/Z from them.
    """
    rows = con.execute(
        f"""
        WITH xz AS (
            SELECT sig_id, t, value FROM changes WHERE {_XZ_PRED}
        ),
        firsts AS (
            SELECT sig_id, MIN(t) AS first_t FROM xz GROUP BY sig_id
        )
        SELECT s.fullname, s.width, s.kind,
               f.first_t,
               (SELECT value FROM xz WHERE xz.sig_id = f.sig_id
                  AND xz.t = f.first_t LIMIT 1) AS first_value,
               (SELECT COUNT(*) FROM xz WHERE xz.sig_id = f.sig_id) AS n
        FROM firsts f JOIN signals s ON s.id = f.sig_id
        ORDER BY f.first_t ASC, s.fullname ASC
        LIMIT ?
        """,
        [limit],
    ).fetchall()
    out: list[XzFirst] = []
    for fullname, width, kind, first_t, first_value, n in rows:
        out.append(XzFirst(
            fullname=fullname, width=width, kind=kind,
            first_xz_t=first_t, first_value=first_value,
            xz_kind=_xz_kind(first_value or ""), xz_change_count=n,
        ))
    return out


# ---------------------------------------------------------------------------
# timeline — enter/leave intervals for one signal
# ---------------------------------------------------------------------------

@dataclass
class XzInterval:
    enter_t: int
    leave_t: int | None    # None = still X/Z at end of trace
    enter_value: str
    leave_value: str | None
    xz_kind: str


def timeline(con, sig_id: str) -> list[XzInterval]:
    """Walk a signal's value changes, pairing each clean→dirty transition
    with the next dirty→clean one. An open interval (leave_t=None) means
    the signal was still X/Z when the trace ended.
    """
    rows = con.execute(
        "SELECT t, value FROM changes WHERE sig_id = ? ORDER BY t ASC",
        [sig_id],
    ).fetchall()
    intervals: list[XzInterval] = []
    cur: XzInterval | None = None
    for t, value in rows:
        dirty = is_xz(value)
        if dirty and cur is None:
            cur = XzInterval(enter_t=t, leave_t=None,
                             enter_value=value, leave_value=None,
                             xz_kind=_xz_kind(value))
        elif not dirty and cur is not None:
            cur.leave_t = t
            cur.leave_value = value
            intervals.append(cur)
            cur = None
    if cur is not None:
        intervals.append(cur)
    return intervals


# ---------------------------------------------------------------------------
# at — every signal in X/Z at an instant
# ---------------------------------------------------------------------------

@dataclass
class XzAt:
    fullname: str
    width: int
    kind: str
    value: str
    since_t: int        # when this X/Z value was set
    xz_kind: str


def at(con, t: int, limit: int = 200) -> list[XzAt]:
    """Every signal whose last value at or before time `t` is X/Z-dirty.

    Uses the correct waveform semantic: the value in effect at `t` is the
    last change with `change.t <= t`, NOT a literal grep at `t`.
    """
    rows = con.execute(
        """
        SELECT s.fullname, s.width, s.kind, lv.value, lv.t
        FROM signals s
        JOIN (
            SELECT c.sig_id, c.value, c.t
            FROM changes c
            JOIN (
                SELECT sig_id, MAX(t) AS mt
                FROM changes WHERE t <= ? GROUP BY sig_id
            ) last ON last.sig_id = c.sig_id AND last.mt = c.t
        ) lv ON lv.sig_id = s.id
        ORDER BY s.fullname
        """,
        [t],
    ).fetchall()
    out: list[XzAt] = []
    for fullname, width, kind, value, since_t in rows:
        if is_xz(value):
            out.append(XzAt(
                fullname=fullname, width=width, kind=kind,
                value=value, since_t=since_t, xz_kind=_xz_kind(value),
            ))
        if len(out) >= limit:
            break
    return out


# ---------------------------------------------------------------------------
# propagate — downstream signals that go X/Z after a seed
# ---------------------------------------------------------------------------

@dataclass
class XzProp:
    fullname: str
    width: int
    kind: str
    first_xz_t: int
    delta_t: int            # first_xz_t - seed_first_xz_t (>= 0)
    first_value: str
    xz_kind: str
    rtl_module: str | None  # module that declares a same-named signal
    rtl_file: str | None
    rtl_line: int | None


def propagate(con, seed_sig_id: str, window: int | None = None,
              limit: int = 100) -> tuple[int, list[XzProp]]:
    """Given a seed signal, find signals that turn X/Z at or after the
    seed's first X/Z time — propagation candidates.

    Returns `(seed_first_xz_t, candidates)`. Candidates are ranked by how
    soon after the seed they went dirty (`delta_t` ascending). When
    `window` is set, only candidates within `[seed_t, seed_t+window]` are
    returned. The seed signal itself is excluded.

    Each candidate is left-joined against `module_signals` so, if the
    `.xevdb` also has RTL ingested, you see which module the signal is
    declared in — the bridge from "this went X" to "here is the line".
    """
    seed_row = con.execute(
        f"SELECT MIN(t) FROM changes WHERE sig_id = ? AND {_XZ_PRED}",
        [seed_sig_id],
    ).fetchone()
    if seed_row is None or seed_row[0] is None:
        return (-1, [])
    seed_t = seed_row[0]
    hi = seed_t + window if window is not None else None

    where_window = "AND f.first_t <= ?" if hi is not None else ""
    params: list[Any] = [seed_sig_id, seed_t]
    if hi is not None:
        params.append(hi)
    params.append(limit)

    rows = con.execute(
        f"""
        WITH xz AS (
            SELECT sig_id, t, value FROM changes WHERE {_XZ_PRED}
        ),
        firsts AS (
            SELECT sig_id, MIN(t) AS first_t FROM xz GROUP BY sig_id
        )
        SELECT s.fullname, s.width, s.kind, s.name,
               f.first_t,
               (SELECT value FROM xz WHERE xz.sig_id = f.sig_id
                  AND xz.t = f.first_t LIMIT 1) AS first_value
        FROM firsts f JOIN signals s ON s.id = f.sig_id
        WHERE f.sig_id != ? AND f.first_t >= ? {where_window}
        ORDER BY f.first_t ASC, s.fullname ASC
        LIMIT ?
        """,
        params,
    ).fetchall()

    # Does the DB have RTL ingested? If so, resolve declarations.
    has_rtl = con.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='table' AND name='module_signals'"
    ).fetchone() is not None

    out: list[XzProp] = []
    for fullname, width, kind, name, first_t, first_value in rows:
        module = file = line = None
        if has_rtl:
            bare = name.split("[")[0]
            decl = con.execute(
                "SELECT m.name, m.file, ms.line "
                "FROM module_signals ms JOIN modules m ON m.id = ms.module_id "
                "WHERE ms.name = ? LIMIT 1",
                [bare],
            ).fetchone()
            if decl:
                module, file, line = decl
        out.append(XzProp(
            fullname=fullname, width=width, kind=kind,
            first_xz_t=first_t, delta_t=first_t - seed_t,
            first_value=first_value or "",
            xz_kind=_xz_kind(first_value or ""),
            rtl_module=module, rtl_file=file, rtl_line=line,
        ))
    return (seed_t, out)


# ---------------------------------------------------------------------------
# JSON helpers
# ---------------------------------------------------------------------------

def to_dict(obj: Any) -> Any:
    """Recursively convert dataclasses to plain dicts for json.dumps."""
    if hasattr(obj, "__dataclass_fields__"):
        return {k: to_dict(v) for k, v in asdict(obj).items()}
    if isinstance(obj, (list, tuple)):
        return [to_dict(x) for x in obj]
    return obj
