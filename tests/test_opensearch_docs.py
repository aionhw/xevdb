"""OpenSearch document builder (Phase 3) — denormalization, no cluster needed."""
from __future__ import annotations

from collections import Counter
from pathlib import Path

import pytest

from xevdb import parser, sim, sv
from xevdb.backends import opensearch_docs as docs


REPO = Path(__file__).resolve().parents[1]
VCD = REPO / "examples" / "counter.vcd"
SV = REPO / "examples" / "counter.sv"
SIM_LOG = REPO / "examples" / "sim.log"


def _by_table(actions):
    out: dict[str, list] = {}
    for a in actions:
        out.setdefault(a.table, []).append(a)
    return out


# ---------------- xz flag ----------------

def test_xz_dirty_matches_like_set():
    assert docs.xz_dirty("0001xz".replace("z", "z"))  # has x
    assert docs.xz_dirty("00X0")
    assert docs.xz_dirty("z")
    assert not docs.xz_dirty("00010111")
    assert not docs.xz_dirty("r1.5")


# ---------------- VCD ----------------

def test_vcd_actions_signals_and_meta():
    vcd = parser.parse_file(VCD)
    tables = _by_table(docs.vcd_actions(vcd, source=str(VCD)))
    assert len(tables["signals"]) == 4
    assert len(tables["changes"]) == 16
    meta = {a.id: a.source["value"] for a in tables["meta"]}
    assert meta["n_signals"] == "4"
    assert meta["n_changes"] == "16"
    # signal docs are keyed by VCD id
    assert all(a.id == a.source["id"] for a in tables["signals"])


def test_changes_are_denormalized():
    vcd = parser.parse_file(VCD)
    changes = _by_table(docs.vcd_actions(vcd))["changes"]
    # every change carries the precomputed xz flag and (when resolvable) the
    # signal's fullname — the join-free fields Strategy-B prompts use.
    for a in changes:
        assert "xz" in a.source
        assert isinstance(a.source["xz"], bool)
        assert "fullname" in a.source     # counter.vcd ids all resolve
    # at least one change should match the signal's expected fullname form
    assert any("." in a.source["fullname"] for a in changes)


# ---------------- RTL ----------------

@pytest.mark.skipif(not sv.have_sv_parse(), reason="sv-parse binary not built")
def test_rtl_actions_denormalize_module_name():
    files = list(sv.walk_rtl(SV))
    tables = _by_table(docs.rtl_actions(files, now=1.0))
    names = {a.source["name"] for a in tables["modules"]}
    assert "counter" in names
    # module id is the stable string key, and children reference it
    mids = {a.id for a in tables["modules"]}
    for port in tables["module_ports"]:
        assert port.source["module_id"] in mids
        assert port.source["module_name"]          # denorm present
    # the counter ports carry their module name without a join
    counter_ports = [p for p in tables["module_ports"]
                     if p.source["module_name"] == "counter"]
    assert {p.source["name"] for p in counter_ports} >= {"clk", "rst", "en", "count"}


# ---------------- sim ----------------

def test_sim_actions_run_and_events():
    events = sim.parse_file(SIM_LOG)
    counts = sim.severity_counts(events)
    acts = list(docs.sim_actions(
        events, run_name="sim.log", source=str(SIM_LOG), now=42.0,
        line_count=20, counts=counts))
    tabs = _by_table(acts)
    assert len(tabs["sim_runs"]) == 1
    run = tabs["sim_runs"][0]
    assert run.source["n_fatal"] == 1
    assert run.source["n_events"] == len(events)
    run_id = run.id
    # every event links back to the one run
    assert {e.source["run_id"] for e in tabs["sim_events"]} == {run_id}
    assert len(tabs["sim_events"]) == len(events)


# ---------------- prompts ----------------

def test_prompt_actions_keyed_by_name():
    from xevdb import seed_prompts
    acts = list(docs.prompt_actions(seed_prompts.PROMPTS, now=1.0))
    assert len(acts) == len(seed_prompts.PROMPTS)
    assert all(a.id == a.source["name"] for a in acts)
    # dual representation field is present (empty until Phase 4 authors DSL)
    assert all("dsl_json" in a.source for a in acts)
    # ids are unique
    assert len(acts) == len(Counter(a.id for a in acts))
