"""Phase 7 — live OpenSearch integration test.

Skipped unless XEVDB_OPENSEARCH_TEST_HOST points at a reachable cluster
(e.g. `localhost:9200` from a `docker run opensearchproject/opensearch`),
so the default test run needs no cluster. When configured it drives the real
client end to end — build, stats, a dsl_json prompt, and the bug KB — against
a throwaway dump id, cleaning up its indices afterwards.

    XEVDB_OPENSEARCH_TEST_HOST=localhost:9200 pytest tests/test_opensearch_integration.py
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

HOST = os.environ.get("XEVDB_OPENSEARCH_TEST_HOST")
pytestmark = pytest.mark.skipif(
    not HOST, reason="set XEVDB_OPENSEARCH_TEST_HOST to run live OpenSearch tests")

pytest.importorskip("opensearchpy")

REPO = Path(__file__).resolve().parents[1]
VCD = REPO / "examples" / "counter.vcd"


def test_live_build_query_and_bug_roundtrip(tmp_path, monkeypatch):
    from xevdb.backends.opensearch_backend import OpenSearchBackend
    from xevdb import bugs

    monkeypatch.setenv("XEVDB_OPENSEARCH_HOSTS", HOST)
    monkeypatch.setenv("XEVDB_OPENSEARCH_DUMP_ID", "xevdb-itest")
    be = OpenSearchBackend(tmp_path / "it.xevdb")
    try:
        result = be.build(VCD, reset=True)
        assert result["signals"] == 4

        with be.open() as c:
            s = be.stats(c)
            assert s["row_counts"]["signals"] == 4
            assert s["row_counts"]["changes"] == 16

            # dsl_json prompt (aggregation mode)
            rows, hit = be.run_prompt(c, "change_count", {"limit": 10})
            assert rows and not hit
            rows2, hit2 = be.run_prompt(c, "change_count", {"limit": 10})
            assert hit2 is True and rows == rows2          # served from cache

            # waveform read
            sig = be.resolve_signal(c, "count")
            assert sig is not None
            assert be.value_at(c, sig.sig_id, 20) == (15, "00000001")

            # bug KB
            be.add_bug(c, "it-bug", symptom="integration probe",
                       keywords=["itest"],
                       links=[bugs.BugLink("signal", "top.u_cnt.count")])
            found = be.search_bugs(c, "integration")
            assert [b.name for b in found] == ["it-bug"]
            assert [b.name for b in be.list_bugs(c, status="open")] == ["it-bug"]
    finally:
        be.drop_indices()
