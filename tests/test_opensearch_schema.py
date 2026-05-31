"""OpenSearch index design (Phase 2) — dependency-free schema + routing.

These tests exercise the pure schema/naming/pointer logic and the registry's
behaviour when the optional `opensearch-py` package is absent. They do NOT
require a running cluster.
"""
from __future__ import annotations

import pytest

from xevdb import backends
from xevdb.backends import opensearch_schema as schema


def test_every_table_has_a_mapping():
    for table in schema.TABLES:
        body = schema.mapping_for(table)
        assert body["mappings"]["properties"], table
        assert body["settings"]["index"]["number_of_shards"] == 1


def test_index_names_are_lowercase_and_namespaced():
    idx = schema.index_name("xevdb", "PicoRV32", "signals")
    assert idx == "xevdb-picorv32-signals"
    names = schema.all_index_names("xevdb", "picorv32")
    assert set(names) == set(schema.TABLES)
    assert all(v.islower() for v in names.values())


def test_unknown_table_rejected():
    with pytest.raises(KeyError):
        schema.index_name("xevdb", "d", "not_a_table")


def test_slugify():
    assert schema.slugify("My Dump #1") == "my-dump-1"
    assert schema.slugify("///") == "dump"


def test_changes_index_is_denormalized():
    # Strategy B: changes must carry signal fields + precomputed xz so the
    # cross-side prompts need no join.
    props = schema.mapping_for("changes")["mappings"]["properties"]
    for f in ("fullname", "width", "kind", "xz"):
        assert f in props
    assert props["xz"]["type"] == "boolean"


def test_pointer_roundtrip(tmp_path):
    ptr = schema.Pointer(hosts=["localhost:9200"], dump_id="picorv32")
    p = tmp_path / "picorv32.xevdb"
    schema.write_pointer(p, ptr)
    assert schema.looks_like_pointer(p)
    back = schema.read_pointer(p)
    assert back.hosts == ["localhost:9200"]
    assert back.dump_id == "picorv32"
    assert back.index("signals") == "xevdb-picorv32-signals"


def test_non_pointer_files_not_misrouted(tmp_path):
    sqlite_like = tmp_path / "x.xevdb"
    sqlite_like.write_bytes(b"SQLite format 3\x00rest-of-binary")
    assert not schema.looks_like_pointer(sqlite_like)
    assert not schema.looks_like_pointer(tmp_path / "does-not-exist")


def test_registry_routes_pointer_to_opensearch(tmp_path):
    """A pointer path selects opensearch; absent the dep, the error names the
    install flag rather than crashing."""
    p = tmp_path / "ptr.xevdb"
    schema.write_pointer(p, schema.Pointer(hosts=["h:9200"], dump_id="d"))
    try:
        import opensearchpy  # noqa: F401
    except ImportError:
        with pytest.raises(ValueError, match="opensearch"):
            backends.get_backend(None, p)
    else:
        be = backends.get_backend(None, p)
        assert be.name == "opensearch"
