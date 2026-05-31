"""OpenSearch backend orchestration (Phase 3) against a fake client.

No live cluster: a fake OpenSearch + fake helpers.bulk record what the backend
would create/delete/index, so we can assert the write path (pointer creation,
index lifecycle, bulk payloads, --reset granularity, counts) end to end.
"""
from __future__ import annotations

from pathlib import Path

import pytest

opensearchpy = pytest.importorskip("opensearchpy")

from xevdb import seed_prompts, sv                                    # noqa: E402
from xevdb.backends import opensearch_backend as osb                 # noqa: E402
from xevdb.backends import opensearch_schema as schema               # noqa: E402


REPO = Path(__file__).resolve().parents[1]
VCD = REPO / "examples" / "counter.vcd"
SV = REPO / "examples" / "counter.sv"
SIM_LOG = REPO / "examples" / "sim.log"


class _Store:
    def __init__(self) -> None:
        self.existing: set[str] = set()
        self.created: list[str] = []
        self.deleted: list[str] = []
        self.ops: list[dict] = []


class _FakeIndices:
    def __init__(self, store: _Store) -> None:
        self._s = store

    def exists(self, index: str) -> bool:
        return index in self._s.existing

    def create(self, index: str, body: dict) -> None:
        self._s.existing.add(index)
        self._s.created.append(index)

    def delete(self, index: str) -> None:
        self._s.existing.discard(index)
        self._s.deleted.append(index)


class _FakeClient:
    def __init__(self, store: _Store) -> None:
        self.indices = _FakeIndices(store)

    def close(self) -> None:
        pass


@pytest.fixture
def store(monkeypatch):
    s = _Store()
    monkeypatch.setattr(osb, "OpenSearch", lambda *a, **k: _FakeClient(s))

    def fake_bulk(client, ops, **kw):
        materialized = list(ops)
        s.ops.extend(materialized)
        return len(materialized), []

    monkeypatch.setattr(osb.helpers, "bulk", fake_bulk)
    monkeypatch.setenv("XEVDB_OPENSEARCH_HOSTS", "localhost:9200")
    return s


def _ops_by_index_suffix(store: _Store) -> dict[str, int]:
    out: dict[str, int] = {}
    for op in store.ops:
        suffix = op["_index"].rsplit("-", 1)[-1]
        out[suffix] = out.get(suffix, 0) + 1
    return out


def test_build_writes_pointer_and_bulk(tmp_path, store):
    ptr_path = tmp_path / "counter.xevdb"
    be = osb.OpenSearchBackend(ptr_path)
    result = be.build(VCD, reset=True)

    # pointer materialized from env + filename
    assert ptr_path.is_file()
    ptr = schema.read_pointer(ptr_path)
    assert ptr.dump_id == "counter"
    assert ptr.hosts == ["localhost:9200"]

    assert result == {"signals": 4, "changes": 16, "t_min": 0, "t_max": 15} \
        or (result["signals"] == 4 and result["changes"] == 16)

    counts = _ops_by_index_suffix(store)
    assert counts["signals"] == 4
    assert counts["changes"] == 16
    assert counts["meta"] == 9
    assert counts["prompts"] == len(seed_prompts.PROMPTS)


def test_build_reset_wipes_all_indices(tmp_path, store):
    be = osb.OpenSearchBackend(tmp_path / "c.xevdb")
    be.build(VCD, reset=True)
    # reset=True recreates the whole table set
    assert set(store.created) == set(schema.all_index_names(
        schema.DEFAULT_PREFIX, "c").values())


def test_build_no_reset_only_touches_waveform(tmp_path, store):
    be = osb.OpenSearchBackend(tmp_path / "c.xevdb")
    be.build(VCD, reset=True)        # establish everything
    store.created.clear()
    store.deleted.clear()
    be.build(VCD, reset=False)       # re-build waveform only
    touched = {n.rsplit("-", 1)[-1] for n in store.deleted}
    assert touched == {"signals", "changes", "meta"}


def test_ingest_sim_counts_and_run_link(tmp_path, store):
    be = osb.OpenSearchBackend(tmp_path / "c.xevdb")
    be.build(VCD, reset=True)
    store.ops.clear()
    result = be.ingest_sim(SIM_LOG, reset=True)
    assert result["events"] == 14
    assert result["fatal"] == 1
    counts = _ops_by_index_suffix(store)
    assert counts["sim_runs"] == 1
    assert counts["sim_events"] == 14


@pytest.mark.skipif(not sv.have_sv_parse(), reason="sv-parse binary not built")
def test_ingest_rtl_counts(tmp_path, store):
    be = osb.OpenSearchBackend(tmp_path / "c.xevdb")
    be.build(VCD, reset=True)
    store.ops.clear()
    result = be.ingest_rtl(SV, reset=True)
    assert result["files"] == 1
    assert result["modules"] >= 2
    counts = _ops_by_index_suffix(store)
    assert counts["source_files"] == 1
    assert counts["modules"] >= 2


def test_missing_pointer_for_ingest_is_clear(tmp_path, store):
    be = osb.OpenSearchBackend(tmp_path / "nope.xevdb")
    with pytest.raises(FileNotFoundError, match="pointer file"):
        be.ingest_sim(SIM_LOG)
