"""Unit tests for the embedded query-result cache (`xevdb.cache`).

Pure SQLite logic with no external dependencies: key derivation, put/get
round-trips, hit counting, TTL expiry, the `XEVDB_NO_CACHE` toggle, and the
stats/list/clear helpers. Previously `cache.py` was only exercised indirectly
through prompt runs, leaving put/get-miss/expiry/stats/clear uncovered.
"""
from __future__ import annotations

import sqlite3

import pytest

from xevdb import cache

CACHE_DDL = """
CREATE TABLE cache (
    key         TEXT PRIMARY KEY,
    prompt_name TEXT NOT NULL,
    args_json   TEXT NOT NULL,
    result_json TEXT NOT NULL,
    created_at  REAL NOT NULL,
    hits        INTEGER NOT NULL DEFAULT 0,
    last_hit_at REAL,
    ttl_seconds INTEGER NOT NULL DEFAULT 0
)
"""


@pytest.fixture
def con():
    c = sqlite3.connect(":memory:")
    c.execute(CACHE_DDL)
    yield c
    c.close()


@pytest.fixture(autouse=True)
def _cache_on(monkeypatch):
    # Ensure a clean, enabled cache regardless of the ambient environment.
    monkeypatch.delenv("XEVDB_NO_CACHE", raising=False)


# --------------------------------------------------------------------------
# make_key
# --------------------------------------------------------------------------

def test_make_key_is_order_independent():
    k1, canon1 = cache.make_key("p", {"a": 1, "b": 2})
    k2, canon2 = cache.make_key("p", {"b": 2, "a": 1})
    assert k1 == k2
    assert canon1 == canon2 == '{"a":1,"b":2}'


def test_make_key_varies_by_prompt_and_args():
    base, _ = cache.make_key("p", {"a": 1})
    assert cache.make_key("q", {"a": 1})[0] != base       # different prompt
    assert cache.make_key("p", {"a": 2})[0] != base       # different args


# --------------------------------------------------------------------------
# put / get
# --------------------------------------------------------------------------

def test_get_miss_returns_none(con):
    assert cache.get(con, "p", {"x": 1}) is None


def test_put_then_get_roundtrips(con):
    rows = [{"n": 3}, {"n": 4}]
    cache.put(con, "p", {"x": 1}, rows)
    assert cache.get(con, "p", {"x": 1}) == rows


def test_get_increments_hit_counter(con):
    cache.put(con, "p", {"x": 1}, [{"n": 1}])
    cache.get(con, "p", {"x": 1})
    cache.get(con, "p", {"x": 1})
    hits = con.execute("SELECT hits, last_hit_at FROM cache").fetchone()
    assert hits[0] == 2
    assert hits[1] is not None


def test_put_replaces_existing_entry(con):
    cache.put(con, "p", {"x": 1}, [{"n": 1}])
    cache.put(con, "p", {"x": 1}, [{"n": 99}])
    assert con.execute("SELECT COUNT(*) FROM cache").fetchone()[0] == 1
    assert cache.get(con, "p", {"x": 1}) == [{"n": 99}]


def test_different_args_are_separate_entries(con):
    cache.put(con, "p", {"x": 1}, [{"n": 1}])
    cache.put(con, "p", {"x": 2}, [{"n": 2}])
    assert cache.get(con, "p", {"x": 1}) == [{"n": 1}]
    assert cache.get(con, "p", {"x": 2}) == [{"n": 2}]


# --------------------------------------------------------------------------
# TTL expiry
# --------------------------------------------------------------------------

def test_ttl_expired_entry_is_evicted_on_get(con, monkeypatch):
    clock = {"now": 1000.0}
    monkeypatch.setattr(cache.time, "time", lambda: clock["now"])
    cache.put(con, "p", {"x": 1}, [{"n": 1}], ttl_seconds=10)
    clock["now"] = 1005.0
    assert cache.get(con, "p", {"x": 1}) == [{"n": 1}]     # still fresh
    clock["now"] = 1020.0
    assert cache.get(con, "p", {"x": 1}) is None           # expired
    assert con.execute("SELECT COUNT(*) FROM cache").fetchone()[0] == 0


def test_ttl_zero_never_expires(con, monkeypatch):
    clock = {"now": 1000.0}
    monkeypatch.setattr(cache.time, "time", lambda: clock["now"])
    cache.put(con, "p", {"x": 1}, [{"n": 1}], ttl_seconds=0)
    clock["now"] = 10_000_000.0
    assert cache.get(con, "p", {"x": 1}) == [{"n": 1}]


# --------------------------------------------------------------------------
# XEVDB_NO_CACHE toggle
# --------------------------------------------------------------------------

def test_disabled_cache_short_circuits_put_and_get(con, monkeypatch):
    monkeypatch.setenv("XEVDB_NO_CACHE", "1")
    assert cache.enabled() is False
    cache.put(con, "p", {"x": 1}, [{"n": 1}])
    assert con.execute("SELECT COUNT(*) FROM cache").fetchone()[0] == 0
    assert cache.get(con, "p", {"x": 1}) is None


# --------------------------------------------------------------------------
# stats / list_entries / clear
# --------------------------------------------------------------------------

def test_stats_aggregates_entries_and_hits(con):
    cache.put(con, "p", {"x": 1}, [{"n": 1}])
    cache.put(con, "q", {"y": 2}, [{"n": 2}])
    cache.get(con, "p", {"x": 1})
    info = cache.stats(con)
    assert info["enabled"] is True
    assert info["entries"] == 2
    assert info["total_hits"] == 1
    assert info["result_bytes"] > 0
    assert info["by_prompt"] == {"p": 1, "q": 1}


def test_list_entries_newest_first_and_truncated_key(con, monkeypatch):
    clock = {"now": 100.0}
    monkeypatch.setattr(cache.time, "time", lambda: clock["now"])
    cache.put(con, "p", {"x": 1}, [{"n": 1}])
    clock["now"] = 200.0
    cache.put(con, "q", {"y": 2}, [{"n": 2}])
    entries = cache.list_entries(con)
    assert [e["prompt"] for e in entries] == ["q", "p"]     # newest first
    assert len(entries[0]["key"]) == 16                     # key is truncated
    assert entries[0]["args"] == {"y": 2}


def test_list_entries_filtered_by_prompt(con):
    cache.put(con, "p", {"x": 1}, [{"n": 1}])
    cache.put(con, "q", {"y": 2}, [{"n": 2}])
    entries = cache.list_entries(con, prompt="q")
    assert [e["prompt"] for e in entries] == ["q"]


def test_clear_all_and_by_prompt(con):
    cache.put(con, "p", {"x": 1}, [{"n": 1}])
    cache.put(con, "p", {"x": 2}, [{"n": 2}])
    cache.put(con, "q", {"y": 1}, [{"n": 3}])
    assert cache.clear(con, prompt="p") == 2
    assert con.execute("SELECT COUNT(*) FROM cache").fetchone()[0] == 1
    assert cache.clear(con) == 1
    assert con.execute("SELECT COUNT(*) FROM cache").fetchone()[0] == 0
