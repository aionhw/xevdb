"""OpenSearch read path (Phase 4) + prompt/cache CRUD (Phase 5).

A richer in-memory fake OpenSearch (query + aggregation + count + update +
delete_by_query) lets us drive a real build -> read pipeline: build a dataset
into the fake, then resolve/value_at/window/find, run dsl_json prompts (hits
and aggs modes), exercise the cache, and CRUD the prompt library — all with no
live cluster.
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


# ---------------- query evaluator ----------------

def _wild(value, pattern) -> bool:
    rx = "".join("." if c == "?" else ".*" if c == "*" else re.escape(c) for c in pattern)
    return re.fullmatch(rx, str(value)) is not None


def _term(doc, field, val) -> bool:
    dv = doc.get(field)
    return val in dv if isinstance(dv, list) else dv == val


def _match(q, doc) -> bool:
    if not q or "match_all" in q:
        return True
    if "term" in q:
        (f, v), = q["term"].items(); return _term(doc, f, v)
    if "terms" in q:
        (f, vs), = q["terms"].items(); return any(_term(doc, f, v) for v in vs)
    if "prefix" in q:
        (f, v), = q["prefix"].items(); return str(doc.get(f, "")).startswith(v)
    if "wildcard" in q:
        (f, v), = q["wildcard"].items()
        dv = doc.get(f)
        vals = dv if isinstance(dv, list) else [dv]
        return any(_wild(x, v) for x in vals if x is not None)
    if "range" in q:
        (f, conds), = q["range"].items()
        x = doc.get(f)
        if x is None:
            return False
        if "lte" in conds and x > conds["lte"]:
            return False
        if "gte" in conds and x < conds["gte"]:
            return False
        return True
    if "multi_match" in q:
        needle = q["multi_match"]["query"].lower()
        for f in q["multi_match"]["fields"]:
            dv = doc.get(f)
            text = " ".join(dv) if isinstance(dv, list) else str(dv or "")
            if needle in text.lower():
                return True
        return False
    if "bool" in q:
        b = q["bool"]
        def _lst(x): return x if isinstance(x, list) else [x]
        ok = all(_match(f, doc) for f in _lst(b.get("filter", [])))
        ok = ok and all(_match(f, doc) for f in _lst(b.get("must", [])))
        if b.get("should"):
            ok = ok and any(_match(f, doc) for f in _lst(b["should"]))
        return ok
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
        self._auto = 0

    # writes
    def index(self, *, index, id=None, body, refresh=None):
        if id is None:
            self._auto += 1
            id = f"_auto{self._auto}"
        self.store[(index, id)] = dict(body)

    def update(self, *, index, id, body, refresh=None):
        self.store[(index, id)].update(body["doc"])

    def get(self, *, index, id):
        if (index, id) not in self.store:
            raise NotFoundError(404, "nf", {})
        return {"_source": self.store[(index, id)]}

    def delete(self, *, index, id, refresh=None):
        if (index, id) not in self.store:
            raise NotFoundError(404, "nf", {})
        del self.store[(index, id)]

    def delete_by_query(self, *, index, body, refresh=None):
        q = body.get("query", {"match_all": {}})
        gone = [k for k in self.store if k[0] == index and _match(q, self.store[k])]
        for k in gone:
            del self.store[k]
        return {"deleted": len(gone)}

    def count(self, *, index):
        return {"count": sum(1 for k in self.store if k[0] == index)}

    # reads
    def search(self, *, index, body):
        rows = [s for (i, _), s in self.store.items() if i == index]
        rows = [d for d in rows if _match(body.get("query", {"match_all": {}}), d)]
        out: dict = {"hits": {"total": {"value": len(rows)}, "hits": []}}
        if body.get("sort"):
            for key in reversed(body["sort"]):
                (f, order), = key.items()
                rows.sort(key=lambda d: (d.get(f) is None, d.get(f)),
                          reverse=(order == "desc"))
        size = body.get("size", 10)
        out["hits"]["hits"] = [{"_source": d} for d in rows[:size]]
        if body.get("aggs"):
            out["aggregations"] = {}
            for name, spec in body["aggs"].items():
                if "terms" in spec:
                    field = spec["terms"]["field"]
                    counts: dict = {}
                    for d in rows:
                        dv = d.get(field)
                        for v in (dv if isinstance(dv, list) else [dv]):
                            if v is not None:
                                counts[v] = counts.get(v, 0) + 1
                    buckets = sorted(counts.items(), key=lambda kv: -kv[1])
                    buckets = buckets[: spec["terms"].get("size", 10)]
                    out["aggregations"][name] = {
                        "buckets": [{"key": k, "doc_count": c} for k, c in buckets]}
                elif "sum" in spec:
                    field = spec["sum"]["field"]
                    out["aggregations"][name] = {
                        "value": sum(d.get(field, 0) or 0 for d in rows)}
        return out

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
    backend.build(VCD, reset=True)            # seeds signals/changes/meta/prompts
    return backend


# ---------------- stats ----------------

def test_stats(be):
    with be.open() as c:
        s = be.stats(c)
    assert s["n_signals"] == "4"
    assert s["n_changes"] == "16"
    assert s["row_counts"]["signals"] == 4
    assert s["row_counts"]["changes"] == 16


# ---------------- waveform queries ----------------

def test_resolve_and_value_at(be):
    with be.open() as c:
        sig = be.resolve_signal(c, "count")
        assert sig is not None
        last_t, val = be.value_at(c, sig.sig_id, 20)
    assert last_t == 15
    assert val == "00000001"


def test_window_and_find(be):
    with be.open() as c:
        sig = be.resolve_signal(c, "count")
        rows = be.window(c, sig.sig_id, 0, 30, limit=100)
        assert rows and all(0 <= t <= 30 for t, _ in rows)
        assert rows == sorted(rows)                 # ascending by t
        hits = be.find_signals(c, "count")
        assert any("count" in h.fullname for h in hits)


# ---------------- run_prompt: dsl engine ----------------

def test_run_prompt_hits_mode(be):
    with be.open() as c:
        rows, hit = be.run_prompt(c, "signals_in_scope", {"prefix": ""})
    assert hit is False
    assert len(rows) == 4
    assert all("fullname" in r for r in rows)


def test_run_prompt_aggs_mode(be):
    with be.open() as c:
        rows, _ = be.run_prompt(c, "change_count", {"limit": 10})
    # aggregation buckets -> {key, count}
    assert rows and all(set(r) == {"key", "count"} for r in rows)
    assert sum(r["count"] for r in rows) == 16


def test_run_prompt_cache(be):
    with be.open() as c:
        r1, hit1 = be.run_prompt(c, "signals_in_scope", {"prefix": ""})
        r2, hit2 = be.run_prompt(c, "signals_in_scope", {"prefix": ""})
    assert hit1 is False and hit2 is True
    assert r1 == r2


def test_run_prompt_sql_only_raises(be):
    with be.open() as c:
        with pytest.raises(NotImplementedError, match="SQL-only"):
            be.run_prompt(c, "stuck_at", {})        # stuck_at has no dsl_json


# ---------------- prompt CRUD + cache CRUD ----------------

def test_prompt_crud(be):
    with be.open() as c:
        names = {p.name for p in be.list_prompts(c)}
        assert {"signals_in_scope", "bug_search"} <= names
        be.add_prompt(c, "mine", "SELECT 1", description="d",
                      dsl_json='{"index":"meta","body":{"query":{"match_all":{}}}}')
        assert be.show_prompt(c, "mine").dsl_json
        assert be.remove_prompt(c, "mine") is True
        with pytest.raises(KeyError):
            be.show_prompt(c, "mine")


def test_cache_crud(be):
    with be.open() as c:
        be.run_prompt(c, "signals_in_scope", {"prefix": ""})
        stats = be.cache_stats(c)
        assert stats["entries"] >= 1
        listed = be.cache_list(c)
        assert listed and listed[0]["prompt"] == "signals_in_scope"
        assert be.cache_clear(c) >= 1
        assert be.cache_stats(c)["entries"] == 0
