"""Backend registry and selection.

Pick a backend with `get_backend(name, db_path)`. `name` defaults to the
`XEVDB_BACKEND` environment variable, then to `sqlite`. New backends register
themselves here by adding an entry to `_REGISTRY`.
"""
from __future__ import annotations

import os
from pathlib import Path

from .base import Backend
from .sqlite_backend import SqliteBackend

# name -> Backend subclass. OpenSearch lands here once its module exists; it is
# imported lazily inside get_backend so its (optional) dependency stays opt-in.
_REGISTRY: dict[str, type[Backend]] = {
    SqliteBackend.name: SqliteBackend,
}

DEFAULT_BACKEND = "sqlite"


def available_backends() -> list[str]:
    return sorted(_REGISTRY)


def get_backend(name: str | None, db_path: str | Path) -> Backend:
    """Construct the backend for `name` over the dataset at `db_path`.

    Selection order: explicit `name` → `$XEVDB_BACKEND` → auto-detect (a JSON
    pointer file routes to `opensearch`) → `sqlite`.
    """
    chosen = name or os.environ.get("XEVDB_BACKEND")
    if not chosen:
        # Auto-route: a pointer file means the data lives on a cluster. The
        # check is dependency-free (no opensearch-py import) so it is safe even
        # when the optional backend isn't installed.
        from .opensearch_schema import looks_like_pointer
        chosen = "opensearch" if looks_like_pointer(db_path) else DEFAULT_BACKEND
    chosen = chosen.lower()

    if chosen == "opensearch" and chosen not in _REGISTRY:
        try:
            from .opensearch_backend import OpenSearchBackend  # noqa: F401
            _REGISTRY["opensearch"] = OpenSearchBackend
        except ImportError as e:  # pragma: no cover - backend not built yet
            raise ValueError(
                "the 'opensearch' backend is not available "
                f"(install with `bash install.sh --with-opensearch`): {e}"
            ) from e

    try:
        cls = _REGISTRY[chosen]
    except KeyError:
        raise ValueError(
            f"unknown backend {chosen!r}; available: {', '.join(available_backends())}"
        ) from None
    return cls(db_path)


__all__ = ["Backend", "SqliteBackend", "get_backend",
           "available_backends", "DEFAULT_BACKEND"]
