"""`xevdb` — VCD + SystemVerilog → single SQLite file.

Direct queries:
    xevdb build  <vcd>                          # parse VCD → .xevdb file
    xevdb at     <db> <signal> --time <t>
    xevdb window <db> <signal> --from <t0> --to <t1>
    xevdb find   <db> <pattern>
    xevdb stats  <db>

RTL ingest + display:
    xevdb ingest-rtl <db> <path>                # parse .sv/.v files into the DB
    xevdb modules    <db> [--filter <name>]     # list parsed modules
    xevdb show       <db> <target> [--full]     # show RTL code for module/signal/file:line

Stored query prompts (parameterized SQL in the .xevdb):
    xevdb prompt list/show/run/add/remove

Cache inspection (cache table inside the .xevdb):
    xevdb cache stats/list/clear
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import click

from . import db as _db
from . import prompts as _prompts
from . import cache as _cache
from . import show as _show
from . import sv as _sv
from . import xztrace as _xz


# ----------------------------------------------------------------------------
@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(package_name="xevdb")
def main() -> None:
    """VCD + SystemVerilog database with stored prompts and a result cache."""


# ----------------------------------------------------------------------------
# build / VCD-side commands
# ----------------------------------------------------------------------------

def _default_db_path(vcd_path: str) -> str:
    p = Path(vcd_path)
    return str(p.with_suffix(p.suffix + ".xevdb"))


@main.command()
@click.argument("vcd_path", type=click.Path(exists=True, dir_okay=False))
@click.option("--db", "db_path", type=click.Path(),
              help="Output database path. Default: <vcd_path>.xevdb")
@click.option("--reset", is_flag=True, help="Replace any existing database.")
@click.option("--no-seed", is_flag=True, help="Skip installing the standard prompt library.")
def build(vcd_path: str, db_path: str | None, reset: bool, no_seed: bool) -> None:
    """Parse VCD_PATH and write a self-contained .xevdb file."""
    if db_path is None:
        db_path = _default_db_path(vcd_path)
    result = _db.build(vcd_path, db_path, reset=reset, seed=not no_seed)
    click.echo(
        f"built {db_path} from {vcd_path}: "
        f"{result['signals']} signals, {result['changes']} changes, "
        f"t in [{result['t_min']}, {result['t_max']}]"
    )


@main.command()
@click.argument("db_path", type=click.Path(exists=True, dir_okay=False))
@click.argument("signal")
@click.option("--time", "-t", "t", type=int, required=True,
              help="Timestamp (last value at-or-before t).")
@click.option("--json", "as_json", is_flag=True)
def at(db_path: str, signal: str, t: int, as_json: bool) -> None:
    """Value of SIGNAL at time T (last change at-or-before t)."""
    with _db.open_db(db_path, read_only=True) as con:
        sig = _db.resolve_signal(con, signal)
        if sig is None:
            raise click.ClickException(f"signal not found or ambiguous: {signal!r}")
        result = _db.value_at(con, sig.sig_id, t)
        if result is None:
            if as_json:
                click.echo(json.dumps({"signal": sig.fullname, "t": t, "value": None}))
            else:
                click.echo(f"{sig.fullname}\t@{t}\t<no value before t>")
            return
        last_t, value = result
        if as_json:
            click.echo(json.dumps({
                "signal": sig.fullname, "id": sig.sig_id, "width": sig.width,
                "kind": sig.kind, "t": t, "last_t": last_t, "value": value,
            }))
        else:
            click.echo(f"{sig.fullname}\t@{t}\tlast_t={last_t}\tvalue={value}")


@main.command()
@click.argument("db_path", type=click.Path(exists=True, dir_okay=False))
@click.argument("signal")
@click.option("--from", "t0", type=int, default=None)
@click.option("--to", "t1", type=int, default=None)
@click.option("--limit", type=int, default=200, show_default=True)
@click.option("--json", "as_json", is_flag=True)
def window(db_path: str, signal: str, t0: int | None, t1: int | None,
           limit: int, as_json: bool) -> None:
    """All value changes of SIGNAL within a time window."""
    with _db.open_db(db_path, read_only=True) as con:
        sig = _db.resolve_signal(con, signal)
        if sig is None:
            raise click.ClickException(f"signal not found or ambiguous: {signal!r}")
        rows = _db.window(con, sig.sig_id, t0, t1, limit)
        if as_json:
            click.echo(json.dumps({
                "signal": sig.fullname, "from": t0, "to": t1,
                "changes": [{"t": t, "value": v} for t, v in rows],
            }))
            return
        if not rows:
            click.echo(f"{sig.fullname}\t<no changes in window>")
            return
        for t, v in rows:
            click.echo(f"{sig.fullname}\t{t}\t{v}")


@main.command()
@click.argument("db_path", type=click.Path(exists=True, dir_okay=False))
@click.argument("pattern")
@click.option("--limit", type=int, default=50, show_default=True)
@click.option("--json", "as_json", is_flag=True)
def find(db_path: str, pattern: str, limit: int, as_json: bool) -> None:
    """Glob (`*reg_pc*`) or substring search for signals."""
    with _db.open_db(db_path, read_only=True) as con:
        hits = _db.find_signals(con, pattern, limit)
        if as_json:
            click.echo(json.dumps([
                {"id": s.sig_id, "fullname": s.fullname, "width": s.width, "kind": s.kind}
                for s in hits
            ]))
            return
        if not hits:
            click.echo(f"no signals matching {pattern!r}")
            return
        for s in hits:
            click.echo(f"{s.sig_id}\t{s.kind}\t{s.width}\t{s.fullname}")


@main.command()
@click.argument("db_path", type=click.Path(exists=True, dir_okay=False))
@click.option("--json", "as_json", is_flag=True)
def stats(db_path: str, as_json: bool) -> None:
    """Database statistics: VCD + RTL counts, time range, source VCD."""
    with _db.open_db(db_path, read_only=True) as con:
        info = _db.stats(con)
    if as_json:
        click.echo(json.dumps(info, indent=2))
        return
    order = ("source", "timescale", "n_signals", "n_changes",
             "t_min", "t_max", "date", "version", "xevdb_version")
    for k in order:
        if k in info:
            click.echo(f"{k:<14s}{info[k]}")
    click.echo("row_counts:")
    for k, v in info["row_counts"].items():
        click.echo(f"  {k:<18s}{v}")


# ----------------------------------------------------------------------------
# RTL ingest + display
# ----------------------------------------------------------------------------

@main.command(name="ingest-sim")
@click.argument("db_path", type=click.Path(exists=True, dir_okay=False))
@click.argument("log_path", type=click.Path(exists=True, dir_okay=False))
@click.option("--name", default=None,
              help="Short identifier for this run (defaults to log basename).")
@click.option("--keep-all", is_flag=True,
              help="Keep every non-blank line (severity='INFO' if no severity matched). "
                   "Default: only keep lines with a recognized severity keyword.")
@click.option("--reset", is_flag=True,
              help="Drop every existing sim_runs/sim_events row before ingesting.")
def ingest_sim_cmd(db_path: str, log_path: str, name: str | None,
                   keep_all: bool, reset: bool) -> None:
    """Parse a simulator log and insert it into an existing .xevdb file.

    Recognized severities: UVM_INFO/WARNING/ERROR/FATAL, plain ERROR/WARN/
    FATAL/NOTE, and Assertion failures. Embedded simulation times (e.g.
    `@ 200:`, `[100]`, `# 100:`, `at 1500 ps`) and file:line refs are
    extracted into dedicated columns for indexed querying.
    """
    result = _db.ingest_sim(log_path, db_path, name=name,
                            keep_all=keep_all, reset=reset)
    click.echo(
        f"ingested {result['line_count']} lines from {log_path} into {db_path}: "
        f"{result['events']} events "
        f"(fatal={result['fatal']}, error={result['error']}, warning={result['warning']})"
    )


@main.command(name="ingest-rtl")
@click.argument("db_path", type=click.Path(exists=True, dir_okay=False))
@click.argument("rtl_path", type=click.Path(exists=True))
@click.option("--reset", is_flag=True,
              help="Drop existing module/source rows before ingesting.")
def ingest_rtl_cmd(db_path: str, rtl_path: str, reset: bool) -> None:
    """Parse .v/.sv files under RTL_PATH and add them to an existing .xevdb file."""
    if not _sv.have_sv_parse():
        raise click.ClickException(
            f"sv-parse binary not found at {_sv.DEFAULT_SV_PARSE}. "
            "Build xezim-core/xezim-parser (`cargo build --release`) "
            "or set XEVDB_SV_PARSE to its location."
        )
    result = _db.ingest_rtl(rtl_path, db_path, reset=reset)
    click.echo(
        f"ingested {result['files']} files into {db_path}: "
        f"{result['modules']} modules, {result['ports']} ports, "
        f"{result['signals']} internal signals, {result['instances']} instantiations"
    )


@main.command()
@click.argument("db_path", type=click.Path(exists=True, dir_okay=False))
@click.option("--filter", "filter_name", default=None,
              help="Only list modules whose name matches this LIKE pattern.")
@click.option("--limit", type=int, default=200, show_default=True)
@click.option("--json", "as_json", is_flag=True)
def modules(db_path: str, filter_name: str | None, limit: int, as_json: bool) -> None:
    """List parsed RTL modules."""
    with _db.open_db(db_path, read_only=True) as con:
        if filter_name:
            rows = con.execute(
                "SELECT name, kind, file, line_start, line_end, body_summary "
                "FROM modules WHERE name LIKE ? ORDER BY file, line_start LIMIT ?",
                (filter_name, limit),
            ).fetchall()
        else:
            rows = con.execute(
                "SELECT name, kind, file, line_start, line_end, body_summary "
                "FROM modules ORDER BY file, line_start LIMIT ?",
                (limit,),
            ).fetchall()
    if as_json:
        click.echo(json.dumps([dict(r) for r in rows], indent=2))
        return
    if not rows:
        click.echo("(no modules)")
        return
    for r in rows:
        click.echo(f"{r['file']}:{r['line_start']:<5d}{r['name']:<24s}  {r['body_summary']}")


@main.command()
@click.argument("db_path", type=click.Path(exists=True, dir_okay=False))
@click.argument("target")
@click.option("--full", is_flag=True, help="Show the entire module body (not just the header).")
@click.option("--context", type=int, default=6, show_default=True,
              help="Lines of context around the target.")
@click.option("--no-line-numbers", is_flag=True)
def show(db_path: str, target: str, full: bool, context: int, no_line_numbers: bool) -> None:
    """Show RTL source code for a signal, module, or file:line target.

    Resolution: file:line → module name → signal/port name → bare-name match
    on a hierarchical VCD path. All slices are read from the source_files
    table inside the .xevdb (the original .sv files are not needed).
    """
    with _db.open_db(db_path, read_only=True) as con:
        slices = _show.show_code(con, target, context=context, full=full)
    if not slices:
        raise click.ClickException(f"no RTL match for {target!r}")
    for i, sl in enumerate(slices):
        if i > 0:
            click.echo("")
        click.echo(_show.render(sl, with_lines=not no_line_numbers))


# ----------------------------------------------------------------------------
# prompt …
# ----------------------------------------------------------------------------

@main.group()
def prompt() -> None:
    """List, run, add, or remove stored query prompts."""


@prompt.command(name="list")
@click.argument("db_path", type=click.Path(exists=True, dir_okay=False))
@click.option("--json", "as_json", is_flag=True)
def prompt_list(db_path: str, as_json: bool) -> None:
    """List every stored prompt with its first-line description."""
    with _db.open_db(db_path, read_only=True) as con:
        ps = _prompts.list_prompts(con)
    if as_json:
        click.echo(json.dumps([
            {"name": p.name, "description": p.description, "params": p.params} for p in ps
        ], indent=2))
        return
    if not ps:
        click.echo("(no prompts)")
        return
    for p in ps:
        first = (p.description.splitlines() or [""])[0]
        click.echo(f"{p.name:<22s}{first}")


@prompt.command(name="show")
@click.argument("db_path", type=click.Path(exists=True, dir_okay=False))
@click.argument("name")
def prompt_show(db_path: str, name: str) -> None:
    """Print a prompt's description, parameter list, and SQL."""
    with _db.open_db(db_path, read_only=True) as con:
        try:
            p = _prompts.show_prompt(con, name)
        except KeyError as e:
            raise click.ClickException(str(e))
    click.echo(f"name:        {p.name}")
    click.echo(f"description: {p.description}")
    if p.params:
        click.echo("params:")
        for spec in p.params:
            click.echo(
                f"  - {spec['name']:<14s}({spec.get('type', 'str'):<5s}) "
                f"default={spec.get('default')!r:<20s} "
                f"{spec.get('description', '')}"
            )
    click.echo("sql:")
    for line in p.sql.splitlines():
        click.echo(f"  {line}")


def _parse_args(pairs: tuple[str, ...]) -> dict[str, str]:
    out: dict[str, str] = {}
    for pair in pairs:
        if "=" not in pair:
            raise click.ClickException(f"--arg expects KEY=VALUE, got: {pair!r}")
        k, _, v = pair.partition("=")
        out[k.strip()] = v
    return out


@prompt.command(name="run")
@click.argument("db_path", type=click.Path(exists=True, dir_okay=False))
@click.argument("name")
@click.option("--arg", "args", multiple=True, metavar="KEY=VALUE")
@click.option("--no-cache", is_flag=True)
@click.option("--ttl", type=int, default=0)
@click.option("--json", "as_json", is_flag=True)
@click.option("--limit-output", type=int, default=200, show_default=True)
def prompt_run(db_path: str, name: str, args: tuple[str, ...],
               no_cache: bool, ttl: int, as_json: bool, limit_output: int) -> None:
    """Run a stored prompt with --arg overrides."""
    arg_dict = _parse_args(args)
    with _db.open_db(db_path, read_only=False) as con:
        try:
            rows, hit = _prompts.run_prompt(con, name, arg_dict,
                                            use_cache=not no_cache, ttl_seconds=ttl)
        except KeyError as e:
            raise click.ClickException(str(e))
    if as_json:
        click.echo(json.dumps({"prompt": name, "args": arg_dict,
                               "cache_hit": hit, "rows": rows},
                              indent=2, default=str))
        return
    if not rows:
        click.echo(f"(no rows)  cache_hit={hit}")
        return
    cols = list(rows[0].keys())
    click.echo("\t".join(cols))
    for r in rows[:limit_output]:
        click.echo("\t".join("" if r[c] is None else str(r[c]) for c in cols))
    if len(rows) > limit_output:
        click.echo(f"... ({len(rows) - limit_output} more rows; use --json for all)")
    if hit:
        click.echo(f"\n(served from cache; {len(rows)} rows)", err=True)


@prompt.command(name="add")
@click.argument("db_path", type=click.Path(exists=True, dir_okay=False))
@click.argument("name")
@click.option("--sql", "sql_text", required=False)
@click.option("--from-file", "from_file", type=click.Path(exists=True, dir_okay=False))
@click.option("--description", default="")
@click.option("--params-json", default="[]")
@click.option("--overwrite", is_flag=True)
def prompt_add(db_path: str, name: str, sql_text: str | None, from_file: str | None,
               description: str, params_json: str, overwrite: bool) -> None:
    """Store a new prompt or update an existing one."""
    if not sql_text and not from_file:
        raise click.ClickException("provide one of --sql or --from-file")
    if from_file:
        sql_text = Path(from_file).read_text()
    try:
        params = json.loads(params_json)
        if not isinstance(params, list):
            raise ValueError("--params-json must decode to a list")
    except (json.JSONDecodeError, ValueError) as e:
        raise click.ClickException(f"--params-json: {e}")
    with _db.open_db(db_path, read_only=False) as con:
        try:
            _prompts.add_prompt(con, name, sql_text, description=description,
                                params=params, overwrite=overwrite)
        except Exception as e:
            raise click.ClickException(str(e))
    click.echo(f"stored prompt {name!r} in {db_path}")


@prompt.command(name="remove")
@click.argument("db_path", type=click.Path(exists=True, dir_okay=False))
@click.argument("name")
def prompt_remove(db_path: str, name: str) -> None:
    """Delete a stored prompt."""
    with _db.open_db(db_path, read_only=False) as con:
        gone = _prompts.remove_prompt(con, name)
    click.echo(f"removed {name!r}" if gone else f"no prompt named {name!r}")


# ----------------------------------------------------------------------------
# cache …
# ----------------------------------------------------------------------------

@main.group()
def cache() -> None:
    """Inspect or clear the per-database query-result cache."""


@cache.command(name="stats")
@click.argument("db_path", type=click.Path(exists=True, dir_okay=False))
@click.option("--json", "as_json", is_flag=True)
def cache_stats(db_path: str, as_json: bool) -> None:
    """Cache size, hits, breakdown by prompt."""
    with _db.open_db(db_path, read_only=True) as con:
        info = _cache.stats(con)
    if as_json:
        click.echo(json.dumps(info, indent=2))
        return
    click.echo(f"enabled       {info['enabled']}")
    click.echo(f"entries       {info['entries']}")
    click.echo(f"total_hits    {info['total_hits']}")
    click.echo(f"result_bytes  {info['result_bytes']}")
    if info["by_prompt"]:
        click.echo("by_prompt:")
        for k, v in info["by_prompt"].items():
            click.echo(f"  {k:<22s}{v}")


@cache.command(name="list")
@click.argument("db_path", type=click.Path(exists=True, dir_okay=False))
@click.option("--prompt", "prompt_name", default=None)
@click.option("--limit", type=int, default=20, show_default=True)
@click.option("--json", "as_json", is_flag=True)
def cache_list(db_path: str, prompt_name: str | None, limit: int, as_json: bool) -> None:
    """List recent cache entries (newest first)."""
    with _db.open_db(db_path, read_only=True) as con:
        rows = _cache.list_entries(con, prompt=prompt_name, limit=limit)
    if as_json:
        click.echo(json.dumps(rows, indent=2, default=str))
        return
    if not rows:
        click.echo("(no entries)")
        return
    for r in rows:
        click.echo(
            f"{r['key']:<18s}{r['prompt']:<22s}{r['bytes']:>8d} bytes  "
            f"{r['hits']:>3d} hits  args={r['args']}"
        )


@cache.command(name="clear")
@click.argument("db_path", type=click.Path(exists=True, dir_okay=False))
@click.option("--prompt", "prompt_name", default=None)
@click.option("--yes", is_flag=True)
def cache_clear(db_path: str, prompt_name: str | None, yes: bool) -> None:
    """Delete cache entries (all or for one prompt)."""
    if not yes:
        msg = (f"clear cache entries for prompt={prompt_name!r}" if prompt_name
               else "clear ALL cache entries")
        click.confirm(f"{msg}?", abort=True)
    with _db.open_db(db_path, read_only=False) as con:
        n = _cache.clear(con, prompt=prompt_name)
    click.echo(f"deleted {n} entries")


# ----------------------------------------------------------------------------
# xz — X/Z state tracing
# ----------------------------------------------------------------------------

@main.group()
def xz() -> None:
    """Trace unknown (`x`) / high-impedance (`z`) states through the VCD.

    \b
    xevdb xz summary   <db>                       overview + root-cause set
    xevdb xz first     <db> [--limit N]           earliest signals to go X/Z
    xevdb xz signal    <db> <signal>              enter/leave timeline
    xevdb xz at        <db> --time T              every signal in X/Z at T
    xevdb xz propagate <db> <seed> [--window W]   downstream X/Z candidates
    """


@xz.command(name="summary")
@click.argument("db_path", type=click.Path(exists=True, dir_okay=False))
@click.option("--json", "as_json", is_flag=True)
def xz_summary(db_path: str, as_json: bool) -> None:
    """Overview: how widespread the X/Z is, and the root-cause set."""
    with _db.open_db(db_path, read_only=True) as con:
        ov = _xz.overview(con)
    if as_json:
        click.echo(json.dumps(_xz.to_dict(ov), indent=2))
        return
    if ov.xz_signals == 0:
        click.echo("no X/Z found — every signal stayed 2-state for the whole trace")
        return
    pct_sig = 100.0 * ov.xz_signals / max(ov.total_signals, 1)
    pct_chg = 100.0 * ov.xz_changes / max(ov.total_changes, 1)
    click.echo(f"X/Z signals    : {ov.xz_signals:>8d} / {ov.total_signals} "
               f"({pct_sig:.1f}%)")
    click.echo(f"X/Z changes    : {ov.xz_changes:>8d} / {ov.total_changes} "
               f"({pct_chg:.1f}%)")
    click.echo(f"first X/Z at t : {ov.first_xz_t}")
    click.echo(f"last  X/Z at t : {ov.last_xz_t}")
    click.echo(f"root-cause set : {len(ov.first_xz_signals)} signal(s) "
               f"go X/Z at t={ov.first_xz_t}")
    for name in ov.first_xz_signals[:20]:
        click.echo(f"    {name}")
    if len(ov.first_xz_signals) > 20:
        click.echo(f"    … +{len(ov.first_xz_signals) - 20} more")


@xz.command(name="first")
@click.argument("db_path", type=click.Path(exists=True, dir_okay=False))
@click.option("--limit", type=int, default=50, show_default=True)
@click.option("--json", "as_json", is_flag=True)
def xz_first(db_path: str, limit: int, as_json: bool) -> None:
    """Signals ranked by the time they FIRST went X/Z (root causes first)."""
    with _db.open_db(db_path, read_only=True) as con:
        rows = _xz.first(con, limit)
    if as_json:
        click.echo(json.dumps(_xz.to_dict(rows), indent=2))
        return
    if not rows:
        click.echo("no X/Z found")
        return
    click.echo(f"{'first_t':>10s}  {'kind':<4s} {'w':>4s} {'#xz':>6s}  signal")
    for r in rows:
        click.echo(f"{r.first_xz_t:>10d}  {r.xz_kind:<4s} {r.width:>4d} "
                   f"{r.xz_change_count:>6d}  {r.fullname}")


@xz.command(name="signal")
@click.argument("db_path", type=click.Path(exists=True, dir_okay=False))
@click.argument("signal")
@click.option("--json", "as_json", is_flag=True)
def xz_signal(db_path: str, signal: str, as_json: bool) -> None:
    """Enter/leave X/Z intervals for one SIGNAL over the whole trace."""
    with _db.open_db(db_path, read_only=True) as con:
        sig = _db.resolve_signal(con, signal)
        if sig is None:
            raise click.ClickException(
                f"signal not found or ambiguous: {signal!r}")
        ivals = _xz.timeline(con, sig.sig_id)
    if as_json:
        click.echo(json.dumps({
            "signal": sig.fullname,
            "intervals": _xz.to_dict(ivals),
        }, indent=2))
        return
    if not ivals:
        click.echo(f"{sig.fullname}\t<never X/Z>")
        return
    click.echo(f"{sig.fullname}  ({len(ivals)} X/Z interval(s))")
    for iv in ivals:
        if iv.leave_t is None:
            span = f"t={iv.enter_t} → (end of trace)"
        else:
            span = f"t={iv.enter_t} → t={iv.leave_t} ({iv.leave_t - iv.enter_t} ticks)"
        click.echo(f"  [{iv.xz_kind:<2s}] {span}  enter={iv.enter_value}")


@xz.command(name="at")
@click.argument("db_path", type=click.Path(exists=True, dir_okay=False))
@click.option("--time", "-t", "t", type=int, required=True,
              help="Simulation time to snapshot.")
@click.option("--limit", type=int, default=200, show_default=True)
@click.option("--json", "as_json", is_flag=True)
def xz_at(db_path: str, t: int, limit: int, as_json: bool) -> None:
    """Every signal sitting in X/Z at simulation time T (waveform semantic)."""
    with _db.open_db(db_path, read_only=True) as con:
        rows = _xz.at(con, t, limit)
    if as_json:
        click.echo(json.dumps({
            "time": t, "xz_signals": _xz.to_dict(rows),
        }, indent=2))
        return
    if not rows:
        click.echo(f"no signals in X/Z at t={t}")
        return
    click.echo(f"{len(rows)} signal(s) in X/Z at t={t}:")
    click.echo(f"{'since_t':>10s}  {'kind':<4s} {'w':>4s}  signal = value")
    for r in rows:
        click.echo(f"{r.since_t:>10d}  {r.xz_kind:<4s} {r.width:>4d}  "
                   f"{r.fullname} = {r.value}")


@xz.command(name="propagate")
@click.argument("db_path", type=click.Path(exists=True, dir_okay=False))
@click.argument("seed")
@click.option("--window", "-w", type=int, default=None,
              help="Only candidates within [seed_t, seed_t+W]. Default: all.")
@click.option("--limit", type=int, default=100, show_default=True)
@click.option("--json", "as_json", is_flag=True)
def xz_propagate(db_path: str, seed: str, window: int | None,
                 limit: int, as_json: bool) -> None:
    """Trace X/Z propagation from SEED — signals that go X/Z at or after it.

    Ranks downstream signals by how soon after the seed they turned X/Z.
    If the .xevdb has RTL ingested, each candidate shows the module + line
    where a same-named signal is declared.
    """
    with _db.open_db(db_path, read_only=True) as con:
        sig = _db.resolve_signal(con, seed)
        if sig is None:
            raise click.ClickException(
                f"seed signal not found or ambiguous: {seed!r}")
        seed_t, cands = _xz.propagate(con, sig.sig_id, window, limit)
    if seed_t < 0:
        raise click.ClickException(
            f"seed signal {sig.fullname!r} never went X/Z")
    if as_json:
        click.echo(json.dumps({
            "seed": sig.fullname, "seed_first_xz_t": seed_t,
            "window": window, "candidates": _xz.to_dict(cands),
        }, indent=2))
        return
    click.echo(f"seed {sig.fullname} first X/Z at t={seed_t}")
    if not cands:
        click.echo("no downstream X/Z candidates")
        return
    click.echo(f"{len(cands)} propagation candidate(s):")
    click.echo(f"{'+dt':>9s}  {'first_t':>10s}  {'kind':<4s}  signal  [rtl]")
    for c in cands:
        rtl = ""
        if c.rtl_module:
            rtl = f"  [{c.rtl_module} @ {c.rtl_file}:{c.rtl_line}]"
        click.echo(f"{c.delta_t:>9d}  {c.first_xz_t:>10d}  {c.xz_kind:<4s}  "
                   f"{c.fullname}{rtl}")


if __name__ == "__main__":
    main()
