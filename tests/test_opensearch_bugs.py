"""OpenSearch bug KB (Phase B4) against a stateful in-memory fake client.

No live cluster: a fake OpenSearch with a dict-backed doc store + a minimal
query evaluator (term/terms/match_all/multi_match/bool) lets us round-trip the
bug surface — add/get/list/search/link/close/remove — and the Bug<->document
denormalization, exactly as the real client would.
"""
from __future__ import annotations


import pytest

opensearchpy = pytest.importorskip("opensearchpy")
from opensearchpy.exceptions import NotFoundError                    # noqa: E402

from xevdb import bugs                                                # noqa: E402
from xevdb.backends import opensearch_backend as osb                 # noqa: E402
from xevdb.backends import opensearch_schema as schema               # noqa: E402


# ---------------- fake client ----------------

def _as_list(v):
    return v if isinstance(v, list) else [v]


def _term(doc, field, val):
    dv = doc.get(field)
    return val in dv if isinstance(dv, list) else dv == val


def _match(q, doc) -> bool:
    if "match_all" in q:
        return True
    if "term" in q:
        (f, v), = q["term"].items()
        return _term(doc, f, v)
    if "terms" in q:
        (f, vs), = q["terms"].items()
        return any(_term(doc, f, v) for v in vs)
    if "multi_match" in q:
        mm = q["multi_match"]
        needle = mm["query"].lower()
        for f in mm["fields"]:
            dv = doc.get(f)
            text = " ".join(dv) if isinstance(dv, list) else str(dv or "")
            if needle in text.lower():
                return True
        return False
    if "bool" in q:
        b = q["bool"]
        ok = all(_match(f, doc) for f in _as_list(b.get("filter", [])))
        if b.get("must") is not None:
            ok = ok and all(_match(m, doc) for m in _as_list(b["must"]))
        return ok
    return True


class _FakeIndices:
    def __init__(self, os): self._os = os
    def exists(self, index): return index in self._os.indices_set
    def create(self, index, body=None): self._os.indices_set.add(index)
    def delete(self, index): self._os.indices_set.discard(index)


class _FakeOS:
    def __init__(self):
        self.store: dict[tuple[str, str], dict] = {}
        self.indices_set: set[str] = set()
        self.indices = _FakeIndices(self)

    def index(self, *, index, id, body, refresh=None):
        self.store[(index, id)] = dict(body)

    def get(self, *, index, id):
        if (index, id) not in self.store:
            raise NotFoundError(404, "not_found", {})
        return {"_source": self.store[(index, id)]}

    def delete(self, *, index, id, refresh=None):
        if (index, id) not in self.store:
            raise NotFoundError(404, "not_found", {})
        del self.store[(index, id)]

    def search(self, *, index, body):
        rows = [s for (i, _), s in self.store.items() if i == index]
        rows = [d for d in rows if _match(body.get("query", {"match_all": {}}), d)]
        if body.get("sort"):
            field = list(body["sort"][0])[0]
            desc = body["sort"][0][field] == "desc"
            rows.sort(key=lambda d: d.get(field, 0), reverse=desc)
        rows = rows[: body.get("size", 10)]
        return {"hits": {"hits": [{"_source": d} for d in rows]}}

    def close(self):
        pass


@pytest.fixture
def be(tmp_path, monkeypatch):
    fake = _FakeOS()
    monkeypatch.setattr(osb, "OpenSearch", lambda *a, **k: fake)
    ptr_path = tmp_path / "ds.xevdb"
    schema.write_pointer(ptr_path, schema.Pointer(hosts=["h:9200"], dump_id="ds"))
    return osb.OpenSearchBackend(ptr_path)


def _add(be, **kw):
    with be.open() as c:
        return be.add_bug(c, kw.pop("name", "AXI X-prop"), **kw)


# ---------------- conversion ----------------

def test_bug_doc_roundtrip_denormalizes_links():
    bug = bugs.Bug(name="b", links=[
        bugs.BugLink("signal", "top.foo"), bugs.BugLink("module", "m1"),
        bugs.BugLink("ref", "f.v:10"), bugs.BugLink("signal", "top.bar")])
    doc = osb.OpenSearchBackend._bug_to_doc(bug)
    assert set(doc["signals"]) == {"top.foo", "top.bar"}
    assert doc["modules"] == ["m1"]
    assert doc["refs"] == ["f.v:10"]
    back = osb.OpenSearchBackend._doc_to_bug(doc)
    assert {(l.kind, l.value) for l in back.links} == {(l.kind, l.value) for l in bug.links}


# ---------------- CRUD ----------------

def test_add_get_roundtrip(be):
    _add(be, severity="error", symptom="fifo_full X after reset",
         root_cause="uninitialized temp array", keywords=["axi", "xprop"],
         links=[bugs.BugLink("signal", "top.dut.fifo_full"),
                bugs.BugLink("module", "axi_fifo")])
    with be.open() as c:
        b = be.get_bug(c, "axi-x-prop")
    assert b is not None and b.name == "axi-x-prop"
    assert b.severity == "error"
    assert ("signal", "top.dut.fifo_full") in {(l.kind, l.value) for l in b.links}


def test_duplicate_requires_overwrite(be):
    _add(be)
    with be.open() as c:
        with pytest.raises(ValueError, match="already exists"):
            be.add_bug(c, "AXI X-prop")
        created = be.get_bug(c, "axi-x-prop").created_at
        b = be.add_bug(c, "AXI X-prop", status="fixed", overwrite=True)
    assert b.status == "fixed" and b.created_at == created


def test_list_and_facets(be):
    _add(be, name="open-bug", status="open", severity="error", tags=["rtl"])
    _add(be, name="fixed-bug", status="fixed", severity="warning", tags=["tb"])
    with be.open() as c:
        assert {b.name for b in be.list_bugs(c)} == {"open-bug", "fixed-bug"}
        assert [b.name for b in be.list_bugs(c, status="fixed")] == ["fixed-bug"]
        assert [b.name for b in be.list_bugs(c, tag="rtl")] == ["open-bug"]


def test_search_multimatch_and_filters(be):
    _add(be, name="open-bug", status="open", symptom="fifo_full uninitialized",
         keywords=["axi"])
    _add(be, name="fixed-bug", status="fixed", symptom="fifo_full unrelated",
         keywords=["axi"])
    with be.open() as c:
        assert {b.name for b in be.search_bugs(c, "uninitialized")} == {"open-bug"}
        assert {b.name for b in be.search_bugs(c, "fifo_full")} == {"open-bug", "fixed-bug"}
        assert [b.name for b in be.search_bugs(c, "fifo_full", status="open")] == ["open-bug"]
        assert be.search_bugs(c, "fifo_full", keyword="nope") == []


def test_link_close_remove(be):
    _add(be, name="b1")
    with be.open() as c:
        b = be.link_bug(c, "b1", "signal", "top.foo")
        assert ("signal", "top.foo") in {(l.kind, l.value) for l in b.links}
        be.link_bug(c, "b1", "signal", "top.foo")        # dedup
        b = be.get_bug(c, "b1")
        assert sum(1 for l in b.links if l.value == "top.foo") == 1
        assert be.close_bug(c, "b1", fix="done").status == "fixed"
        assert be.remove_bug(c, "b1") is True
        assert be.get_bug(c, "b1") is None
        assert be.remove_bug(c, "b1") is False
        with pytest.raises(ValueError, match="invalid link kind"):
            be.link_bug(c, "b1", "bogus", "v")
