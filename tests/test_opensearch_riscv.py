"""Phase 6: drive RISC-V reference ingest -> search on a fake OpenSearch.

Builds the standalone riscv reference dataset into an in-memory fake cluster
(ingest_riscv), then runs every riscv_* seed prompt and checks the results —
no live cluster. The fake's query evaluator mirrors test_opensearch_read.py plus
single-field ``match`` (used by riscv_reg_lookup).
"""
from __future__ import annotations


import pytest

pytest.importorskip("opensearchpy")
from opensearchpy.exceptions import NotFoundError             # noqa: E402

from xevdb import riscv                                       # noqa: E402
from xevdb.backends import opensearch_backend as osb          # noqa: E402
from xevdb.backends import opensearch_schema as schema        # noqa: E402


# ---------------- query evaluator (subset + single-field match) ----------------

def _term(doc, field, val) -> bool:
    dv = doc.get(field)
    return val in dv if isinstance(dv, list) else dv == val


def _match(q, doc) -> bool:
    if not q or "match_all" in q:
        return True
    if "term" in q:
        (f, v), = q["term"].items(); return _term(doc, f, v)
    if "match" in q:
        (f, v), = q["match"].items()
        return str(v).lower() in str(doc.get(f, "")).lower()
    if "multi_match" in q:
        needle = q["multi_match"]["query"].lower()
        for f in q["multi_match"]["fields"]:
            dv = doc.get(f)
            text = " ".join(map(str, dv)) if isinstance(dv, list) else str(dv or "")
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

    def index(self, *, index, id=None, body, refresh=None):
        if id is None:
            self._auto += 1
            id = f"_auto{self._auto}"
        self.store[(index, id)] = dict(body)

    def get(self, *, index, id):
        if (index, id) not in self.store:
            raise NotFoundError(404, "nf", {})
        return {"_source": self.store[(index, id)]}

    def update(self, *, index, id, body, refresh=None):
        self.store[(index, id)].update(body["doc"])

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
                field = spec["terms"]["field"]
                counts: dict = {}
                for d in rows:
                    v = d.get(field)
                    if v is not None:
                        counts[v] = counts.get(v, 0) + 1
                buckets = sorted(counts.items(), key=lambda kv: -kv[1])
                buckets = buckets[: spec["terms"].get("size", 10)]
                out["aggregations"][name] = {
                    "buckets": [{"key": k, "doc_count": c} for k, c in buckets]}
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
    backend = osb.OpenSearchBackend(tmp_path / "riscv.ptr.json")
    counts = backend.ingest_riscv(reset=True)
    return backend, counts


def _names(rows):
    return {r["name"] for r in rows}


# ---------------- ingest / stats ----------------

def test_ingest_counts_and_stats(be):
    backend, counts = be
    data = riscv.load()
    assert counts == data.counts()
    with backend.open() as c:
        s = backend.stats(c)
    for tbl in schema.RISCV_TABLES:
        cat = tbl.removeprefix("riscv_")
        assert s["row_counts"][tbl] == counts[cat]
    assert s["riscv_spec_version"] == data.spec_version


# ---------------- instruction search ----------------

def test_instr_search(be):
    backend, _ = be
    with backend.open() as c:
        rows, _ = backend.run_prompt(c, "riscv_instr_search", {"query": "jal"})
    assert {"jal", "jalr"} <= _names(rows)


def test_instr_by_name_has_encoding(be):
    backend, _ = be
    with backend.open() as c:
        rows, _ = backend.run_prompt(c, "riscv_instr_by_name", {"name": "jalr"})
    assert len(rows) == 1
    r = rows[0]
    assert r["name"] == "jalr" and r["mask"].startswith("0x") and r["match"].startswith("0x")


def test_by_extension(be):
    backend, _ = be
    with backend.open() as c:
        rows, _ = backend.run_prompt(c, "riscv_by_extension", {"extension": "M"})
    assert {"mul", "div", "rem"} <= _names(rows)


# ---------------- CSR / register / pseudo ----------------

def test_csr_by_addr_decode(be):
    backend, _ = be
    with backend.open() as c:
        rows, _ = backend.run_prompt(c, "riscv_csr_by_addr", {"addr": "0x305"})
    assert len(rows) == 1 and rows[0]["name"] == "mtvec"


def test_csr_lookup_by_text(be):
    backend, _ = be
    with backend.open() as c:
        rows, _ = backend.run_prompt(c, "riscv_csr_lookup", {"query": "trap"})
    assert "mtvec" in _names(rows)


def test_reg_lookup_abi(be):
    backend, _ = be
    with backend.open() as c:
        rows, _ = backend.run_prompt(c, "riscv_reg_lookup", {"query": "a0"})
    assert any(r["name"] == "x10" and r["abi"] == "a0" for r in rows)


def test_pseudo_search(be):
    backend, _ = be
    with backend.open() as c:
        rows, _ = backend.run_prompt(c, "riscv_pseudo_search", {"query": "ret"})
    ret = next(r for r in rows if r["name"] == "ret")
    assert ret["expansion"] == "jalr x0, 0(ra)"


def test_ext_overview_aggs(be):
    backend, _ = be
    with backend.open() as c:
        rows, _ = backend.run_prompt(c, "riscv_ext_overview", {"limit": 50})
    assert rows and all(set(r) == {"key", "count"} for r in rows)
    by_ext = {r["key"]: r["count"] for r in rows}
    assert by_ext.get("M", 0) >= 8       # mul/mulh/.../remu(+w)
