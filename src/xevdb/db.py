"""SQLite-backed VCD + SV database (.xevdb file).

Tables (all in one file):

  -- waveform side -------------------------------------------------------
  signals(id, hier, name, fullname, width, kind)
  changes(sig_id, t, value)
  meta(key, value)

  -- RTL side -----------------------------------------------------------
  source_files(path PK, content, hash, ingested_at)        -- full source text
  modules(id PK, name, kind, file, line_start, line_end,
          leading_comment, body_summary, params_json, ast_json,
          ingested_at)
  module_ports(module_id, position, name, direction, width, kind)
  module_signals(module_id, name, kind, line, width, decl_text)
  module_instances(parent_module_id, child_module_name, instance_name, line)

  -- prompts + cache ----------------------------------------------------
  prompts(name PK, description, sql, params_json, created_at, updated_at)
  cache(key PK, prompt_name, args_json, result_json, created_at,
        hits, last_hit_at, ttl_seconds)
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Sequence

from .parser import VCD, bare_name, parse_file
from . import seed_prompts
from . import sv as _sv
from . import sim as _sim
from . import xtrace as _xtrace


# ----------------------------------------------------------------------------
# Schema
# ----------------------------------------------------------------------------

_SQLITE_SCHEMA = [
    # waveform
    """
    CREATE TABLE IF NOT EXISTS signals (
        id       TEXT PRIMARY KEY,
        hier     TEXT NOT NULL,
        name     TEXT NOT NULL,
        fullname TEXT NOT NULL,
        width    INTEGER NOT NULL,
        kind     TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS changes (
        sig_id TEXT NOT NULL,
        t      INTEGER NOT NULL,
        value  TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS meta (
        key TEXT PRIMARY KEY, value TEXT
    )
    """,
    # RTL
    """
    CREATE TABLE IF NOT EXISTS source_files (
        path        TEXT PRIMARY KEY,
        content     TEXT NOT NULL,
        hash        TEXT NOT NULL,
        ingested_at REAL NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS modules (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        name            TEXT NOT NULL,
        kind            TEXT NOT NULL,
        file            TEXT NOT NULL,
        line_start      INTEGER NOT NULL,
        line_end        INTEGER NOT NULL,
        leading_comment TEXT NOT NULL DEFAULT '',
        body_summary    TEXT NOT NULL DEFAULT '',
        params_json     TEXT NOT NULL DEFAULT '[]',
        ast_json        TEXT NOT NULL DEFAULT '{}',
        ingested_at     REAL NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS module_ports (
        module_id INTEGER NOT NULL,
        position  INTEGER NOT NULL,
        name      TEXT NOT NULL,
        direction TEXT NOT NULL DEFAULT '',
        width     TEXT NOT NULL DEFAULT '',
        kind      TEXT NOT NULL DEFAULT ''
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS module_signals (
        module_id INTEGER NOT NULL,
        name      TEXT NOT NULL,
        kind      TEXT NOT NULL,
        line      INTEGER NOT NULL,
        width     TEXT NOT NULL DEFAULT '',
        decl_text TEXT NOT NULL DEFAULT ''
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS module_instances (
        parent_module_id    INTEGER NOT NULL,
        child_module_name   TEXT NOT NULL,
        instance_name       TEXT NOT NULL,
        line                INTEGER NOT NULL
    )
    """,
    # simulator output
    """
    CREATE TABLE IF NOT EXISTS sim_runs (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        name            TEXT NOT NULL,
        source          TEXT NOT NULL,
        ingested_at     REAL NOT NULL,
        line_count      INTEGER NOT NULL,
        n_events        INTEGER NOT NULL,
        n_fatal         INTEGER NOT NULL DEFAULT 0,
        n_error         INTEGER NOT NULL DEFAULT 0,
        n_warning       INTEGER NOT NULL DEFAULT 0,
        severity_json   TEXT NOT NULL DEFAULT '{}'
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS sim_events (
        id        INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id    INTEGER NOT NULL,
        line_no   INTEGER NOT NULL,
        severity  TEXT NOT NULL,
        t         INTEGER,
        ref_file  TEXT NOT NULL DEFAULT '',
        ref_line  INTEGER NOT NULL DEFAULT 0,
        message   TEXT NOT NULL
    )
    """,
    # bug knowledge base
    """
    CREATE TABLE IF NOT EXISTS bugs (
        name          TEXT PRIMARY KEY,
        title         TEXT NOT NULL DEFAULT '',
        status        TEXT NOT NULL DEFAULT 'open',
        severity      TEXT NOT NULL DEFAULT '',
        symptom       TEXT NOT NULL DEFAULT '',
        root_cause    TEXT NOT NULL DEFAULT '',
        fix           TEXT NOT NULL DEFAULT '',
        fix_ref       TEXT NOT NULL DEFAULT '',
        keywords_json TEXT NOT NULL DEFAULT '[]',
        tags_json     TEXT NOT NULL DEFAULT '[]',
        created_at    REAL NOT NULL,
        updated_at    REAL NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS bug_links (
        bug_name TEXT NOT NULL,
        kind     TEXT NOT NULL,
        value    TEXT NOT NULL,
        extra    TEXT NOT NULL DEFAULT ''
    )
    """,
    # prompts + cache
    """
    CREATE TABLE IF NOT EXISTS prompts (
        name        TEXT PRIMARY KEY,
        description TEXT NOT NULL DEFAULT '',
        sql         TEXT NOT NULL,
        dsl_json    TEXT NOT NULL DEFAULT '',
        params_json TEXT NOT NULL DEFAULT '[]',
        created_at  REAL NOT NULL,
        updated_at  REAL NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS cache (
        key         TEXT PRIMARY KEY,
        prompt_name TEXT NOT NULL,
        args_json   TEXT NOT NULL,
        result_json TEXT NOT NULL,
        created_at  REAL NOT NULL,
        hits        INTEGER NOT NULL DEFAULT 0,
        last_hit_at REAL,
        ttl_seconds INTEGER NOT NULL DEFAULT 0
    )
    """,
    # indices
    "CREATE INDEX IF NOT EXISTS idx_changes_sig_t    ON changes(sig_id, t)",
    "CREATE INDEX IF NOT EXISTS idx_signals_full     ON signals(fullname)",
    "CREATE INDEX IF NOT EXISTS idx_signals_name     ON signals(name)",
    "CREATE INDEX IF NOT EXISTS idx_modules_name     ON modules(name)",
    "CREATE INDEX IF NOT EXISTS idx_modules_file     ON modules(file)",
    "CREATE INDEX IF NOT EXISTS idx_mod_ports        ON module_ports(module_id)",
    "CREATE INDEX IF NOT EXISTS idx_mod_signals_mod  ON module_signals(module_id)",
    "CREATE INDEX IF NOT EXISTS idx_mod_signals_name ON module_signals(name)",
    "CREATE INDEX IF NOT EXISTS idx_mod_inst_parent  ON module_instances(parent_module_id)",
    "CREATE INDEX IF NOT EXISTS idx_mod_inst_child   ON module_instances(child_module_name)",
    "CREATE INDEX IF NOT EXISTS idx_sim_events_run   ON sim_events(run_id)",
    "CREATE INDEX IF NOT EXISTS idx_sim_events_sev   ON sim_events(severity)",
    "CREATE INDEX IF NOT EXISTS idx_sim_events_t     ON sim_events(t)",
    "CREATE INDEX IF NOT EXISTS idx_sim_events_ref   ON sim_events(ref_file)",
    "CREATE INDEX IF NOT EXISTS idx_cache_prompt     ON cache(prompt_name)",
    "CREATE INDEX IF NOT EXISTS idx_bugs_status      ON bugs(status)",
    "CREATE INDEX IF NOT EXISTS idx_bugs_severity    ON bugs(severity)",
    "CREATE INDEX IF NOT EXISTS idx_bug_links_name   ON bug_links(bug_name)",
    "CREATE INDEX IF NOT EXISTS idx_bug_links_kv     ON bug_links(kind, value)",
]

# Columns indexed by the bug full-text-search virtual table, in order. `name`
# is UNINDEXED (stored, returned, filterable — but not tokenized/searched).
BUG_FTS_COLUMNS = ("name", "title", "symptom", "root_cause", "fix", "keywords", "tags")


def _ensure_bug_fts(con: sqlite3.Connection) -> bool:
    """Create the bug FTS5 table if this SQLite was built with FTS5.

    Returns True if the table exists afterwards. When FTS5 is unavailable the
    bug search path falls back to LIKE, so this is best-effort.
    """
    try:
        con.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS bugs_fts USING fts5("
            "name UNINDEXED, title, symptom, root_cause, fix, keywords, tags)"
        )
        return True
    except sqlite3.OperationalError:
        return False  # FTS5 not compiled in


def bug_fts_available(con: sqlite3.Connection) -> bool:
    row = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='bugs_fts'"
    ).fetchone()
    return row is not None


# Additive, idempotent column migrations for .xevdb files created by an older
# xevdb.  Each entry is (table, column, "<type> <constraints>").  Applied only
# when the column is absent, so re-running is a no-op.
_COLUMN_MIGRATIONS = [
    ("prompts", "dsl_json", "TEXT NOT NULL DEFAULT ''"),
]


def _migrate(con: sqlite3.Connection) -> None:
    for table, column, decl in _COLUMN_MIGRATIONS:
        cols = {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
        if column not in cols:
            con.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def _ensure_schema(con: sqlite3.Connection) -> None:
    for stmt in _SQLITE_SCHEMA:
        con.execute(stmt)
    _ensure_bug_fts(con)
    _migrate(con)


def _fullname(hier: str, name: str) -> str:
    bare = bare_name(name)
    return f"{hier}.{bare}" if hier else bare


# ----------------------------------------------------------------------------
# Build (VCD)
# ----------------------------------------------------------------------------

def build(
    vcd_path: str | Path,
    db_path: str | Path,
    reset: bool = False,
    seed: bool = True,
) -> dict[str, int]:
    """Parse a VCD into a fresh .xevdb file.

    Always seeds the standard prompt library unless `seed=False`.
    """
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if reset and db_path.exists():
        db_path.unlink()

    return build_vcd(parse_file(vcd_path), db_path, source_path=vcd_path, reset=reset, seed=seed)


def build_xtrace(
    xtrace_path: str | Path,
    db_path: str | Path,
    reset: bool = False,
    seed: bool = True,
) -> dict[str, int]:
    """Parse an XTrace capture into a fresh .xevdb file."""
    return build_vcd(
        _xtrace.parse_file(xtrace_path),
        db_path,
        source_path=xtrace_path,
        reset=reset,
        seed=seed,
    )


def build_vcd(
    vcd: VCD,
    db_path: str | Path,
    *,
    source_path: str | Path,
    reset: bool = False,
    seed: bool = True,
) -> dict[str, int]:
    """Write a parsed waveform object into a .xevdb file."""
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if reset and db_path.exists():
        db_path.unlink()

    con = sqlite3.connect(db_path)
    try:
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA synchronous=NORMAL")
        _ensure_schema(con)

        if vcd.signals:
            con.executemany(
                "INSERT OR REPLACE INTO signals (id, hier, name, fullname, width, kind) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                [
                    (s.id, s.hier, s.name, _fullname(s.hier, s.name), s.width, s.kind)
                    for s in vcd.signals.values()
                ],
            )

        if vcd.changes:
            con.executemany(
                "INSERT INTO changes (sig_id, t, value) VALUES (?, ?, ?)",
                [(c.sig_id, c.t, c.value) for c in vcd.changes],
            )

        t_min = min((c.t for c in vcd.changes), default=0)
        t_max = max((c.t for c in vcd.changes), default=0)
        meta_rows = [
            ("source", str(source_path)),
            ("date", vcd.date),
            ("version", vcd.version),
            ("timescale", vcd.timescale),
            ("t_min", str(t_min)),
            ("t_max", str(t_max)),
            ("n_signals", str(len(vcd.signals))),
            ("n_changes", str(len(vcd.changes))),
            ("xevdb_version", "0.1.0"),
        ]
        con.executemany("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", meta_rows)

        if seed:
            seed_prompts.install(con)

        con.commit()
        return {
            "signals": len(vcd.signals),
            "changes": len(vcd.changes),
            "t_min": t_min,
            "t_max": t_max,
        }
    finally:
        con.close()


# ----------------------------------------------------------------------------
# Ingest RTL into an existing .xevdb
# ----------------------------------------------------------------------------

def ingest_rtl(
    rtl_path: str | Path,
    db_path: str | Path,
    reset: bool = False,
) -> dict[str, int]:
    """Walk a file or directory of .v/.sv and insert into the modules tables.

    If `reset=True`, all existing module / source / port / signal / instance
    rows are dropped before ingest.
    """
    db_path = Path(db_path)
    if not db_path.exists():
        raise FileNotFoundError(
            f"{db_path} does not exist — run `xevdb build <vcd> --db {db_path}` first "
            "to create the database (or pass --create to xevdb ingest-rtl)."
        )

    con = sqlite3.connect(db_path)
    try:
        con.execute("PRAGMA journal_mode=WAL")
        _ensure_schema(con)

        if reset:
            con.execute("DELETE FROM module_instances")
            con.execute("DELETE FROM module_signals")
            con.execute("DELETE FROM module_ports")
            con.execute("DELETE FROM modules")
            con.execute("DELETE FROM source_files")

        n_files = 0
        n_modules = 0
        n_ports = 0
        n_signals = 0
        n_instances = 0
        now = time.time()

        for path, modules, raw_src in _sv.walk_rtl(rtl_path):
            n_files += 1
            h = hashlib.sha256(raw_src.encode("utf-8")).hexdigest()
            con.execute(
                "INSERT OR REPLACE INTO source_files (path, content, hash, ingested_at) "
                "VALUES (?, ?, ?, ?)",
                (str(path), raw_src, h, now),
            )
            for m in modules:
                cur = con.execute(
                    "INSERT INTO modules (name, kind, file, line_start, line_end, "
                    "leading_comment, body_summary, params_json, ast_json, ingested_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (m.name, m.kind, str(path), m.line_start, m.line_end,
                     m.leading_comment, m.body_summary,
                     json.dumps(m.parameters), json.dumps(m.ast), now),
                )
                mid = cur.lastrowid
                n_modules += 1
                for i, p in enumerate(m.ports):
                    con.execute(
                        "INSERT INTO module_ports (module_id, position, name, "
                        "direction, width, kind) VALUES (?, ?, ?, ?, ?, ?)",
                        (mid, i, p.name, p.direction, p.width, p.kind),
                    )
                    n_ports += 1
                for s in m.signals:
                    con.execute(
                        "INSERT INTO module_signals (module_id, name, kind, line, "
                        "width, decl_text) VALUES (?, ?, ?, ?, ?, ?)",
                        (mid, s.name, s.kind, s.line, s.width, s.decl_text),
                    )
                    n_signals += 1
                for inst in m.instances:
                    con.execute(
                        "INSERT INTO module_instances (parent_module_id, "
                        "child_module_name, instance_name, line) "
                        "VALUES (?, ?, ?, ?)",
                        (mid, inst.module_name, inst.instance_name, inst.line),
                    )
                    n_instances += 1

        con.commit()
        return {
            "files": n_files, "modules": n_modules, "ports": n_ports,
            "signals": n_signals, "instances": n_instances,
        }
    finally:
        con.close()


# ----------------------------------------------------------------------------
# Query helpers (waveform side)
# ----------------------------------------------------------------------------

@dataclass
class ResolvedSignal:
    sig_id: str
    hier: str
    name: str
    fullname: str
    width: int
    kind: str


@contextmanager
def open_db(db_path: str | Path, read_only: bool = False) -> Iterator[sqlite3.Connection]:
    path = Path(db_path)
    if read_only:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    else:
        con = sqlite3.connect(path)
    try:
        con.row_factory = sqlite3.Row
        yield con
    finally:
        con.close()


def _exec(con, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
    return list(con.execute(sql, tuple(params)).fetchall())


def resolve_signal(con, query: str) -> ResolvedSignal | None:
    cols = "id, hier, name, fullname, width, kind"
    rows = _exec(con, f"SELECT {cols} FROM signals WHERE id = ?", [query])
    if rows:
        return ResolvedSignal(*rows[0])
    rows = _exec(con, f"SELECT {cols} FROM signals WHERE fullname = ?", [query])
    if rows:
        return ResolvedSignal(*rows[0])
    bare = query.rsplit(".", 1)[-1]
    rows = _exec(
        con, f"SELECT {cols} FROM signals WHERE name = ? OR name LIKE ?",
        [bare, f"{bare}[%"],
    )
    if len(rows) == 1:
        return ResolvedSignal(*rows[0])
    rows = _exec(
        con, f"SELECT {cols} FROM signals WHERE fullname LIKE ?", [f"%.{query}"],
    )
    if len(rows) == 1:
        return ResolvedSignal(*rows[0])
    return None


def value_at(con, sig_id: str, t: int) -> tuple[int, str] | None:
    rows = _exec(
        con,
        "SELECT t, value FROM changes WHERE sig_id = ? AND t <= ? ORDER BY t DESC LIMIT 1",
        [sig_id, t],
    )
    return (rows[0]["t"], rows[0]["value"]) if rows else None


def window(con, sig_id: str, t0: int | None, t1: int | None,
           limit: int = 200) -> list[tuple[int, str]]:
    if t0 is None and t1 is None:
        rows = _exec(
            con,
            "SELECT t, value FROM changes WHERE sig_id = ? ORDER BY t LIMIT ?",
            [sig_id, limit],
        )
    else:
        t0 = t0 if t0 is not None else 0
        t1 = t1 if t1 is not None else 2**62
        rows = _exec(
            con,
            "SELECT t, value FROM changes WHERE sig_id = ? AND t BETWEEN ? AND ? "
            "ORDER BY t LIMIT ?",
            [sig_id, t0, t1, limit],
        )
    return [(r["t"], r["value"]) for r in rows]


def find_signals(con, pattern: str, limit: int = 50) -> list[ResolvedSignal]:
    if any(ch in pattern for ch in "*?[]"):
        like = pattern.replace("*", "%").replace("?", "_")
    else:
        like = f"%{pattern}%"
    rows = _exec(
        con,
        "SELECT id, hier, name, fullname, width, kind FROM signals "
        "WHERE fullname LIKE ? OR name LIKE ? ORDER BY fullname LIMIT ?",
        [like, like, limit],
    )
    return [ResolvedSignal(*r) for r in rows]


def stats(con) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for row in _exec(con, "SELECT key, value FROM meta"):
        out[row["key"]] = row["value"]
    out["row_counts"] = {
        "signals":          _exec(con, "SELECT COUNT(*) AS n FROM signals")[0]["n"],
        "changes":          _exec(con, "SELECT COUNT(*) AS n FROM changes")[0]["n"],
        "source_files":     _exec(con, "SELECT COUNT(*) AS n FROM source_files")[0]["n"],
        "modules":          _exec(con, "SELECT COUNT(*) AS n FROM modules")[0]["n"],
        "module_ports":     _exec(con, "SELECT COUNT(*) AS n FROM module_ports")[0]["n"],
        "module_signals":   _exec(con, "SELECT COUNT(*) AS n FROM module_signals")[0]["n"],
        "module_instances": _exec(con, "SELECT COUNT(*) AS n FROM module_instances")[0]["n"],
        "sim_runs":         _exec(con, "SELECT COUNT(*) AS n FROM sim_runs")[0]["n"],
        "sim_events":       _exec(con, "SELECT COUNT(*) AS n FROM sim_events")[0]["n"],
        "bugs":             _exec(con, "SELECT COUNT(*) AS n FROM bugs")[0]["n"],
        "bug_links":        _exec(con, "SELECT COUNT(*) AS n FROM bug_links")[0]["n"],
        "prompts":          _exec(con, "SELECT COUNT(*) AS n FROM prompts")[0]["n"],
        "cache":            _exec(con, "SELECT COUNT(*) AS n FROM cache")[0]["n"],
    }
    return out


# ----------------------------------------------------------------------------
# Ingest sim log
# ----------------------------------------------------------------------------

def ingest_sim(
    log_path: str | Path,
    db_path: str | Path,
    name: str | None = None,
    keep_all: bool = False,
    reset: bool = False,
) -> dict[str, int]:
    """Parse a simulator log and insert one row into `sim_runs` + one row per
    matched line into `sim_events`.

    Args:
        log_path: path to the log file.
        db_path: existing .xevdb to write into.
        name: short identifier for this run; defaults to the file basename.
        keep_all: if True, keep every non-blank line (severity='INFO' for
                  lines that didn't match a severity pattern).
        reset: if True, drop every sim_runs/sim_events row before ingesting.
    """
    db_path = Path(db_path)
    if not db_path.exists():
        raise FileNotFoundError(
            f"{db_path} does not exist — run `xevdb build <vcd> --db {db_path}` first."
        )

    events = _sim.parse_file(log_path, keep_all=keep_all)
    counts = _sim.severity_counts(events)
    raw_lines = Path(log_path).read_text(encoding="utf-8", errors="replace").splitlines()
    line_count = len(raw_lines)

    run_name = name or Path(log_path).name
    now = time.time()

    con = sqlite3.connect(db_path)
    try:
        _ensure_schema(con)
        if reset:
            con.execute("DELETE FROM sim_events")
            con.execute("DELETE FROM sim_runs")
        cur = con.execute(
            "INSERT INTO sim_runs (name, source, ingested_at, line_count, n_events, "
            "n_fatal, n_error, n_warning, severity_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                run_name, str(log_path), now, line_count, len(events),
                sum(counts.get(s, 0) for s in ("FATAL", "UVM_FATAL")),
                sum(counts.get(s, 0) for s in ("ERROR", "UVM_ERROR", "ASSERTION")),
                sum(counts.get(s, 0) for s in ("WARNING", "UVM_WARNING")),
                json.dumps(counts),
            ),
        )
        run_id = cur.lastrowid
        if events:
            con.executemany(
                "INSERT INTO sim_events (run_id, line_no, severity, t, "
                "ref_file, ref_line, message) VALUES (?, ?, ?, ?, ?, ?, ?)",
                [(run_id, e.line_no, e.severity, e.t, e.ref_file, e.ref_line, e.message)
                 for e in events],
            )
        con.commit()
    finally:
        con.close()

    return {
        "run_id": run_id,
        "name": run_name,
        "line_count": line_count,
        "events": len(events),
        "fatal": sum(counts.get(s, 0) for s in ("FATAL", "UVM_FATAL")),
        "error": sum(counts.get(s, 0) for s in ("ERROR", "UVM_ERROR", "ASSERTION")),
        "warning": sum(counts.get(s, 0) for s in ("WARNING", "UVM_WARNING")),
    }
