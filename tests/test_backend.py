"""Backend abstraction (Phase 1) + dual-representation prompts (Phase 0 B)."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from xevdb import db, prompts, backends


REPO = Path(__file__).resolve().parents[1]
VCD = REPO / "examples" / "counter.vcd"


@pytest.fixture
def built(tmp_path):
    out = tmp_path / "counter.xevdb"
    db.build(VCD, out, reset=True)
    return out


# ---------------- backend registry ----------------

def test_default_backend_is_sqlite(built):
    be = backends.get_backend(None, built)
    assert be.name == "sqlite"
    assert be.supports_raw_sql is True


def test_unknown_backend_raises(built):
    with pytest.raises(ValueError):
        backends.get_backend("nope", built)


def test_backend_roundtrip(built):
    be = backends.get_backend("sqlite", built)
    with be.open(read_only=False) as con:
        info = be.stats(con)
        rows, hit = be.run_prompt(con, "stuck_at", {"limit": 5})
    assert int(info["n_signals"]) == 4
    assert hit is False
    assert isinstance(rows, list)


# ---------------- dual-representation prompts ----------------

def test_dsl_json_roundtrips(built):
    body = '{"query":{"match_all":{}}}'
    with db.open_db(built) as con:
        prompts.add_prompt(con, "p_dsl", "SELECT 1 AS one", dsl_json=body)
        p = prompts.show_prompt(con, "p_dsl")
    assert p.dsl_json == body
    assert p.dsl == {"query": {"match_all": {}}}


def test_sql_only_prompt_has_no_dsl(built):
    with db.open_db(built) as con:
        p = prompts.show_prompt(con, "stuck_at")
    assert p.dsl_json == ""
    assert p.dsl is None


def test_migration_adds_dsl_json_to_legacy_db(tmp_path):
    """A prompts table created before the dsl_json column gains it on open."""
    legacy = tmp_path / "legacy.xevdb"
    con = sqlite3.connect(legacy)
    con.execute(
        "CREATE TABLE prompts (name TEXT PRIMARY KEY, description TEXT, "
        "sql TEXT, params_json TEXT, created_at REAL, updated_at REAL)"
    )
    con.execute(
        "INSERT INTO prompts VALUES ('old', '', 'SELECT 1', '[]', 0, 0)"
    )
    con.commit()
    con.close()

    cols = lambda c: {r[1] for r in c.execute("PRAGMA table_info(prompts)")}
    with db.open_db(legacy) as con:
        db._ensure_schema(con)
        assert "dsl_json" in cols(con)
        p = prompts.show_prompt(con, "old")
    assert p.dsl_json == ""
