"""Kernel architecture ingest -> search on a fake OpenSearch (no live cluster)."""
from __future__ import annotations

import pytest

pytest.importorskip("opensearchpy")
from opensearchpy.exceptions import NotFoundError             # noqa: E402

from xevdb import kernel                                      # noqa: E402
from xevdb.backends import opensearch_backend as osb          # noqa: E402
from xevdb.backends import opensearch_schema as schema        # noqa: E402


def _term(doc, f, v):
    dv = doc.get(f)
    return v in dv if isinstance(dv, list) else dv == v


def _match(q, doc):
    if not q or "match_all" in q:
        return True
    if "term" in q:
        (f, v), = q["term"].items(); return _term(doc, f, v)
    if "multi_match" in q:
        needle = str(q["multi_match"]["query"]).lower()
        for f in q["multi_match"]["fields"]:
            if needle in str(doc.get(f, "")).lower():
                return True
        return False
    if "bool" in q:
        b = q["bool"]
        def _l(x): return x if isinstance(x, list) else [x]
        ok = all(_match(f, doc) for f in _l(b.get("filter", [])))
        ok = ok and all(_match(f, doc) for f in _l(b.get("must", [])))
        if b.get("should"):
            ok = ok and any(_match(f, doc) for f in _l(b["should"]))
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
            self._auto += 1; id = f"_a{self._auto}"
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
        gone = [k for k in self.store
                if k[0] == index and _match(body.get("query", {"match_all": {}}), self.store[k])]
        for k in gone:
            del self.store[k]
        return {"deleted": len(gone)}

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
        size = body.get("size", 10)
        return {"hits": {"hits": [{"_source": d} for d in rows[:size]]}}

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
    backend = osb.OpenSearchBackend(tmp_path / "kernel.ptr.json")
    counts = backend.ingest_kernel(reset=True)            # bundled data
    return backend, counts


def test_ingest_counts(be):
    backend, counts = be
    assert counts == kernel.load().counts()
    with backend.open() as c:
        s = backend.stats(c)
    for tbl in schema.KERNEL_TABLES:
        cat = tbl.removeprefix("kernel_")
        assert s["row_counts"][tbl] == counts[cat]
    assert s["kernel_version"]


def test_syscall_by_nr(be):
    backend, _ = be
    with backend.open() as c:
        rows, _ = backend.run_prompt(c, "kernel_syscall_by_nr", {"nr": 64})
    assert len(rows) == 1 and rows[0]["name"] == "write"


def test_syscall_search(be):
    backend, _ = be
    with backend.open() as c:
        rows, _ = backend.run_prompt(c, "kernel_syscall_search", {"query": "openat"})
    assert any(r["name"] == "openat" for r in rows)


def test_trap_by_code(be):
    backend, _ = be
    with backend.open() as c:
        rows, _ = backend.run_prompt(c, "kernel_trap_by_code",
                                     {"code": 13, "kind": "exception"})
    assert len(rows) == 1 and rows[0]["name"] == "EXC_LOAD_PAGE_FAULT"


def test_sbi_functions(be):
    backend, _ = be
    with backend.open() as c:
        rows, _ = backend.run_prompt(c, "kernel_sbi_functions", {"extension": "HSM"})
    names = [r["name"] for r in rows]
    assert "SBI_EXT_HSM_HART_START" in names
    assert [r["fid"] for r in rows] == sorted(r["fid"] for r in rows)   # fid-sorted


def test_sbi_search(be):
    backend, _ = be
    with backend.open() as c:
        rows, _ = backend.run_prompt(c, "kernel_sbi_search", {"query": "timer"})
    assert any("TIME" in r["extension"] for r in rows)


def test_memmap_by_mode(be):
    backend, _ = be
    with backend.open() as c:
        rows, _ = backend.run_prompt(c, "kernel_memmap_by_mode", {"mode": "Sv39"})
    assert rows and all(r["mode"] == "Sv39" for r in rows)
    assert any(r["region"] == "kernel" for r in rows)
