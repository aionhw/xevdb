"""Bug knowledge base — CRUD (B1) + full-text search (B2)."""
from __future__ import annotations

from pathlib import Path

import pytest

from xevdb import db, bugs, backends


REPO = Path(__file__).resolve().parents[1]
VCD = REPO / "examples" / "simple" / "counter.vcd"


@pytest.fixture
def built(tmp_path):
    out = tmp_path / "c.xevdb"
    db.build(VCD, out, reset=True)
    return out


def _sample(con, name="AXI FIFO X-prop", **kw):
    defaults = dict(
        severity="error",
        symptom="fifo_full goes X after reset deassert",
        root_cause="uninitialized mem_inst_temp read past populated range",
        fix="pre-init temp arrays to 0 before readmemh",
        fix_ref="abc123",
        keywords=["axi", "xprop"],
        tags=["rtl"],
        links=[bugs.BugLink("signal", "top.dut.fifo_full"),
               bugs.BugLink("module", "axi_fifo"),
               bugs.BugLink("ref", "picorv32.v:176")],
    )
    defaults.update(kw)
    return bugs.add_bug(con, name, **defaults)


# ---------------- B1: CRUD ----------------

def test_add_and_get_roundtrip(built):
    with db.open_db(built) as con:
        _sample(con)
        b = bugs.get_bug(con, "axi-fifo-x-prop")
    assert b is not None
    assert b.name == "axi-fifo-x-prop"          # slugified from the title-y name
    assert b.severity == "error"
    assert b.keywords == ["axi", "xprop"]
    kinds = {(l.kind, l.value) for l in b.links}
    assert ("signal", "top.dut.fifo_full") in kinds
    assert ("module", "axi_fifo") in kinds
    assert b.created_at > 0 and b.updated_at >= b.created_at


def test_name_slugified_and_validated(built):
    with db.open_db(built) as con:
        b = bugs.add_bug(con, "  Weird Name!! #2 ")
        assert b.name == "weird-name-2"
        with pytest.raises(ValueError):
            bugs.add_bug(con, "!!!")             # nothing left after slugify


def test_duplicate_requires_overwrite(built):
    with db.open_db(built) as con:
        _sample(con)
        with pytest.raises(ValueError, match="already exists"):
            _sample(con)
        # overwrite updates and preserves created_at
        before = bugs.get_bug(con, "axi-fifo-x-prop")
        b2 = _sample(con, status="fixed", overwrite=True)
        assert b2.status == "fixed"
        assert b2.created_at == before.created_at


def test_overwrite_replaces_link_set(built):
    with db.open_db(built) as con:
        _sample(con)
        bugs.add_bug(con, "axi-fifo-x-prop", overwrite=True,
                     links=[bugs.BugLink("module", "only_this")])
        b = bugs.get_bug(con, "axi-fifo-x-prop")
    assert [(l.kind, l.value) for l in b.links] == [("module", "only_this")]


def test_list_filters(built):
    with db.open_db(built) as con:
        _sample(con, name="bug-a", status="open", severity="error", tags=["rtl"])
        _sample(con, name="bug-b", status="fixed", severity="warning", tags=["tb"])
        assert {b.name for b in bugs.list_bugs(con)} == {"bug-a", "bug-b"}
        assert [b.name for b in bugs.list_bugs(con, status="fixed")] == ["bug-b"]
        assert [b.name for b in bugs.list_bugs(con, severity="error")] == ["bug-a"]
        assert [b.name for b in bugs.list_bugs(con, tag="tb")] == ["bug-b"]


def test_remove(built):
    with db.open_db(built) as con:
        _sample(con)
        assert bugs.remove_bug(con, "axi-fifo-x-prop") is True
        assert bugs.get_bug(con, "axi-fifo-x-prop") is None
        assert bugs.remove_bug(con, "axi-fifo-x-prop") is False


def test_get_missing_returns_none(built):
    with db.open_db(built) as con:
        assert bugs.get_bug(con, "nope") is None


# ---------------- B2: search ----------------

def test_search_finds_by_text(built):
    with db.open_db(built) as con:
        _sample(con)
        hits = bugs.search_bugs(con, "uninitialized")     # in root_cause
        assert [b.name for b in hits] == ["axi-fifo-x-prop"]
        hits = bugs.search_bugs(con, "readmemh")          # in fix
        assert [b.name for b in hits] == ["axi-fifo-x-prop"]


def test_search_facet_filters(built):
    with db.open_db(built) as con:
        _sample(con, name="open-bug", status="open")
        _sample(con, name="fixed-bug", status="fixed")
        hits = bugs.search_bugs(con, "fifo", status="open")
        assert [b.name for b in hits] == ["open-bug"]
        hits = bugs.search_bugs(con, "fifo", keyword="xprop")
        assert {b.name for b in hits} == {"open-bug", "fixed-bug"}
        hits = bugs.search_bugs(con, "fifo", keyword="not-a-keyword")
        assert hits == []


def test_search_like_fallback(built, monkeypatch):
    # Force the non-FTS path and confirm search still works.
    monkeypatch.setattr(db, "bug_fts_available", lambda con: False)
    with db.open_db(built) as con:
        _sample(con)
        hits = bugs.search_bugs(con, "readmemh")
    assert [b.name for b in hits] == ["axi-fifo-x-prop"]


def test_malformed_fts_query_falls_back(built):
    # A bare FTS operator would raise; search should degrade, not crash.
    with db.open_db(built) as con:
        _sample(con)
        hits = bugs.search_bugs(con, 'fifo OR "')
    assert isinstance(hits, list)


# ---------------- backend surface ----------------

def test_backend_bug_methods(built):
    be = backends.get_backend("sqlite", built)
    with be.open(read_only=False) as con:
        be.add_bug(con, "be-bug", symptom="something broke", keywords=["k1"])
    with be.open(read_only=True) as con:
        assert be.get_bug(con, "be-bug").name == "be-bug"
        assert [b.name for b in be.search_bugs(con, "broke")] == ["be-bug"]
        assert [b.name for b in be.list_bugs(con)] == ["be-bug"]


def test_seed_prompt_bug_search_present_and_runs(built):
    from xevdb import prompts
    with db.open_db(built, read_only=False) as con:
        names = {p.name for p in prompts.list_prompts(con)}
        assert {"bug_search", "bugs_by_status"} <= names
        bugs.add_bug(con, "p-bug", symptom="prompt path works", keywords=["zzz"])
        rows, _ = prompts.run_prompt(con, "bug_search", {"query": "zzz"})
    assert any(r["name"] == "p-bug" for r in rows)


# ---------------- B3: link + close ----------------

def test_link_bug(built):
    with db.open_db(built) as con:
        bugs.add_bug(con, "b1", symptom="x")
        b = bugs.link_bug(con, "b1", "signal", "top.dut.foo")
        assert ("signal", "top.dut.foo") in {(l.kind, l.value) for l in b.links}
        # dedup: linking the same thing again is a no-op
        b = bugs.link_bug(con, "b1", "signal", "top.dut.foo")
        assert sum(1 for l in b.links if l.value == "top.dut.foo") == 1
        with pytest.raises(ValueError, match="invalid link kind"):
            bugs.link_bug(con, "b1", "bogus", "v")
        with pytest.raises(ValueError, match="no bug named"):
            bugs.link_bug(con, "ghost", "signal", "v")


def test_close_bug(built):
    with db.open_db(built) as con:
        bugs.add_bug(con, "b1", symptom="x")
        b = bugs.close_bug(con, "b1", fix="patched it", fix_ref="PR#9")
        assert b.status == "fixed"
        assert b.fix == "patched it"
        assert bugs.get_bug(con, "b1").status == "fixed"
        b = bugs.close_bug(con, "b1", status="wontfix")
        assert b.status == "wontfix"
        with pytest.raises(ValueError, match="no bug named"):
            bugs.close_bug(con, "ghost")


# ---------------- B3: cross prompts ----------------

def test_bugs_for_signal_and_module_prompts(built):
    from xevdb import prompts
    with db.open_db(built, read_only=False) as con:
        bugs.add_bug(con, "sig-bug", title="signal one",
                     links=[bugs.BugLink("signal", "top.dut.fifo_full")])
        bugs.add_bug(con, "mod-bug", title="module one",
                     links=[bugs.BugLink("module", "axi_fifo")])
        # bare-name match via the LIKE '%.'||name branch
        rows, _ = prompts.run_prompt(con, "bugs_for_signal", {"signal": "fifo_full"})
        assert [r["name"] for r in rows] == ["sig-bug"]
        # fully-qualified exact match
        rows, _ = prompts.run_prompt(con, "bugs_for_signal",
                                     {"signal": "top.dut.fifo_full"})
        assert [r["name"] for r in rows] == ["sig-bug"]
        rows, _ = prompts.run_prompt(con, "bugs_for_module", {"module": "axi_fifo"})
        assert [r["name"] for r in rows] == ["mod-bug"]


def test_cross_prompts_run_cleanly(built):
    from xevdb import prompts
    with db.open_db(built, read_only=False) as con:
        bugs.add_bug(con, "xb", links=[bugs.BugLink("ref", "counter.sv:5"),
                                       bugs.BugLink("signal", "count")])
        # these run cleanly regardless of whether RTL is ingested in this fixture
        for name in ("bugs_with_rtl", "xz_signals_with_open_bugs"):
            rows, _ = prompts.run_prompt(con, name, {})
            assert isinstance(rows, list)


def test_bug_cross_prompts_carry_dsl(built):
    from xevdb import prompts
    with db.open_db(built) as con:
        for name in ("bugs_for_signal", "bugs_for_module", "bugs_by_status"):
            assert prompts.show_prompt(con, name).dsl_json  # non-empty DSL template
        # true cross-index joins stay SQL-only
        assert prompts.show_prompt(con, "bugs_with_rtl").dsl_json == ""
