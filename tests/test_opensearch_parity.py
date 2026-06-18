"""OpenSearch parity: the prompts newly given a dsl_json actually run.

Builds the VCD into a fake cluster (seeding changes + the prompt library),
injects a few RTL/sim docs directly, and runs each ported prompt — verifying
they return rows instead of raising NotImplementedError.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

opensearchpy = pytest.importorskip("opensearchpy")
from opensearchpy.exceptions import NotFoundError                    # noqa: E402

from xevdb.backends import opensearch_backend as osb                 # noqa: E402

REPO = Path(__file__).resolve().parents[1]
VCD = REPO / "examples" / "simple" / "counter.vcd"


# ---- query evaluator (term/terms/range/bool/match_all + aggs terms + sort) ----

def _term(doc, f, v):
    dv = doc.get(f)
    return v in dv if isinstance(dv, list) else dv == v


def _match(q, doc):
    if not q or "match_all" in q:
        return True
    if "term" in q:
        (f, v), = q["term"].items(); return _term(doc, f, v)
    if "terms" in q:
        (f, vs), = q["terms"].items(); return any(_term(doc, f, v) for v in vs)
    if "range" in q:
        (f, c), = q["range"].items(); x = doc.get(f)
        if x is None:
            return False
        if "lte" in c and x > c["lte"]:
            return False
        if "gte" in c and x < c["gte"]:
            return False
        return True
    if "bool" in q:
        b = q["bool"]
        def _l(x): return x if isinstance(x, list) else [x]
        return all(_match(f, doc) for f in _l(b.get("filter", []))) \
            and all(_match(f, doc) for f in _l(b.get("must", [])))
    return True


class _Idx:
    def __init__(self, os): self._os = os
    def exists(self, index): return index in self._os.idx
    def create(self, index, body=None): self._os.idx.add(index)
    def delete(self, index):
        self._os.idx.discard(index)
        for k in [k for k in self._os.store if k[0] == index]:
            del self._os.store[k]


class FakeOS:
    def __init__(self):
        self.store: dict[tuple, dict] = {}
        self.idx: set[str] = set()
        self.indices = _Idx(self)
        self._n = 0

    def index(self, *, index, id=None, body, refresh=None):
        if id is None:
            self._n += 1; id = f"_a{self._n}"
        self.store[(index, id)] = dict(body)

    def get(self, *, index, id):
        if (index, id) not in self.store:
            raise NotFoundError(404, "nf", {})
        return {"_source": self.store[(index, id)]}

    def count(self, *, index):
        return {"count": sum(1 for k in self.store if k[0] == index)}

    def search(self, *, index, body):
        rows = [s for (i, _), s in self.store.items() if i == index]
        rows = [d for d in rows if _match(body.get("query", {"match_all": {}}), d)]
        if body.get("sort"):
            for key in reversed(body["sort"]):
                (f, order), = key.items()
                rows.sort(key=lambda d: (d.get(f) is None, d.get(f)),
                          reverse=(order == "desc"))
        out = {"hits": {"hits": [{"_source": d} for d in rows[:body.get("size", 10)]]}}
        if body.get("aggs"):
            out["aggregations"] = {}
            for name, spec in body["aggs"].items():
                field = spec["terms"]["field"]
                counts: dict = {}
                for d in rows:
                    v = d.get(field)
                    if v is not None:
                        counts[v] = counts.get(v, 0) + 1
                buckets = sorted(counts.items(), key=lambda kv: -kv[1])
                out["aggregations"][name] = {
                    "buckets": [{"key": k, "doc_count": c}
                                for k, c in buckets[: spec["terms"].get("size", 10)]]}
        return out

    def update(self, **k): pass
    def delete(self, **k): pass
    def delete_by_query(self, *, index, body, refresh=None): return {"deleted": 0}
    def close(self): pass


@pytest.fixture
def be(tmp_path, monkeypatch):
    fake = FakeOS()
    monkeypatch.setattr(osb, "OpenSearch", lambda *a, **k: fake)

    def fake_bulk(client, ops, **kw):
        ops = list(ops)
        for op in ops:
            client.index(index=op["_index"], id=op.get("_id"), body=op["_source"])
        return len(ops), []

    monkeypatch.setattr(osb.helpers, "bulk", fake_bulk)
    monkeypatch.setenv("XEVDB_OPENSEARCH_HOSTS", "localhost:9200")
    backend = osb.OpenSearchBackend(tmp_path / "ds.xevdb")
    backend.build(VCD, reset=True)                  # changes + meta + prompts

    # inject a couple of RTL + sim docs directly (no sv-parse needed)
    ptr = backend._pointer()
    fake.index(index=ptr.index("module_ports"), id="p0", body={
        "module_id": "m", "position": 0, "name": "clk", "direction": "input",
        "width": "1", "kind": "wire", "module_name": "counter"})
    fake.index(index=ptr.index("module_ports"), id="p1", body={
        "module_id": "m", "position": 1, "name": "count", "direction": "output",
        "width": "8", "kind": "reg", "module_name": "counter"})
    fake.index(index=ptr.index("module_signals"), id="s0", body={
        "module_id": "m", "name": "next", "kind": "wire", "line": 7,
        "width": "8", "decl_text": "wire [7:0] next;", "module_name": "counter"})
    fake.index(index=ptr.index("sim_runs"), id="r0", body={
        "id": "run::1", "name": "run", "source": "run.log", "line_count": 10,
        "n_events": 3, "n_fatal": 0, "n_error": 1, "n_warning": 1})
    for i, (sev, t) in enumerate([("UVM_ERROR", 200), ("UVM_WARNING", 100),
                                  ("UVM_INFO", 50)]):
        fake.index(index=ptr.index("sim_events"), id=f"e{i}", body={
            "run_id": "run::1", "line_no": i, "severity": sev, "t": t,
            "ref_file": "counter.sv", "ref_line": 14, "message": f"msg {sev}"})
    return backend


def _run(be, name, args=None):
    with be.open() as c:
        rows, _ = be.run_prompt(c, name, args or {})
    return rows


def test_signal_transitions(be):
    rows = _run(be, "signal_transitions", {"t0": 0, "t1": 40, "limit": 10})
    assert rows and set(rows[0]) == {"key", "count"}      # aggs buckets


def test_signal_history(be):
    rows = _run(be, "signal_history", {"signal": "top.u_cnt.count"})
    assert rows and all("value" in r and "t" in r for r in rows)
    assert [r["t"] for r in rows] == sorted(r["t"] for r in rows)


def test_ports_of_module(be):
    rows = _run(be, "ports_of_module", {"module": "counter"})
    assert [r["name"] for r in rows] == ["clk", "count"]   # position-sorted


def test_signals_of_module(be):
    rows = _run(be, "signals_of_module", {"module": "counter"})
    assert any(r["name"] == "next" for r in rows)


def test_sim_summary(be):
    rows = _run(be, "sim_summary")
    assert rows and rows[0]["n_error"] == 1


def test_sim_errors(be):
    rows = _run(be, "sim_errors")
    sevs = {r["severity"] for r in rows}
    assert "UVM_ERROR" in sevs and "UVM_WARNING" not in sevs    # only error-class


def test_sim_around_time(be):
    rows = _run(be, "sim_around_time", {"t0": 0, "t1": 120})
    assert {r["t"] for r in rows} == {50, 100}                  # 200 excluded


def test_cross_join_prompt_still_sql_only(be):
    # the genuine cross-index joins stay sql-only and report it clearly
    with be.open() as c:
        with pytest.raises(NotImplementedError, match="SQL-only"):
            be.run_prompt(c, "sim_with_rtl", {})
