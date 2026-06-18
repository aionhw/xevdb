"""Phase 3: riscv_actions builds correctly-shaped, idempotent documents."""
from __future__ import annotations

from xevdb import riscv
from xevdb.backends import opensearch_docs as docs
from xevdb.backends import opensearch_schema as schema


def test_riscv_actions_cover_all_tables():
    data = riscv.load()
    actions = list(docs.riscv_actions(data, now=123.0))
    by_table: dict[str, list] = {}
    for a in actions:
        by_table.setdefault(a.table, []).append(a)
    assert set(by_table) == set(schema.RISCV_TABLES)
    counts = data.counts()
    assert len(by_table["riscv_instructions"]) == counts["instructions"]
    assert len(by_table["riscv_registers"]) == counts["registers"]
    assert len(by_table["riscv_csrs"]) == counts["csrs"]


def test_riscv_actions_ids_and_provenance():
    data = riscv.load()
    actions = list(docs.riscv_actions(data, now=99.0))
    for a in actions:
        assert a.id == a.source["id"]            # keyed by stable id (idempotent)
        assert a.source["ingested_at"] == 99.0
    # ids are unique across every doc within a table
    for table in schema.RISCV_TABLES:
        ids = [a.id for a in actions if a.table == table]
        assert len(ids) == len(set(ids))


def test_riscv_actions_sources_match_mapping_fields():
    """Every doc field is declared in the table's mapping (no stray fields)."""
    data = riscv.load()
    for a in docs.riscv_actions(data, now=1.0):
        allowed = set(schema._PROPS[a.table])
        assert set(a.source) <= allowed, (
            f"{a.table}: unexpected fields {set(a.source) - allowed}")
