"""Phase 6 — raw-SQL CLI features are gated to the relational backend.

`modules`, `show`, and `xz` read via hand-written SQL, so on the OpenSearch
backend they must fail fast with a clear message (not crash on a client that
has no `.execute`). The guard fires before any connection is opened.
"""
from __future__ import annotations

import pytest
from click.testing import CliRunner

pytest.importorskip("opensearchpy")

from xevdb.cli import main                                           # noqa: E402
from xevdb.backends import opensearch_schema as schema              # noqa: E402


@pytest.fixture
def pointer(tmp_path):
    p = tmp_path / "ds.xevdb"
    schema.write_pointer(p, schema.Pointer(hosts=["localhost:9200"], dump_id="ds"))
    return str(p)


@pytest.mark.parametrize("argv", [
    ["show", "counter"],
    ["modules"],
    ["xz", "summary"],
    ["xz", "first"],
])
def test_raw_sql_features_gated_on_opensearch(pointer, argv):
    runner = CliRunner()
    # insert db path after the subcommand path that needs it
    cmd = ["--backend", "opensearch", argv[0]]
    if argv[0] == "xz":
        cmd = ["--backend", "opensearch", "xz", argv[1], pointer]
    else:
        cmd = ["--backend", "opensearch", *argv[:1], pointer, *argv[1:]]
    result = runner.invoke(main, cmd)
    assert result.exit_code != 0
    assert "relational (sqlite) backend" in result.output


def test_prompt_and_bug_not_gated(pointer):
    # non-raw features resolve the backend fine (they'd only fail later on a
    # real connection, which we don't make here) — the guard must NOT fire.
    runner = CliRunner()
    result = runner.invoke(main, ["--backend", "opensearch", "prompt", "show",
                                  pointer, "list_modules"])
    assert "relational (sqlite) backend" not in result.output
