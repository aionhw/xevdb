"""End-to-end smoke for VCD + RTL pipeline against examples/counter.{vcd,sv}.

Run with: pytest tests/ -v
"""
from __future__ import annotations

from pathlib import Path

import pytest

from xevdb import db, prompts, cache, show, sv


REPO = Path(__file__).resolve().parents[1]
VCD = REPO / "examples" / "counter.vcd"
SV = REPO / "examples" / "counter.sv"


# ---------------- VCD-side ----------------

@pytest.fixture
def xevdb_file(tmp_path):
    out = tmp_path / "counter.xevdb"
    db.build(VCD, out, reset=True)
    return out


def test_build_writes_signals_and_changes(xevdb_file):
    with db.open_db(xevdb_file, read_only=True) as con:
        s = db.stats(con)
    assert int(s["n_signals"]) == 4
    assert int(s["n_changes"]) == 16
    assert s["row_counts"]["prompts"] >= 14


def test_value_at_correct_semantic(xevdb_file):
    with db.open_db(xevdb_file, read_only=True) as con:
        sig = db.resolve_signal(con, "count")
        last_t, val = db.value_at(con, sig.sig_id, 20)
    assert last_t == 15
    assert val == "00000001"


def test_seed_prompts(xevdb_file):
    with db.open_db(xevdb_file, read_only=True) as con:
        names = [p.name for p in prompts.list_prompts(con)]
    # VCD set
    for n in ("signal_transitions", "change_count", "stuck_at", "xz_signals",
              "value_at_many", "signal_history", "clock_period", "signals_in_scope"):
        assert n in names
    # RTL set
    for n in ("list_modules", "ports_of_module", "signals_of_module",
              "signal_declaration", "modules_in_file", "instance_tree",
              "xz_signals_with_rtl"):
        assert n in names


# ---------------- RTL-side ----------------

@pytest.fixture
def xevdb_with_rtl(xevdb_file):
    if not sv.have_sv_parse():
        pytest.skip("sv-parse binary not built; run `cargo build --release` "
                    "in xezim-core/xezim-parser/")
    db.ingest_rtl(SV, xevdb_file, reset=True)
    return xevdb_file


def test_rtl_ingest_counter(xevdb_with_rtl):
    with db.open_db(xevdb_with_rtl, read_only=True) as con:
        rows = con.execute(
            "SELECT name, line_start, line_end FROM modules ORDER BY line_start"
        ).fetchall()
    names = [r["name"] for r in rows]
    assert "counter" in names
    assert "top" in names


def test_rtl_ports_extracted(xevdb_with_rtl):
    with db.open_db(xevdb_with_rtl, read_only=True) as con:
        rows = con.execute(
            "SELECT p.name, p.direction FROM module_ports p "
            "JOIN modules m ON m.id = p.module_id "
            "WHERE m.name = 'counter' ORDER BY p.position"
        ).fetchall()
    names = [r["name"] for r in rows]
    assert names == ["clk", "rst", "en", "count"]
    dirs = [r["direction"] for r in rows]
    assert dirs == ["input", "input", "input", "output"]


def test_show_module(xevdb_with_rtl):
    with db.open_db(xevdb_with_rtl, read_only=True) as con:
        slices = show.show_code(con, "counter", context=4)
    assert len(slices) == 1
    assert "module counter" in slices[0].text
    assert slices[0].module_name == "counter"


def test_show_signal_by_name(xevdb_with_rtl):
    # 'count' is both a port name AND a (technically declared via output) signal.
    # show_code should resolve it; we accept either a port or a signal match.
    with db.open_db(xevdb_with_rtl, read_only=True) as con:
        slices = show.show_code(con, "count")
    assert slices, "expected to find code for signal 'count'"
    assert any("count" in s.text for s in slices)


def test_show_file_line(xevdb_with_rtl):
    with db.open_db(xevdb_with_rtl, read_only=True) as con:
        rows = con.execute(
            "SELECT file, line_start FROM modules WHERE name = 'counter'"
        ).fetchall()
        file = rows[0]["file"]
        target = f"{file}:{rows[0]['line_start']}"
        slices = show.show_code(con, target, context=2)
    assert len(slices) == 1
    assert "module counter" in slices[0].text


def test_prompt_signal_declaration(xevdb_with_rtl):
    with db.open_db(xevdb_with_rtl, read_only=False) as con:
        rows, _ = prompts.run_prompt(con, "signal_declaration", {"name": "count"})
    assert rows, "expected at least one match for 'count'"
    # Each row has 'where_' field ('signal' or 'port')
    wheres = {r["where_"] for r in rows}
    assert wheres & {"signal", "port"}


def test_prompt_list_modules_and_cache(xevdb_with_rtl):
    with db.open_db(xevdb_with_rtl, read_only=False) as con:
        rows1, hit1 = prompts.run_prompt(con, "list_modules", {"limit": 10})
        rows2, hit2 = prompts.run_prompt(con, "list_modules", {"limit": 10})
    assert hit1 is False
    assert hit2 is True
    assert rows1 == rows2
    assert len(rows1) >= 2


# ---------------- Sim-side ----------------

from xevdb import sim                                                # noqa: E402
SIM_LOG = REPO / "examples" / "sim.log"


def test_sim_parser_extracts_severity_time_ref():
    events = sim.parse_file(SIM_LOG)
    assert len(events) == 14
    counts = sim.severity_counts(events)
    assert counts.get("UVM_FATAL", 0) == 1
    assert counts.get("UVM_ERROR", 0) == 3
    assert counts.get("UVM_WARNING", 0) == 3
    assert counts.get("ASSERTION", 0) == 1
    # Line 3 has 'at t=200ns' — should be extracted by the t= pattern.
    by_line = {e.line_no: e for e in events}
    assert by_line[3].t == 200
    # Line 1 has '@ 0:' — UVM canonical time prefix.
    assert by_line[1].t == 0
    # Line 8 references picorv32.v:176
    assert by_line[8].ref_file == "picorv32.v"
    assert by_line[8].ref_line == 176


def test_sim_ingest_populates_runs_and_events(xevdb_with_rtl):
    result = db.ingest_sim(SIM_LOG, xevdb_with_rtl, reset=True)
    assert result["events"] == 14
    assert result["fatal"] == 1
    assert result["error"] >= 3        # UVM_ERROR + ASSERTION + ERROR
    assert result["warning"] == 3
    with db.open_db(xevdb_with_rtl, read_only=True) as con:
        n_runs = con.execute("SELECT COUNT(*) FROM sim_runs").fetchone()[0]
        n_events = con.execute("SELECT COUNT(*) FROM sim_events").fetchone()[0]
    assert n_runs == 1
    assert n_events == 14


def test_sim_seed_prompts_present(xevdb_with_rtl):
    with db.open_db(xevdb_with_rtl, read_only=True) as con:
        names = {p.name for p in prompts.list_prompts(con)}
    for n in ("sim_summary", "sim_errors", "sim_around_time",
              "sim_by_ref_file", "sim_with_rtl"):
        assert n in names


def test_sim_with_rtl_bridge(xevdb_with_rtl):
    """sim_with_rtl should join the sim log's picorv32.v:176 ref with the
    counter module (which is in counter.sv). Since the fixture RTL doesn't
    include picorv32.v, we expect zero rows here — but the prompt must run
    cleanly. The bridge is exercised end-to-end in the picorv32 demo."""
    db.ingest_sim(SIM_LOG, xevdb_with_rtl, reset=True)
    with db.open_db(xevdb_with_rtl, read_only=False) as con:
        rows, _ = prompts.run_prompt(con, "sim_with_rtl", {"limit": 5})
    assert isinstance(rows, list)  # query ran; row count depends on RTL ingest contents
