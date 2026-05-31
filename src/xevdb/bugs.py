"""Bug knowledge base stored in the .xevdb file (SQLite implementation).

A bug is a named, keyworded record of what went wrong and how it was fixed,
plus links to the signals / modules / file:line / events it touches. The name
is a unique slug — the searchable handle for the bug.

Storage (see `db._SQLITE_SCHEMA`):
  bugs(name PK, title, status, severity, symptom, root_cause, fix, fix_ref,
       keywords_json, tags_json, created_at, updated_at)
  bug_links(bug_name, kind, value, extra)        -- many associations per bug
  bugs_fts(...)                                  -- FTS5, when available

Full-text search uses FTS5 when this SQLite was built with it; otherwise it
falls back to a portable LIKE scan. The OpenSearch backend implements the same
logical surface against its own index (Phase B4).
"""
from __future__ import annotations

import json
import re
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Any

from . import db as _db


_VALID_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
LINK_KINDS = ("signal", "module", "ref", "event", "assertion", "txn", "coverage")


@dataclass
class BugLink:
    kind: str
    value: str
    extra: str = ""


@dataclass
class Bug:
    name: str
    title: str = ""
    status: str = "open"
    severity: str = ""
    symptom: str = ""
    root_cause: str = ""
    fix: str = ""
    fix_ref: str = ""
    keywords: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    created_at: float = 0.0
    updated_at: float = 0.0
    links: list[BugLink] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d = {
            "name": self.name, "title": self.title, "status": self.status,
            "severity": self.severity, "symptom": self.symptom,
            "root_cause": self.root_cause, "fix": self.fix, "fix_ref": self.fix_ref,
            "keywords": self.keywords, "tags": self.tags,
            "created_at": self.created_at, "updated_at": self.updated_at,
            "links": [{"kind": l.kind, "value": l.value, "extra": l.extra}
                      for l in self.links],
        }
        return d


# ----------------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------------

def normalize_name(name: str) -> str:
    """Slugify a bug name into the `[a-z0-9._-]` handle space."""
    s = re.sub(r"[^a-z0-9._-]+", "-", name.strip().lower()).strip("-")
    return s


def _table_exists(con: sqlite3.Connection, table: str) -> bool:
    return con.execute(
        "SELECT 1 FROM sqlite_master WHERE type IN ('table','view') AND name=?",
        (table,),
    ).fetchone() is not None


def _fts_sync(con: sqlite3.Connection, bug: Bug) -> None:
    if not _db.bug_fts_available(con):
        return
    con.execute("DELETE FROM bugs_fts WHERE name = ?", (bug.name,))
    con.execute(
        "INSERT INTO bugs_fts (name, title, symptom, root_cause, fix, keywords, tags) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (bug.name, bug.title, bug.symptom, bug.root_cause, bug.fix,
         " ".join(bug.keywords), " ".join(bug.tags)),
    )


def _fts_delete(con: sqlite3.Connection, name: str) -> None:
    if _db.bug_fts_available(con):
        con.execute("DELETE FROM bugs_fts WHERE name = ?", (name,))


def _row_to_bug(con: sqlite3.Connection, row: sqlite3.Row,
                with_links: bool = True) -> Bug:
    bug = Bug(
        name=row["name"], title=row["title"], status=row["status"],
        severity=row["severity"], symptom=row["symptom"],
        root_cause=row["root_cause"], fix=row["fix"], fix_ref=row["fix_ref"],
        keywords=json.loads(row["keywords_json"]),
        tags=json.loads(row["tags_json"]),
        created_at=row["created_at"], updated_at=row["updated_at"],
    )
    if with_links:
        bug.links = [
            BugLink(kind=r["kind"], value=r["value"], extra=r["extra"])
            for r in con.execute(
                "SELECT kind, value, extra FROM bug_links WHERE bug_name = ? "
                "ORDER BY kind, value", (bug.name,))
        ]
    return bug


# ----------------------------------------------------------------------------
# CRUD (B1)
# ----------------------------------------------------------------------------

def add_bug(
    con: sqlite3.Connection,
    name: str,
    *,
    title: str = "",
    status: str = "open",
    severity: str = "",
    symptom: str = "",
    root_cause: str = "",
    fix: str = "",
    fix_ref: str = "",
    keywords: list[str] | None = None,
    tags: list[str] | None = None,
    links: list[BugLink] | None = None,
    overwrite: bool = False,
) -> Bug:
    """Insert a bug (or update it when `overwrite=True`)."""
    _db._ensure_schema(con)
    slug = normalize_name(name)
    if not slug or not _VALID_NAME.match(slug):
        raise ValueError(f"invalid bug name {name!r}")

    exists = con.execute("SELECT created_at FROM bugs WHERE name = ?", (slug,)).fetchone()
    if exists and not overwrite:
        raise ValueError(f"bug {slug!r} already exists (use overwrite to update)")

    now = time.time()
    created = exists["created_at"] if exists else now
    keywords = keywords or []
    tags = tags or []

    con.execute(
        "INSERT OR REPLACE INTO bugs (name, title, status, severity, symptom, "
        "root_cause, fix, fix_ref, keywords_json, tags_json, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (slug, title, status, severity, symptom, root_cause, fix, fix_ref,
         json.dumps(keywords), json.dumps(tags), created, now),
    )
    # replace the link set wholesale on overwrite
    con.execute("DELETE FROM bug_links WHERE bug_name = ?", (slug,))
    for lk in (links or []):
        con.execute(
            "INSERT INTO bug_links (bug_name, kind, value, extra) VALUES (?, ?, ?, ?)",
            (slug, lk.kind, lk.value, lk.extra),
        )

    bug = Bug(name=slug, title=title, status=status, severity=severity,
              symptom=symptom, root_cause=root_cause, fix=fix, fix_ref=fix_ref,
              keywords=keywords, tags=tags, created_at=created, updated_at=now,
              links=list(links or []))
    _fts_sync(con, bug)
    con.commit()
    return bug


def get_bug(con: sqlite3.Connection, name: str) -> Bug | None:
    if not _table_exists(con, "bugs"):
        return None
    slug = normalize_name(name)
    row = con.execute("SELECT * FROM bugs WHERE name = ?", (slug,)).fetchone()
    return _row_to_bug(con, row) if row else None


def list_bugs(
    con: sqlite3.Connection,
    *,
    status: str | None = None,
    severity: str | None = None,
    tag: str | None = None,
    limit: int = 50,
) -> list[Bug]:
    if not _table_exists(con, "bugs"):
        return []
    where, params = [], []
    if status:
        where.append("status = ?"); params.append(status)
    if severity:
        where.append("severity = ?"); params.append(severity)
    if tag:
        where.append("tags_json LIKE ?"); params.append(f'%"{tag}"%')
    sql = "SELECT * FROM bugs"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY updated_at DESC LIMIT ?"
    params.append(limit)
    return [_row_to_bug(con, r) for r in con.execute(sql, params)]


def link_bug(con: sqlite3.Connection, name: str, kind: str, value: str,
             extra: str = "") -> Bug:
    """Attach an association (signal/module/ref/event/assertion/txn/coverage)."""
    if kind not in LINK_KINDS:
        raise ValueError(f"invalid link kind {kind!r}; one of {', '.join(LINK_KINDS)}")
    slug = normalize_name(name)
    bug = get_bug(con, slug)
    if bug is None:
        raise ValueError(f"no bug named {slug!r}")
    dup = con.execute(
        "SELECT 1 FROM bug_links WHERE bug_name=? AND kind=? AND value=?",
        (slug, kind, value),
    ).fetchone()
    if dup is None:
        con.execute(
            "INSERT INTO bug_links (bug_name, kind, value, extra) VALUES (?, ?, ?, ?)",
            (slug, kind, value, extra),
        )
    con.execute("UPDATE bugs SET updated_at = ? WHERE name = ?", (time.time(), slug))
    con.commit()
    return get_bug(con, slug)


def close_bug(con: sqlite3.Connection, name: str, *, status: str = "fixed",
              fix: str | None = None, fix_ref: str | None = None) -> Bug:
    """Set a bug's resolution (status, and optionally fix / fix_ref)."""
    slug = normalize_name(name)
    bug = get_bug(con, slug)
    if bug is None:
        raise ValueError(f"no bug named {slug!r}")
    bug.status = status
    if fix is not None:
        bug.fix = fix
    if fix_ref is not None:
        bug.fix_ref = fix_ref
    bug.updated_at = time.time()
    con.execute(
        "UPDATE bugs SET status=?, fix=?, fix_ref=?, updated_at=? WHERE name=?",
        (bug.status, bug.fix, bug.fix_ref, bug.updated_at, slug),
    )
    _fts_sync(con, bug)
    con.commit()
    return bug


def remove_bug(con: sqlite3.Connection, name: str) -> bool:
    if not _table_exists(con, "bugs"):
        return False
    slug = normalize_name(name)
    cur = con.execute("DELETE FROM bugs WHERE name = ?", (slug,))
    con.execute("DELETE FROM bug_links WHERE bug_name = ?", (slug,))
    _fts_delete(con, slug)
    con.commit()
    return cur.rowcount > 0


# ----------------------------------------------------------------------------
# Search (B2)
# ----------------------------------------------------------------------------

def search_bugs(
    con: sqlite3.Connection,
    query: str,
    *,
    status: str | None = None,
    keyword: str | None = None,
    limit: int = 50,
) -> list[Bug]:
    """Full-text search over bug text (FTS5 when available, else LIKE).

    `status` / `keyword` are post-filters. Results are ranked by FTS relevance
    when FTS5 is in use, otherwise by most-recently-updated.
    """
    if not _table_exists(con, "bugs"):
        return []
    query = (query or "").strip()

    bugs: list[Bug] | None = None
    if _db.bug_fts_available(con) and query:
        try:
            names = [
                r["name"] for r in con.execute(
                    "SELECT name FROM bugs_fts WHERE bugs_fts MATCH ? ORDER BY rank",
                    (query,))
            ]
            bugs = [b for n in names if (b := get_bug(con, n)) is not None]
        except sqlite3.OperationalError:
            bugs = None  # malformed FTS query syntax — fall back to LIKE
    if bugs is None:
        bugs = _like_search(con, query, limit * 4 if query else limit)

    if status:
        bugs = [b for b in bugs if b.status == status]
    if keyword:
        bugs = [b for b in bugs if keyword in b.keywords]
    return bugs[:limit]


def _like_search(con: sqlite3.Connection, query: str, limit: int) -> list[Bug]:
    if not query:
        return list_bugs(con, limit=limit)
    like = f"%{query}%"
    rows = con.execute(
        "SELECT * FROM bugs WHERE title LIKE ? OR symptom LIKE ? OR root_cause LIKE ? "
        "OR fix LIKE ? OR keywords_json LIKE ? OR tags_json LIKE ? "
        "ORDER BY updated_at DESC LIMIT ?",
        (like, like, like, like, like, like, limit),
    )
    return [_row_to_bug(con, r) for r in rows]
