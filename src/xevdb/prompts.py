"""Named query prompts stored in the .vcdb file.

A prompt is a parameterized SQL template with documented parameters. Running
a prompt substitutes the parameters and returns the rows, caching the result
keyed by (prompt_name, args).

The seed library is installed automatically by `vcdb build`; users may add,
edit, or remove prompts via `add_prompt` / `remove_prompt` (or the CLI).
"""
from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from typing import Any

from . import cache as _cache


@dataclass
class Prompt:
    name: str
    description: str
    sql: str
    params: list[dict]    # [{name, default, type, description}]
    created_at: float
    updated_at: float
    dsl_json: str = ""    # backend-agnostic query template (e.g. OpenSearch DSL)

    def param_defaults(self) -> dict[str, Any]:
        return {p["name"]: p.get("default") for p in self.params}

    @property
    def dsl(self) -> Any | None:
        """Parsed `dsl_json`, or None when the prompt is SQL-only."""
        return json.loads(self.dsl_json) if self.dsl_json else None


# ----------------------------------------------------------------------------
# CRUD
# ----------------------------------------------------------------------------

def _row_to_prompt(row: sqlite3.Row) -> Prompt:
    keys = row.keys()
    return Prompt(
        name=row["name"],
        description=row["description"],
        sql=row["sql"],
        params=json.loads(row["params_json"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        dsl_json=row["dsl_json"] if "dsl_json" in keys else "",
    )


def list_prompts(con: sqlite3.Connection) -> list[Prompt]:
    return [_row_to_prompt(r) for r in con.execute("SELECT * FROM prompts ORDER BY name")]


def show_prompt(con: sqlite3.Connection, name: str) -> Prompt:
    row = con.execute("SELECT * FROM prompts WHERE name = ?", (name,)).fetchone()
    if row is None:
        raise KeyError(f"no prompt named {name!r}")
    return _row_to_prompt(row)


def add_prompt(
    con: sqlite3.Connection,
    name: str,
    sql: str,
    description: str = "",
    params: list[dict] | None = None,
    overwrite: bool = False,
    dsl_json: str = "",
) -> None:
    """Insert or replace a prompt.

    `dsl_json` is an optional backend-agnostic query template (e.g. an
    OpenSearch query DSL body). Relational backends ignore it and run `sql`;
    document-store backends run `dsl_json` instead. Either may be empty.
    """
    now = time.time()
    params_json = json.dumps(params or [])
    if overwrite:
        con.execute(
            "INSERT OR REPLACE INTO prompts "
            "(name, description, sql, dsl_json, params_json, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, COALESCE("
            "  (SELECT created_at FROM prompts WHERE name = ?), ?"
            "), ?)",
            (name, description, sql, dsl_json, params_json, name, now, now),
        )
    else:
        con.execute(
            "INSERT INTO prompts "
            "(name, description, sql, dsl_json, params_json, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (name, description, sql, dsl_json, params_json, now, now),
        )
    con.commit()


def remove_prompt(con: sqlite3.Connection, name: str) -> bool:
    cur = con.execute("DELETE FROM prompts WHERE name = ?", (name,))
    con.commit()
    return cur.rowcount > 0


# ----------------------------------------------------------------------------
# Run
# ----------------------------------------------------------------------------

def _coerce_args(prompt: Prompt, raw: dict[str, Any]) -> dict[str, Any]:
    """Merge defaults with user-supplied args and coerce to declared types."""
    out = prompt.param_defaults()
    out.update(raw)
    for spec in prompt.params:
        n = spec["name"]
        ty = spec.get("type", "str")
        if n in out and out[n] is not None:
            v = out[n]
            if isinstance(v, str):
                if ty == "int":
                    try:
                        out[n] = int(v)
                    except ValueError:
                        raise ValueError(f"arg {n!r} expects int, got {v!r}")
                elif ty == "float":
                    out[n] = float(v)
                # str: leave alone
    return out


def run_prompt(
    con: sqlite3.Connection,
    name: str,
    args: dict[str, Any] | None = None,
    use_cache: bool = True,
    ttl_seconds: int = 0,
) -> tuple[list[dict], bool]:
    """Run a prompt. Returns (rows, cache_hit).

    Each row is a dict of {column_name: value}.
    """
    prompt = show_prompt(con, name)
    args_resolved = _coerce_args(prompt, args or {})

    if use_cache:
        cached = _cache.get(con, name, args_resolved)
        if cached is not None:
            return cached, True

    cur = con.execute(prompt.sql, args_resolved)
    cols = [d[0] for d in cur.description]
    rows = [dict(zip(cols, r)) for r in cur.fetchall()]

    if use_cache:
        _cache.put(con, name, args_resolved, rows, ttl_seconds=ttl_seconds)

    return rows, False
