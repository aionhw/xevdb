"""Unit tests for xevdb.xztrace — X/Z tracing.

The xztrace functions take a plain sqlite connection over the standard
`signals` + `changes` schema, so the tests build a tiny in-memory DB
directly rather than going through a VCD fixture.

Run with: pytest tests/test_xztrace.py -v
"""
from __future__ import annotations

import sqlite3

import pytest

from xevdb import xztrace


@pytest.fixture
def con():
    """A 4-signal in-memory waveform DB exercising every X/Z pattern.

      sig a : clean the whole run
      sig b : X from t=0, leaves at t=20, re-enters at t=40 (open)
      sig c : clean until t=10, then X forever
      sig d : Z from t=5, leaves at t=30
    """
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(
        """
        CREATE TABLE signals (id, hier, name, fullname, width, kind);
        CREATE TABLE changes (sig_id, t, value);
        CREATE INDEX ix_changes ON changes(sig_id, t);
        """
    )
    c.executemany(
        "INSERT INTO signals VALUES (?,?,?,?,?,?)",
        [
            ("a", "top", "a", "top.a", 8, "wire"),
            ("b", "top", "b", "top.b", 8, "reg"),
            ("c", "top", "c", "top.c", 1, "wire"),
            ("d", "top", "d", "top.d", 4, "wire"),
        ],
    )
    c.executemany(
        "INSERT INTO changes VALUES (?,?,?)",
        [
            ("a", 0, "00000000"), ("a", 10, "00000001"), ("a", 20, "00000010"),
            ("b", 0, "x"), ("b", 20, "00000001"), ("b", 40, "xxxxxxxx"),
            ("c", 0, "0"), ("c", 10, "x"),
            ("d", 0, "0000"), ("d", 5, "zzzz"), ("d", 30, "1111"),
        ],
    )
    c.commit()
    return c


# ---------------- is_xz ----------------

def test_is_xz_detects_all_cases():
    assert xztrace.is_xz("x")
    assert xztrace.is_xz("0000x111")
    assert xztrace.is_xz("zzzz")
    assert xztrace.is_xz("X")          # uppercase
    assert not xztrace.is_xz("00001111")
    assert not xztrace.is_xz("")
    assert not xztrace.is_xz(None)


# ---------------- overview ----------------

def test_overview_counts(con):
    ov = xztrace.overview(con)
    assert ov.total_signals == 4
    assert ov.xz_signals == 3            # b, c, d — not a
    assert ov.first_xz_t == 0            # b and d... b at 0
    assert ov.last_xz_t == 40            # b re-enters at 40
    # b goes X at t=0; d goes Z at t=5 — only b is in the root set.
    assert ov.first_xz_signals == ["top.b"]


# ---------------- first ----------------

def test_first_ordering(con):
    rows = xztrace.first(con, limit=10)
    names = [r.fullname for r in rows]
    # ascending by first_xz_t: b@0, d@5, c@10
    assert names == ["top.b", "top.d", "top.c"]
    assert rows[0].first_xz_t == 0
    assert rows[1].first_xz_t == 5
    assert rows[1].xz_kind == "z"
    assert rows[2].first_xz_t == 10


def test_first_xz_change_count(con):
    rows = {r.fullname: r for r in xztrace.first(con, limit=10)}
    # b carries X/Z at t=0 and again at t=40 → 2 dirty changes
    assert rows["top.b"].xz_change_count == 2
    assert rows["top.c"].xz_change_count == 1


# ---------------- timeline ----------------

def test_timeline_closed_and_open_intervals(con):
    ivals = xztrace.timeline(con, "b")
    assert len(ivals) == 2
    # first interval: enter 0, leave 20
    assert ivals[0].enter_t == 0 and ivals[0].leave_t == 20
    assert ivals[0].leave_value == "00000001"
    # second interval: enter 40, still open
    assert ivals[1].enter_t == 40 and ivals[1].leave_t is None


def test_timeline_clean_signal_has_no_intervals(con):
    assert xztrace.timeline(con, "a") == []


def test_timeline_z_interval(con):
    ivals = xztrace.timeline(con, "d")
    assert len(ivals) == 1
    assert ivals[0].xz_kind == "z"
    assert ivals[0].enter_t == 5 and ivals[0].leave_t == 30


# ---------------- at ----------------

def test_at_waveform_semantic(con):
    # at t=7: b=x (since 0), c=0 (clean), d=z (since 5) → b, d dirty
    rows = xztrace.at(con, 7)
    names = sorted(r.fullname for r in rows)
    assert names == ["top.b", "top.d"]

    # at t=25: b left X at 20, d still z, c is x since 10 → c, d dirty
    rows = xztrace.at(con, 25)
    names = sorted(r.fullname for r in rows)
    assert names == ["top.c", "top.d"]

    # at t=45: b re-entered X at 40, c x, d clean since 30 → b, c dirty
    rows = xztrace.at(con, 45)
    names = sorted(r.fullname for r in rows)
    assert names == ["top.b", "top.c"]


def test_at_before_any_change(con):
    # Nothing has a change with t <= -1.
    assert xztrace.at(con, -1) == []


# ---------------- propagate ----------------

def test_propagate_orders_by_delta(con):
    seed_t, cands = xztrace.propagate(con, "b")    # b first X/Z at t=0
    assert seed_t == 0
    names = [c.fullname for c in cands]
    # everything after t>=0, excluding b itself: d@5, c@10
    assert names == ["top.d", "top.c"]
    assert cands[0].delta_t == 5     # d
    assert cands[1].delta_t == 10    # c


def test_propagate_window_filters(con):
    _, cands = xztrace.propagate(con, "b", window=5)
    # only candidates with first_t <= 0+5 → just d
    assert [c.fullname for c in cands] == ["top.d"]


def test_propagate_seed_never_xz(con):
    seed_t, cands = xztrace.propagate(con, "a")
    assert seed_t == -1
    assert cands == []


# ---------------- to_dict ----------------

def test_to_dict_roundtrips_dataclasses(con):
    rows = xztrace.first(con, limit=2)
    d = xztrace.to_dict(rows)
    assert isinstance(d, list)
    assert isinstance(d[0], dict)
    assert d[0]["fullname"] == "top.b"
