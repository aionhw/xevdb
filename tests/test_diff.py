"""Waveform diff — first-divergence logic + a two-dataset integration."""
from __future__ import annotations

from pathlib import Path

from xevdb import db as _db, diff as _diff
from xevdb.backends.sqlite_backend import SqliteBackend


# ---------------- pure first_divergence ----------------

def test_first_divergence_basic():
    a = [(0, "0"), (10, "1"), (20, "0")]
    b = [(0, "0"), (10, "1"), (20, "1")]
    assert _diff.first_divergence(a, b) == (20, "0", "1")


def test_first_divergence_none_when_equal():
    a = [(0, "0"), (10, "1")]
    assert _diff.first_divergence(a, a) is None


def test_first_divergence_ignores_leading_zeros():
    # pure-binary vectors compared by value, not text width
    a = [(0, "00000010")]
    b = [(0, "10")]
    assert _diff.first_divergence(a, b) is None


def test_first_divergence_xz_compared_raw():
    a = [(0, "0"), (5, "x")]
    b = [(0, "0"), (5, "0")]
    assert _diff.first_divergence(a, b) == (5, "x", "0")


def test_first_divergence_different_change_times():
    a = [(0, "0"), (30, "1")]            # becomes 1 at 30
    b = [(0, "0"), (20, "1")]            # becomes 1 at 20  -> diverge at 20
    assert _diff.first_divergence(a, b) == (20, "0", "1")


# ---------------- dataset integration (sqlite) ----------------

def _vcd(path: Path, changes):
    lines = ["$timescale 1ns $end", "$scope module top $end",
             "$var wire 8 ! sig [7:0] $end", "$upscope $end", "$enddefinitions $end"]
    for t, v in changes:
        lines += [f"#{t}", f"b{v} !"]
    path.write_text("\n".join(lines) + "\n")


def _build(tmp, name, changes):
    vcd = tmp / f"{name}.vcd"
    _vcd(vcd, changes)
    dbp = tmp / f"{name}.xevdb"
    _db.build(vcd, dbp, reset=True)
    return SqliteBackend(str(dbp))


def test_diff_datasets_finds_divergence(tmp_path):
    a = _build(tmp_path, "golden",
               [(0, "00000000"), (10, "00000001"), (20, "00000010")])
    b = _build(tmp_path, "dut",
               [(0, "00000000"), (10, "00000001"), (20, "00000011")])
    with a.open(read_only=True) as sa, b.open(read_only=True) as sb:
        res = _diff.diff_datasets(a, sa, b, sb)
    assert res["n_common"] == 1
    assert res["n_divergent"] == 1
    d = res["divergences"][0]
    assert d.t == 20 and d.fullname == "top.sig"
    assert d.value_a == "00000010" and d.value_b == "00000011"


def test_diff_datasets_identical_no_divergence(tmp_path):
    changes = [(0, "00000000"), (10, "00000001")]
    a = _build(tmp_path, "a", changes)
    b = _build(tmp_path, "b", changes)
    with a.open(read_only=True) as sa, b.open(read_only=True) as sb:
        res = _diff.diff_datasets(a, sa, b, sb)
    assert res["n_common"] == 1 and res["n_divergent"] == 0
