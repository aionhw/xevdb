"""The storage-backend contract for a xevdb dataset.

A *backend* owns one dataset (today: one `.xevdb` SQLite file; later: a set of
OpenSearch indices) and knows how to build it from a VCD, ingest RTL/sim into
it, hand out a read/write *session*, and run stored prompts against it.

`cli.py` talks to a `Backend` instead of importing `db`/`prompts`/`cache`
directly, so a second backend can be dropped in without touching the command
layer. The default `sqlite` backend is a thin adapter over the existing
modules — behaviour is byte-for-byte what it was before the abstraction.

Design notes for future backends (e.g. OpenSearch):

* `open()` yields an opaque *session* the backend produced itself. For a
  relational backend (`supports_raw_sql=True`) the session IS a DB-API
  connection, so SQL-only consumers (`show`, `xztrace`, the raw-SQL CLI
  commands) keep working by using it directly. A document-store backend yields
  its own client handle and must set `supports_raw_sql=False`; those SQL-only
  features stay backend-gated until ported.
* Stored prompts carry both a `sql` and an optional `dsl_json` template (see
  `prompts.Prompt`). `run_prompt` is the dispatch point: SQL backends run
  `sql`; document-store backends run `dsl_json`. Keeping `run_prompt` on the
  backend is why the prompt surface is backend-agnostic.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any


class Backend(ABC):
    """Abstract storage backend for a single xevdb dataset."""

    #: short stable identifier, used by the backend registry and `--backend`.
    name: str = "abstract"
    #: True when `open()` yields a DB-API connection usable for raw SQL.
    supports_raw_sql: bool = False

    def __init__(self, db_path: str | Path) -> None:
        #: dataset locator — a filesystem path for SQLite, a pointer/URI later.
        self.db_path = Path(db_path)

    # -- lifecycle / write --------------------------------------------------

    @abstractmethod
    def build(self, vcd_path: str | Path, *, reset: bool = False,
              seed: bool = True) -> dict[str, int]:
        """Parse a VCD into a fresh dataset. Returns ingest counts."""

    @abstractmethod
    def build_xtrace(self, xtrace_path: str | Path, *, reset: bool = False,
                     seed: bool = True) -> dict[str, int]:
        """Parse an XTrace capture into a fresh dataset. Returns ingest counts."""

    @abstractmethod
    def ingest_rtl(self, rtl_path: str | Path, *,
                   reset: bool = False) -> dict[str, int]:
        """Parse .v/.sv under `rtl_path` into the dataset."""

    @abstractmethod
    def ingest_sim(self, log_path: str | Path, *, name: str | None = None,
                   keep_all: bool = False, reset: bool = False) -> dict[str, int]:
        """Parse a simulator log into the dataset."""

    # -- sessions -----------------------------------------------------------

    @abstractmethod
    def open(self, *, read_only: bool = False) -> AbstractContextManager[Any]:
        """Context manager yielding a backend session.

        For `supports_raw_sql` backends the session is a DB-API connection.
        """

    # -- query / prompt -----------------------------------------------------

    @abstractmethod
    def stats(self, session: Any) -> dict[str, Any]:
        """Dataset statistics (meta + per-table row counts)."""

    @abstractmethod
    def run_prompt(self, session: Any, name: str, args: dict[str, Any] | None = None,
                   *, use_cache: bool = True,
                   ttl_seconds: int = 0) -> tuple[list[dict], bool]:
        """Run a stored prompt. Returns (rows, cache_hit).

        Relational backends run the prompt's `sql`; document-store backends run
        its `dsl_json` and raise if a prompt has none (a SQL-only prompt).
        """

    # -- waveform queries ---------------------------------------------------

    @abstractmethod
    def resolve_signal(self, session: Any, query: str) -> Any | None:
        """Resolve a signal by id / fullname / bare or suffix name. -> ResolvedSignal."""

    @abstractmethod
    def value_at(self, session: Any, sig_id: str, t: int) -> tuple[int, str] | None:
        """Last (t, value) of a signal at-or-before time t."""

    @abstractmethod
    def window(self, session: Any, sig_id: str, t0: int | None, t1: int | None,
               limit: int = 200) -> list[tuple[int, str]]:
        """All (t, value) changes of a signal within a time window."""

    @abstractmethod
    def find_signals(self, session: Any, pattern: str, limit: int = 50) -> list[Any]:
        """Glob/substring search for signals. -> list[ResolvedSignal]."""

    # -- prompt library CRUD ------------------------------------------------

    @abstractmethod
    def list_prompts(self, session: Any) -> list[Any]:
        """All stored prompts. -> list[Prompt]."""

    @abstractmethod
    def show_prompt(self, session: Any, name: str) -> Any:
        """One prompt by name (raises KeyError if absent). -> Prompt."""

    @abstractmethod
    def add_prompt(self, session: Any, name: str, sql: str, *, description: str = "",
                   params: list[dict] | None = None, overwrite: bool = False,
                   dsl_json: str = "") -> None:
        """Insert or replace a stored prompt."""

    @abstractmethod
    def remove_prompt(self, session: Any, name: str) -> bool:
        """Delete a stored prompt. Returns True if one was removed."""

    # -- result cache -------------------------------------------------------

    @abstractmethod
    def cache_stats(self, session: Any) -> dict[str, Any]:
        """Cache size / hits / per-prompt breakdown."""

    @abstractmethod
    def cache_list(self, session: Any, prompt: str | None = None,
                   limit: int = 50) -> list[dict]:
        """Recent cache entries, newest first."""

    @abstractmethod
    def cache_clear(self, session: Any, prompt: str | None = None) -> int:
        """Delete cache entries (all, or for one prompt). Returns count."""

    # -- bug knowledge base -------------------------------------------------
    # The logical surface is identical across backends; storage differs (a
    # SQLite side-table vs. embedded OpenSearch arrays). `links` is a sequence
    # of objects/tuples with `kind`/`value`/`extra`.

    @abstractmethod
    def add_bug(self, session: Any, name: str, *, title: str = "",
                status: str = "open", severity: str = "", symptom: str = "",
                root_cause: str = "", fix: str = "", fix_ref: str = "",
                keywords: list[str] | None = None, tags: list[str] | None = None,
                links: list[Any] | None = None, overwrite: bool = False) -> Any:
        """Create or (with overwrite) update a bug. Returns the stored bug."""

    @abstractmethod
    def get_bug(self, session: Any, name: str) -> Any | None:
        """Fetch one bug by name, or None."""

    @abstractmethod
    def list_bugs(self, session: Any, *, status: str | None = None,
                  severity: str | None = None, tag: str | None = None,
                  limit: int = 50) -> list[Any]:
        """List bugs, newest first, with optional facet filters."""

    @abstractmethod
    def remove_bug(self, session: Any, name: str) -> bool:
        """Delete a bug. Returns True if one was removed."""

    @abstractmethod
    def search_bugs(self, session: Any, query: str, *, status: str | None = None,
                    keyword: str | None = None, limit: int = 50) -> list[Any]:
        """Full-text search over bug text, with optional facet post-filters."""

    @abstractmethod
    def link_bug(self, session: Any, name: str, kind: str, value: str,
                 extra: str = "") -> Any:
        """Attach an association to a bug. Returns the updated bug."""

    @abstractmethod
    def close_bug(self, session: Any, name: str, *, status: str = "fixed",
                  fix: str | None = None, fix_ref: str | None = None) -> Any:
        """Set a bug's resolution. Returns the updated bug."""
