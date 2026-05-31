"""SQLite backend — the default, and the reference implementation.

This is a thin adapter: every method delegates to the existing `db`,
`prompts`, and `cache` modules, which remain the SQLite implementation and the
direct API used by the test suite. The adapter exists so `cli.py` can select a
backend uniformly; it adds no behaviour of its own.
"""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import sqlite3

from .base import Backend
from .. import db as _db
from .. import prompts as _prompts
from .. import cache as _cache
from .. import bugs as _bugs


class SqliteBackend(Backend):
    """Single self-contained `.xevdb` SQLite file."""

    name = "sqlite"
    supports_raw_sql = True

    def build(self, vcd_path: str | Path, *, reset: bool = False,
              seed: bool = True) -> dict[str, int]:
        return _db.build(vcd_path, self.db_path, reset=reset, seed=seed)

    def ingest_rtl(self, rtl_path: str | Path, *,
                   reset: bool = False) -> dict[str, int]:
        return _db.ingest_rtl(rtl_path, self.db_path, reset=reset)

    def ingest_sim(self, log_path: str | Path, *, name: str | None = None,
                   keep_all: bool = False, reset: bool = False) -> dict[str, int]:
        return _db.ingest_sim(log_path, self.db_path, name=name,
                              keep_all=keep_all, reset=reset)

    @contextmanager
    def open(self, *, read_only: bool = False) -> Iterator[sqlite3.Connection]:
        with _db.open_db(self.db_path, read_only=read_only) as con:
            yield con

    def stats(self, session: sqlite3.Connection) -> dict[str, Any]:
        return _db.stats(session)

    def run_prompt(self, session: sqlite3.Connection, name: str,
                   args: dict[str, Any] | None = None, *, use_cache: bool = True,
                   ttl_seconds: int = 0) -> tuple[list[dict], bool]:
        return _prompts.run_prompt(session, name, args,
                                   use_cache=use_cache, ttl_seconds=ttl_seconds)

    # -- waveform queries ---------------------------------------------------

    def resolve_signal(self, session: sqlite3.Connection, query: str) -> Any | None:
        return _db.resolve_signal(session, query)

    def value_at(self, session: sqlite3.Connection, sig_id: str,
                 t: int) -> tuple[int, str] | None:
        return _db.value_at(session, sig_id, t)

    def window(self, session: sqlite3.Connection, sig_id: str, t0: int | None,
               t1: int | None, limit: int = 200) -> list[tuple[int, str]]:
        return _db.window(session, sig_id, t0, t1, limit)

    def find_signals(self, session: sqlite3.Connection, pattern: str,
                     limit: int = 50) -> list[Any]:
        return _db.find_signals(session, pattern, limit)

    # -- prompt library CRUD ------------------------------------------------

    def list_prompts(self, session: sqlite3.Connection) -> list[Any]:
        return _prompts.list_prompts(session)

    def show_prompt(self, session: sqlite3.Connection, name: str) -> Any:
        return _prompts.show_prompt(session, name)

    def add_prompt(self, session: sqlite3.Connection, name: str, sql: str, *,
                   description: str = "", params: list[dict] | None = None,
                   overwrite: bool = False, dsl_json: str = "") -> None:
        _prompts.add_prompt(session, name, sql, description=description,
                            params=params, overwrite=overwrite, dsl_json=dsl_json)

    def remove_prompt(self, session: sqlite3.Connection, name: str) -> bool:
        return _prompts.remove_prompt(session, name)

    # -- result cache -------------------------------------------------------

    def cache_stats(self, session: sqlite3.Connection) -> dict[str, Any]:
        return _cache.stats(session)

    def cache_list(self, session: sqlite3.Connection, prompt: str | None = None,
                   limit: int = 50) -> list[dict]:
        return _cache.list_entries(session, prompt=prompt, limit=limit)

    def cache_clear(self, session: sqlite3.Connection, prompt: str | None = None) -> int:
        return _cache.clear(session, prompt=prompt)

    # -- bug knowledge base -------------------------------------------------

    def add_bug(self, session: sqlite3.Connection, name: str, *, title: str = "",
                status: str = "open", severity: str = "", symptom: str = "",
                root_cause: str = "", fix: str = "", fix_ref: str = "",
                keywords: list[str] | None = None, tags: list[str] | None = None,
                links: list[Any] | None = None, overwrite: bool = False) -> Any:
        return _bugs.add_bug(
            session, name, title=title, status=status, severity=severity,
            symptom=symptom, root_cause=root_cause, fix=fix, fix_ref=fix_ref,
            keywords=keywords, tags=tags, links=links, overwrite=overwrite)

    def get_bug(self, session: sqlite3.Connection, name: str) -> Any | None:
        return _bugs.get_bug(session, name)

    def list_bugs(self, session: sqlite3.Connection, *, status: str | None = None,
                  severity: str | None = None, tag: str | None = None,
                  limit: int = 50) -> list[Any]:
        return _bugs.list_bugs(session, status=status, severity=severity,
                               tag=tag, limit=limit)

    def remove_bug(self, session: sqlite3.Connection, name: str) -> bool:
        return _bugs.remove_bug(session, name)

    def search_bugs(self, session: sqlite3.Connection, query: str, *,
                    status: str | None = None, keyword: str | None = None,
                    limit: int = 50) -> list[Any]:
        return _bugs.search_bugs(session, query, status=status,
                                 keyword=keyword, limit=limit)

    def link_bug(self, session: sqlite3.Connection, name: str, kind: str,
                 value: str, extra: str = "") -> Any:
        return _bugs.link_bug(session, name, kind, value, extra)

    def close_bug(self, session: sqlite3.Connection, name: str, *,
                  status: str = "fixed", fix: str | None = None,
                  fix_ref: str | None = None) -> Any:
        return _bugs.close_bug(session, name, status=status, fix=fix, fix_ref=fix_ref)
