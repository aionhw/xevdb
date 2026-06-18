"""`xevdb` — VCD + SystemVerilog → single SQLite file.

Direct queries:
    xevdb build  <vcd>                          # parse VCD → .xevdb file
    xevdb build-xtrace <xtrace>                 # parse XTrace → .xevdb file
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
from pathlib import Path

import click

from . import db as _db
from . import show as _show
from . import sv as _sv
from . import xztrace as _xz
from . import bugs as _bugs
from . import backends as _backends


# Backend chosen for this invocation. Set by the `main` group callback from
# --backend / $XEVDB_BACKEND; consumed by `_backend()`. None means "default".
_BACKEND_NAME: str | None = None


def _backend(db_path: str) -> _backends.Backend:
    """The selected backend bound to `db_path`."""
    try:
        return _backends.get_backend(_BACKEND_NAME, db_path)
    except ValueError as e:
        raise click.ClickException(str(e))


def _require_raw_sql(db_path: str, feature: str) -> None:
    """Raise unless the selected backend supports raw-SQL features.

    `modules`, `show`, and `xz` read via hand-written SQL over a DB-API
    connection; on a document-store backend (OpenSearch) that connection is a
    client, not a cursor, so these stay relational-only for now.
    """
    backend = _backend(db_path)
    if not backend.supports_raw_sql:
        raise click.ClickException(
            f"`{feature}` is only available on a relational (sqlite) backend; "
            f"the {backend.name!r} backend does not support it yet.")


# ----------------------------------------------------------------------------
@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(package_name="xevdb")
@click.option("--backend", "backend_name", default=None, metavar="NAME",
              help="Storage backend (default: $XEVDB_BACKEND or 'sqlite'). "
                   f"Available: {', '.join(_backends.available_backends())}.")
def main(backend_name: str | None) -> None:
    """VCD + SystemVerilog database with stored prompts and a result cache."""
    global _BACKEND_NAME
    _BACKEND_NAME = backend_name


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
    result = _backend(db_path).build(vcd_path, reset=reset, seed=not no_seed)
    click.echo(
        f"built {db_path} from {vcd_path}: "
        f"{result['signals']} signals, {result['changes']} changes, "
        f"t in [{result['t_min']}, {result['t_max']}]"
    )


@main.command(name="build-xtrace")
@click.argument("xtrace_path", type=click.Path(exists=True, dir_okay=False))
@click.option("--db", "db_path", type=click.Path(),
              help="Output database path. Default: <xtrace_path>.xevdb")
@click.option("--reset", is_flag=True, help="Replace any existing database.")
@click.option("--no-seed", is_flag=True, help="Skip installing the standard prompt library.")
def build_xtrace(xtrace_path: str, db_path: str | None, reset: bool, no_seed: bool) -> None:
    """Parse XTRACE_PATH and write a self-contained .xevdb file."""
    if db_path is None:
        db_path = _default_db_path(xtrace_path)
    result = _backend(db_path).build_xtrace(xtrace_path, reset=reset, seed=not no_seed)
    click.echo(
        f"built {db_path} from {xtrace_path}: "
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
    backend = _backend(db_path)
    with backend.open(read_only=True) as con:
        sig = backend.resolve_signal(con, signal)
        if sig is None:
            raise click.ClickException(f"signal not found or ambiguous: {signal!r}")
        result = backend.value_at(con, sig.sig_id, t)
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
    backend = _backend(db_path)
    with backend.open(read_only=True) as con:
        sig = backend.resolve_signal(con, signal)
        if sig is None:
            raise click.ClickException(f"signal not found or ambiguous: {signal!r}")
        rows = backend.window(con, sig.sig_id, t0, t1, limit)
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
    backend = _backend(db_path)
    with backend.open(read_only=True) as con:
        hits = backend.find_signals(con, pattern, limit)
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
    backend = _backend(db_path)
    with backend.open(read_only=True) as con:
        info = backend.stats(con)
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


@main.command()
@click.argument("db_path", type=click.Path(exists=True, dir_okay=False))
def mcp(db_path: str) -> None:
    """Serve DB_PATH to AI agents over MCP (stdio).

    Exposes the dataset's queries — values, windows, signal search, the stored
    prompt library (waveform/RTL/sim/bug/RISC-V/kernel), and the bug KB — as MCP
    tools. Configure it in an MCP client, e.g.:

        {"mcpServers": {"xevdb": {"command": "xevdb",
                                  "args": ["mcp", "/path/to/db.xevdb"]}}}
    """
    from . import mcp_server
    mcp_server.serve(db_path, _BACKEND_NAME)


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
    result = _backend(db_path).ingest_sim(log_path, name=name,
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
    result = _backend(db_path).ingest_rtl(rtl_path, reset=reset)
    click.echo(
        f"ingested {result['files']} files into {db_path}: "
        f"{result['modules']} modules, {result['ports']} ports, "
        f"{result['signals']} internal signals, {result['instances']} instantiations"
    )


@main.command(name="ingest-riscv")
@click.argument("db_path", type=click.Path())
@click.option("--data", "data_dir", type=click.Path(exists=True, file_okay=False),
              default=None,
              help="Directory of RISC-V reference JSON. Default: bundled data.")
@click.option("--reset", is_flag=True,
              help="Drop existing riscv_* indices before ingesting.")
@click.option("--no-seed", is_flag=True,
              help="Skip installing the standard prompt library.")
def ingest_riscv_cmd(db_path: str, data_dir: str | None, reset: bool,
                     no_seed: bool) -> None:
    """Build a standalone RISC-V ISA reference database (OpenSearch only).

    DB_PATH is an opensearch pointer file. It need not exist yet — pass
    `--backend opensearch` to synthesize one (cluster from
    $XEVDB_OPENSEARCH_HOSTS). The reference is independent of any waveform dump.
    """
    result = _backend(db_path).ingest_riscv(
        data_dir, reset=reset, seed=not no_seed)
    summary = ", ".join(f"{v} {k}" for k, v in result.items())
    click.echo(f"ingested RISC-V ISA reference into {db_path}: {summary}")


@main.command(name="ingest-kernel")
@click.argument("db_path", type=click.Path())
@click.option("--kernel-tree", type=click.Path(exists=True, file_okay=False),
              default=None,
              help="Parse this linux/ checkout instead of the bundled snapshot.")
@click.option("--data", "data_dir", type=click.Path(exists=True, file_okay=False),
              default=None, help="Directory of pre-generated kernel JSON.")
@click.option("--reset", is_flag=True,
              help="Drop existing kernel_* indices before ingesting.")
@click.option("--no-seed", is_flag=True,
              help="Skip installing the standard prompt library.")
def ingest_kernel_cmd(db_path: str, kernel_tree: str | None, data_dir: str | None,
                      reset: bool, no_seed: bool) -> None:
    """Build a RISC-V Linux kernel architecture database (OpenSearch only).

    Parses syscalls, trap causes, the SBI ABI, and the VM layout / boot ABI.
    With --kernel-tree it reads a real linux/ checkout; otherwise it uses the
    bundled snapshot. Independent of any waveform dump.
    """
    result = _backend(db_path).ingest_kernel(
        kernel_tree, data_dir=data_dir, reset=reset, seed=not no_seed)
    summary = ", ".join(f"{v} {k}" for k, v in result.items())
    click.echo(f"ingested kernel architecture into {db_path}: {summary}")


@main.command()
@click.argument("db_path", type=click.Path(exists=True, dir_okay=False))
@click.option("--filter", "filter_name", default=None,
              help="Only list modules whose name matches this LIKE pattern.")
@click.option("--limit", type=int, default=200, show_default=True)
@click.option("--json", "as_json", is_flag=True)
def modules(db_path: str, filter_name: str | None, limit: int, as_json: bool) -> None:
    """List parsed RTL modules."""
    _require_raw_sql(db_path, "modules")
    with _backend(db_path).open(read_only=True) as con:
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
    _require_raw_sql(db_path, "show")
    with _backend(db_path).open(read_only=True) as con:
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
    backend = _backend(db_path)
    with backend.open(read_only=True) as con:
        ps = backend.list_prompts(con)
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
    backend = _backend(db_path)
    with backend.open(read_only=True) as con:
        try:
            p = backend.show_prompt(con, name)
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
    if p.dsl_json:
        click.echo("dsl_json:")
        for line in p.dsl_json.splitlines():
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
    backend = _backend(db_path)
    with backend.open(read_only=False) as con:
        try:
            rows, hit = backend.run_prompt(con, name, arg_dict,
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
@click.option("--dsl-json", "dsl_json", default="",
              help="Optional backend-agnostic query template (e.g. OpenSearch DSL). "
                   "Ignored by the SQLite backend, which runs --sql.")
@click.option("--overwrite", is_flag=True)
def prompt_add(db_path: str, name: str, sql_text: str | None, from_file: str | None,
               description: str, params_json: str, dsl_json: str, overwrite: bool) -> None:
    """Store a new prompt or update an existing one."""
    if not sql_text and not from_file:
        raise click.ClickException("provide one of --sql or --from-file")
    if from_file:
        sql_text = Path(from_file).read_text()
    if dsl_json:
        try:
            json.loads(dsl_json)
        except json.JSONDecodeError as e:
            raise click.ClickException(f"--dsl-json: {e}")
    try:
        params = json.loads(params_json)
        if not isinstance(params, list):
            raise ValueError("--params-json must decode to a list")
    except (json.JSONDecodeError, ValueError) as e:
        raise click.ClickException(f"--params-json: {e}")
    backend = _backend(db_path)
    with backend.open(read_only=False) as con:
        try:
            backend.add_prompt(con, name, sql_text, description=description,
                               params=params, overwrite=overwrite, dsl_json=dsl_json)
        except Exception as e:
            raise click.ClickException(str(e))
    click.echo(f"stored prompt {name!r} in {db_path}")


@prompt.command(name="remove")
@click.argument("db_path", type=click.Path(exists=True, dir_okay=False))
@click.argument("name")
def prompt_remove(db_path: str, name: str) -> None:
    """Delete a stored prompt."""
    backend = _backend(db_path)
    with backend.open(read_only=False) as con:
        gone = backend.remove_prompt(con, name)
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
    backend = _backend(db_path)
    with backend.open(read_only=True) as con:
        info = backend.cache_stats(con)
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
    backend = _backend(db_path)
    with backend.open(read_only=True) as con:
        rows = backend.cache_list(con, prompt=prompt_name, limit=limit)
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
    backend = _backend(db_path)
    with backend.open(read_only=False) as con:
        n = backend.cache_clear(con, prompt=prompt_name)
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
    _require_raw_sql(db_path, "xz summary")
    with _backend(db_path).open(read_only=True) as con:
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
    _require_raw_sql(db_path, "xz first")
    with _backend(db_path).open(read_only=True) as con:
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
    _require_raw_sql(db_path, "xz signal")
    with _backend(db_path).open(read_only=True) as con:
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
    _require_raw_sql(db_path, "xz at")
    with _backend(db_path).open(read_only=True) as con:
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
    _require_raw_sql(db_path, "xz propagate")
    with _backend(db_path).open(read_only=True) as con:
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


# ----------------------------------------------------------------------------
# bug — bug knowledge base
# ----------------------------------------------------------------------------

@main.group()
def bug() -> None:
    """Record, search, and recall named bugs (what was wrong + the fix).

    \b
    xevdb bug add    <db> <name> [--symptom ... --fix ... --signal ...]
    xevdb bug show   <db> <name>
    xevdb bug list   <db> [--status open]
    xevdb bug search <db> <query>          full-text over symptom/root-cause/fix
    xevdb bug remove <db> <name>
    """


def _render_bug(b: _bugs.Bug) -> str:
    lines = [f"{b.name}   [{b.status}{('/' + b.severity) if b.severity else ''}]"]
    if b.title:
        lines.append(f"  title:      {b.title}")
    for label, val in (("symptom", b.symptom), ("root_cause", b.root_cause),
                       ("fix", b.fix), ("fix_ref", b.fix_ref)):
        if val:
            lines.append(f"  {label:<11s} {val}")
    if b.keywords:
        lines.append(f"  keywords:   {', '.join(b.keywords)}")
    if b.tags:
        lines.append(f"  tags:       {', '.join(b.tags)}")
    if b.links:
        lines.append("  links:")
        for lk in b.links:
            lines.append(f"    {lk.kind:<10s} {lk.value}"
                         f"{('  (' + lk.extra + ')') if lk.extra else ''}")
    return "\n".join(lines)


def _links_from_opts(signals, modules, refs) -> list[_bugs.BugLink]:
    out: list[_bugs.BugLink] = []
    out += [_bugs.BugLink("signal", v) for v in signals]
    out += [_bugs.BugLink("module", v) for v in modules]
    out += [_bugs.BugLink("ref", v) for v in refs]
    return out


@bug.command(name="add")
@click.argument("db_path", type=click.Path(exists=True, dir_okay=False))
@click.argument("name")
@click.option("--title", default="")
@click.option("--status", default="open", show_default=True,
              help="open | investigating | fixed | wontfix")
@click.option("--severity", default="", help="fatal | error | warning | info")
@click.option("--symptom", default="", help="what was wrong / how it showed up")
@click.option("--root-cause", "root_cause", default="")
@click.option("--fix", default="")
@click.option("--fix-ref", "fix_ref", default="", help="commit / PR / issue ref")
@click.option("--keyword", "keywords", multiple=True, help="searchable keyword (repeatable)")
@click.option("--tag", "tags", multiple=True, help="facet tag (repeatable)")
@click.option("--signal", "signals", multiple=True, help="associated signal fullname (repeatable)")
@click.option("--module", "modules", multiple=True, help="associated module (repeatable)")
@click.option("--ref", "refs", multiple=True, help="associated file:line (repeatable)")
@click.option("--overwrite", is_flag=True, help="update the bug if it already exists")
def bug_add(db_path: str, name: str, title: str, status: str, severity: str,
            symptom: str, root_cause: str, fix: str, fix_ref: str,
            keywords: tuple[str, ...], tags: tuple[str, ...], signals: tuple[str, ...],
            modules: tuple[str, ...], refs: tuple[str, ...], overwrite: bool) -> None:
    """Record a bug (or update it with --overwrite)."""
    backend = _backend(db_path)
    links = _links_from_opts(signals, modules, refs)
    with backend.open(read_only=False) as con:
        try:
            b = backend.add_bug(con, name, title=title, status=status,
                                severity=severity, symptom=symptom,
                                root_cause=root_cause, fix=fix, fix_ref=fix_ref,
                                keywords=list(keywords), tags=list(tags),
                                links=links, overwrite=overwrite)
        except ValueError as e:
            raise click.ClickException(str(e))
    click.echo(f"stored bug {b.name!r} in {db_path}")


@bug.command(name="show")
@click.argument("db_path", type=click.Path(exists=True, dir_okay=False))
@click.argument("name")
@click.option("--json", "as_json", is_flag=True)
def bug_show(db_path: str, name: str, as_json: bool) -> None:
    """Show one bug by name."""
    backend = _backend(db_path)
    with backend.open(read_only=True) as con:
        b = backend.get_bug(con, name)
    if b is None:
        raise click.ClickException(f"no bug named {name!r}")
    click.echo(json.dumps(b.to_dict(), indent=2) if as_json else _render_bug(b))


@bug.command(name="list")
@click.argument("db_path", type=click.Path(exists=True, dir_okay=False))
@click.option("--status", default=None)
@click.option("--severity", default=None)
@click.option("--tag", default=None)
@click.option("--limit", type=int, default=50, show_default=True)
@click.option("--json", "as_json", is_flag=True)
def bug_list(db_path: str, status: str | None, severity: str | None,
             tag: str | None, limit: int, as_json: bool) -> None:
    """List bugs (newest first) with optional facet filters."""
    backend = _backend(db_path)
    with backend.open(read_only=True) as con:
        bugs = backend.list_bugs(con, status=status, severity=severity,
                                 tag=tag, limit=limit)
    if as_json:
        click.echo(json.dumps([b.to_dict() for b in bugs], indent=2))
        return
    if not bugs:
        click.echo("(no bugs)")
        return
    for b in bugs:
        sev = f"/{b.severity}" if b.severity else ""
        click.echo(f"{b.name:<28s}[{b.status}{sev}]  {b.title}")


@bug.command(name="search")
@click.argument("db_path", type=click.Path(exists=True, dir_okay=False))
@click.argument("query")
@click.option("--status", default=None)
@click.option("--keyword", default=None)
@click.option("--limit", type=int, default=50, show_default=True)
@click.option("--json", "as_json", is_flag=True)
def bug_search(db_path: str, query: str, status: str | None, keyword: str | None,
               limit: int, as_json: bool) -> None:
    """Full-text search bugs (FTS5 when available, else LIKE)."""
    backend = _backend(db_path)
    with backend.open(read_only=True) as con:
        bugs = backend.search_bugs(con, query, status=status,
                                   keyword=keyword, limit=limit)
    if as_json:
        click.echo(json.dumps([b.to_dict() for b in bugs], indent=2))
        return
    if not bugs:
        click.echo(f"no bugs matching {query!r}")
        return
    for b in bugs:
        sev = f"/{b.severity}" if b.severity else ""
        click.echo(f"{b.name:<28s}[{b.status}{sev}]  {b.title}")


@bug.command(name="link")
@click.argument("db_path", type=click.Path(exists=True, dir_okay=False))
@click.argument("name")
@click.option("--signal", "signal", default=None)
@click.option("--module", "module", default=None)
@click.option("--ref", "ref", default=None, help="file:line")
@click.option("--event", "event", default=None)
@click.option("--assertion", "assertion", default=None)
@click.option("--txn", "txn", default=None)
@click.option("--coverage", "coverage", default=None)
@click.option("--extra", default="", help="optional note stored with the link")
def bug_link(db_path: str, name: str, signal: str | None, module: str | None,
             ref: str | None, event: str | None, assertion: str | None,
             txn: str | None, coverage: str | None, extra: str) -> None:
    """Attach an association (signal/module/ref/event/assertion/txn/coverage) to a bug."""
    pairs = [(k, v) for k, v in (
        ("signal", signal), ("module", module), ("ref", ref), ("event", event),
        ("assertion", assertion), ("txn", txn), ("coverage", coverage)) if v]
    if len(pairs) != 1:
        raise click.ClickException("provide exactly one of "
                                   "--signal/--module/--ref/--event/--assertion/--txn/--coverage")
    kind, value = pairs[0]
    backend = _backend(db_path)
    with backend.open(read_only=False) as con:
        try:
            backend.link_bug(con, name, kind, value, extra)
        except ValueError as e:
            raise click.ClickException(str(e))
    click.echo(f"linked {kind}={value!r} to bug {name!r}")


@bug.command(name="close")
@click.argument("db_path", type=click.Path(exists=True, dir_okay=False))
@click.argument("name")
@click.option("--status", default="fixed", show_default=True,
              help="resolution status (fixed | wontfix | ...)")
@click.option("--fix", default=None, help="how it was fixed")
@click.option("--fix-ref", "fix_ref", default=None, help="commit / PR / issue ref")
def bug_close(db_path: str, name: str, status: str, fix: str | None,
              fix_ref: str | None) -> None:
    """Resolve a bug (set status, optionally fix/fix-ref)."""
    backend = _backend(db_path)
    with backend.open(read_only=False) as con:
        try:
            b = backend.close_bug(con, name, status=status, fix=fix, fix_ref=fix_ref)
        except ValueError as e:
            raise click.ClickException(str(e))
    click.echo(f"bug {b.name!r} -> {b.status}")


@bug.command(name="remove")
@click.argument("db_path", type=click.Path(exists=True, dir_okay=False))
@click.argument("name")
def bug_remove(db_path: str, name: str) -> None:
    """Delete a bug by name."""
    backend = _backend(db_path)
    with backend.open(read_only=False) as con:
        gone = backend.remove_bug(con, name)
    click.echo(f"removed {name!r}" if gone else f"no bug named {name!r}")


if __name__ == "__main__":
    main()
