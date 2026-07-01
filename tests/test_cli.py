"""End-to-end tests for the `xevdb` CLI (Click command bodies).

These drive `xevdb.cli.main` through `click.testing.CliRunner` against a real
`.xevdb` built from the bundled `examples/simple/counter.vcd`. Before this file
the CLI was the largest module in the tree yet only its argument parsing and the
backend guard were exercised — every command *body* (query, JSON formatting,
error/exit-code paths) was uncovered. The default sqlite backend needs no
optional dependencies, so all of this runs in any environment.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from xevdb.cli import main

REPO = Path(__file__).resolve().parents[1]
VCD = REPO / "examples" / "simple" / "counter.vcd"


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def db(runner, tmp_path) -> str:
    """A freshly built counter database, isolated per test."""
    path = str(tmp_path / "counter.xevdb")
    res = runner.invoke(main, ["build", str(VCD), "--db", path])
    assert res.exit_code == 0, res.output
    return path


def _json(result):
    assert result.exit_code == 0, result.output
    return json.loads(result.output)


# --------------------------------------------------------------------------
# build
# --------------------------------------------------------------------------

def test_build_reports_signals_and_time_range(db, runner):
    # `db` fixture already built once; rebuild with --reset to hit that branch.
    res = runner.invoke(main, ["build", str(VCD), "--db", db, "--reset"])
    assert res.exit_code == 0, res.output
    assert "4 signals" in res.output
    assert "16 changes" in res.output
    assert "t in [0, 40]" in res.output


def test_build_default_db_path_is_derived_from_vcd(runner, tmp_path):
    vcd_copy = tmp_path / "wave.vcd"
    vcd_copy.write_bytes(VCD.read_bytes())
    res = runner.invoke(main, ["build", str(vcd_copy)])
    assert res.exit_code == 0, res.output
    # <vcd>.xevdb sits next to the source when --db is omitted.
    assert (tmp_path / "wave.vcd.xevdb").exists()


def test_build_missing_vcd_is_a_usage_error(runner):
    res = runner.invoke(main, ["build", "does-not-exist.vcd"])
    assert res.exit_code != 0
    # click.Path(exists=True) rejects before any backend work.
    assert "does not exist" in res.output.lower() or "invalid" in res.output.lower()


# --------------------------------------------------------------------------
# at
# --------------------------------------------------------------------------

def test_at_human_output(db, runner):
    res = runner.invoke(main, ["at", db, "count", "-t", "20"])
    assert res.exit_code == 0, res.output
    assert "top.u_cnt.count" in res.output
    assert "last_t=15" in res.output
    assert "value=00000001" in res.output


def test_at_json_output(db, runner):
    payload = _json(runner.invoke(main, ["at", db, "count", "-t", "20", "--json"]))
    assert payload["signal"] == "top.u_cnt.count"
    assert payload["t"] == 20
    assert payload["last_t"] == 15
    assert payload["value"] == "00000001"
    assert payload["width"] == 8


def test_at_before_first_change_reports_no_value_json(db, runner):
    # t below the earliest change (t=0) → no value in effect.
    payload = _json(runner.invoke(main, ["at", db, "count", "-t", "-5", "--json"]))
    assert payload["value"] is None


def test_at_unknown_signal_errors(db, runner):
    res = runner.invoke(main, ["at", db, "no_such_signal", "-t", "0"])
    assert res.exit_code != 0
    assert "signal not found or ambiguous" in res.output


def test_at_requires_time_option(db, runner):
    res = runner.invoke(main, ["at", db, "count"])
    assert res.exit_code != 0  # --time is required


# --------------------------------------------------------------------------
# window
# --------------------------------------------------------------------------

def test_window_json_lists_all_changes(db, runner):
    payload = _json(runner.invoke(main, ["window", db, "count", "--json"]))
    times = [c["t"] for c in payload["changes"]]
    assert times == [0, 15, 25, 35]


def test_window_human_output(db, runner):
    res = runner.invoke(main, ["window", db, "count"])
    assert res.exit_code == 0, res.output
    assert "top.u_cnt.count\t0\t00000000" in res.output


def test_window_range_filters(db, runner):
    payload = _json(
        runner.invoke(main, ["window", db, "count", "--from", "10", "--to", "26", "--json"])
    )
    times = [c["t"] for c in payload["changes"]]
    assert times == [15, 25]


def test_window_unknown_signal_errors(db, runner):
    res = runner.invoke(main, ["window", db, "nope"])
    assert res.exit_code != 0
    assert "signal not found or ambiguous" in res.output


# --------------------------------------------------------------------------
# find
# --------------------------------------------------------------------------

def test_find_json(db, runner):
    hits = _json(runner.invoke(main, ["find", db, "count", "--json"]))
    assert any(h["fullname"] == "top.u_cnt.count" for h in hits)
    assert all({"id", "fullname", "width", "kind"} <= h.keys() for h in hits)


def test_find_human(db, runner):
    res = runner.invoke(main, ["find", db, "*clk*"])
    assert res.exit_code == 0, res.output
    assert "clk" in res.output


def test_find_no_match(db, runner):
    res = runner.invoke(main, ["find", db, "zzz_nothing"])
    assert res.exit_code == 0, res.output
    assert "no signals matching" in res.output


# --------------------------------------------------------------------------
# stats
# --------------------------------------------------------------------------

def test_stats_json(db, runner):
    info = _json(runner.invoke(main, ["stats", db, "--json"]))
    assert info["n_signals"] == "4"
    assert info["n_changes"] == "16"
    assert info["row_counts"]["signals"] == 4


def test_stats_human(db, runner):
    res = runner.invoke(main, ["stats", db])
    assert res.exit_code == 0, res.output
    assert "n_signals" in res.output
    assert "row_counts:" in res.output


# --------------------------------------------------------------------------
# diff
# --------------------------------------------------------------------------

def test_diff_identical_datasets_reports_no_divergence(db, runner, tmp_path):
    db_b = str(tmp_path / "b.xevdb")
    assert runner.invoke(main, ["build", str(VCD), "--db", db_b]).exit_code == 0
    res = runner.invoke(main, ["diff", db, db_b])
    assert res.exit_code == 0, res.output
    assert "no divergences" in res.output


def test_diff_json_shape(db, runner, tmp_path):
    db_b = str(tmp_path / "b.xevdb")
    runner.invoke(main, ["build", str(VCD), "--db", db_b])
    out = _json(runner.invoke(main, ["diff", db, db_b, "--json"]))
    assert out["n_divergent"] == 0
    assert isinstance(out["divergences"], list)
    assert out["n_common"] >= 1


# --------------------------------------------------------------------------
# riscv-decode  (no database needed)
# --------------------------------------------------------------------------

def test_riscv_decode_human(runner):
    res = runner.invoke(main, ["riscv-decode", "0x00c58533"])
    assert res.exit_code == 0, res.output
    assert "add" in res.output


def test_riscv_decode_json_multiple_words(runner):
    out = _json(runner.invoke(main, ["riscv-decode", "0x00c58533", "0x00450513", "--json"]))
    assert len(out) == 2
    assert out[0]["asm"].startswith("add")


def test_riscv_decode_bad_word_errors(runner):
    res = runner.invoke(main, ["riscv-decode", "not-a-word"])
    assert res.exit_code != 0
    assert res.output  # a ClickException message, not a traceback


# --------------------------------------------------------------------------
# decode  (word read off the waveform)
# --------------------------------------------------------------------------

def test_decode_signal_value(db, runner):
    res = runner.invoke(main, ["decode", db, "count", "-t", "20"])
    assert res.exit_code == 0, res.output
    assert "0x00000001" in res.output


def test_decode_unknown_signal_errors(db, runner):
    res = runner.invoke(main, ["decode", db, "nope", "-t", "0"])
    assert res.exit_code != 0
    assert "signal not found or ambiguous" in res.output


# --------------------------------------------------------------------------
# prompt list / show / run / add / remove
# --------------------------------------------------------------------------

def test_prompt_list_json_includes_seeded_prompts(db, runner):
    ps = _json(runner.invoke(main, ["prompt", "list", db, "--json"]))
    names = {p["name"] for p in ps}
    assert "change_count" in names


def test_prompt_show_unknown_errors(db, runner):
    res = runner.invoke(main, ["prompt", "show", db, "does_not_exist"])
    assert res.exit_code != 0


def test_prompt_run_cache_hit_on_second_call(db, runner):
    first = _json(runner.invoke(main, ["prompt", "run", db, "change_count", "--json"]))
    assert first["cache_hit"] is False
    second = _json(runner.invoke(main, ["prompt", "run", db, "change_count", "--json"]))
    assert second["cache_hit"] is True
    assert second["rows"] == first["rows"]


def test_prompt_run_no_cache_flag_skips_cache(db, runner):
    out = _json(runner.invoke(
        main, ["prompt", "run", db, "change_count", "--no-cache", "--json"]))
    assert out["cache_hit"] is False


def test_prompt_add_and_run_roundtrip(db, runner):
    add = runner.invoke(main, [
        "prompt", "add", db, "my_probe",
        "--sql", "SELECT COUNT(*) AS n FROM signals",
        "--description", "count signals",
    ])
    assert add.exit_code == 0, add.output
    assert "stored prompt 'my_probe'" in add.output

    out = _json(runner.invoke(main, ["prompt", "run", db, "my_probe", "--json"]))
    assert out["rows"][0]["n"] == 4


def test_prompt_add_requires_sql_or_file(db, runner):
    res = runner.invoke(main, ["prompt", "add", db, "empty"])
    assert res.exit_code != 0
    assert "provide one of --sql or --from-file" in res.output


def test_prompt_add_rejects_bad_params_json(db, runner):
    res = runner.invoke(main, [
        "prompt", "add", db, "bad", "--sql", "SELECT 1", "--params-json", "{not a list}",
    ])
    assert res.exit_code != 0
    assert "--params-json" in res.output


def test_prompt_remove(db, runner):
    runner.invoke(main, ["prompt", "add", db, "temp", "--sql", "SELECT 1 AS x"])
    res = runner.invoke(main, ["prompt", "remove", db, "temp"])
    assert res.exit_code == 0, res.output
    assert "removed 'temp'" in res.output
    # Removing again reports the miss without failing.
    again = runner.invoke(main, ["prompt", "remove", db, "temp"])
    assert "no prompt named 'temp'" in again.output


# --------------------------------------------------------------------------
# cache stats / list / clear
# --------------------------------------------------------------------------

def test_cache_stats_empty(db, runner):
    info = _json(runner.invoke(main, ["cache", "stats", db, "--json"]))
    assert info["entries"] == 0


def test_cache_populated_then_cleared(db, runner):
    # Populate the cache via a prompt run, then inspect and clear it.
    runner.invoke(main, ["prompt", "run", db, "change_count"])
    stats = _json(runner.invoke(main, ["cache", "stats", db, "--json"]))
    assert stats["entries"] >= 1

    listed = _json(runner.invoke(main, ["cache", "list", db, "--json"]))
    assert any(r["prompt"] == "change_count" for r in listed)

    cleared = runner.invoke(main, ["cache", "clear", db, "--yes"])
    assert cleared.exit_code == 0, cleared.output
    assert "deleted" in cleared.output
    after = _json(runner.invoke(main, ["cache", "stats", db, "--json"]))
    assert after["entries"] == 0


def test_cache_clear_aborts_without_confirmation(db, runner):
    runner.invoke(main, ["prompt", "run", db, "change_count"])
    # No --yes and 'n' at the confirm prompt → abort, cache untouched.
    res = runner.invoke(main, ["cache", "clear", db], input="n\n")
    assert res.exit_code != 0
    stats = _json(runner.invoke(main, ["cache", "stats", db, "--json"]))
    assert stats["entries"] >= 1


# --------------------------------------------------------------------------
# bug add / show / list / search / link / close / remove
# --------------------------------------------------------------------------

def test_bug_add_show_and_list(db, runner):
    add = runner.invoke(main, [
        "bug", "add", db, "reset_glitch",
        "--title", "reset not asserted",
        "--symptom", "count keeps incrementing during reset",
        "--severity", "error", "--keyword", "reset",
    ])
    assert add.exit_code == 0, add.output
    assert "stored bug 'reset_glitch'" in add.output

    shown = _json(runner.invoke(main, ["bug", "show", db, "reset_glitch", "--json"]))
    assert shown["name"] == "reset_glitch"
    assert shown["severity"] == "error"

    listed = _json(runner.invoke(main, ["bug", "list", db, "--json"]))
    assert any(b["name"] == "reset_glitch" for b in listed)


def test_bug_show_unknown_errors(db, runner):
    res = runner.invoke(main, ["bug", "show", db, "ghost"])
    assert res.exit_code != 0
    assert "no bug named 'ghost'" in res.output


def test_bug_search_finds_by_symptom(db, runner):
    runner.invoke(main, [
        "bug", "add", db, "clk_skew",
        "--symptom", "sampling window violated by clock skew",
    ])
    hits = _json(runner.invoke(main, ["bug", "search", db, "skew", "--json"]))
    assert any(b["name"] == "clk_skew" for b in hits)


def test_bug_link_requires_exactly_one_target(db, runner):
    runner.invoke(main, ["bug", "add", db, "b1"])
    res = runner.invoke(main, ["bug", "link", db, "b1"])  # nothing provided
    assert res.exit_code != 0
    assert "exactly one" in res.output


def test_bug_link_and_close(db, runner):
    runner.invoke(main, ["bug", "add", db, "b2"])
    linked = runner.invoke(main, ["bug", "link", db, "b2", "--signal", "top.u_cnt.count"])
    assert linked.exit_code == 0, linked.output
    assert "linked signal=" in linked.output

    closed = runner.invoke(main, ["bug", "close", db, "b2", "--fix", "assert reset"])
    assert closed.exit_code == 0, closed.output
    assert "-> fixed" in closed.output


def test_bug_remove(db, runner):
    runner.invoke(main, ["bug", "add", db, "gone"])
    res = runner.invoke(main, ["bug", "remove", db, "gone"])
    assert "removed 'gone'" in res.output
    again = runner.invoke(main, ["bug", "remove", db, "gone"])
    assert "no bug named 'gone'" in again.output


# --------------------------------------------------------------------------
# xz — the counter fixture is entirely 2-state, so these hit the empty paths.
# --------------------------------------------------------------------------

def test_xz_summary_reports_clean_trace(db, runner):
    res = runner.invoke(main, ["xz", "summary", db])
    assert res.exit_code == 0, res.output
    assert "no X/Z found" in res.output


def test_xz_first_json_empty(db, runner):
    out = _json(runner.invoke(main, ["xz", "first", db, "--json"]))
    assert out == []


def test_xz_signal_never_xz(db, runner):
    res = runner.invoke(main, ["xz", "signal", db, "count"])
    assert res.exit_code == 0, res.output
    assert "never X/Z" in res.output
