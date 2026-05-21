"""Query-result cache embedded in the .vcdb file.

Stores the JSON-serialized output of `prompt run <name>` keyed by a SHA256 of
(prompt_name, canonical_args_json). Two writes with the same arguments serve
the second one from cache and bump the hit counter.

TTL is optional; 0 means no expiry. Bypass entirely via `XEVDB_NO_CACHE=1`.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from typing import Any


def enabled() -> bool:
    return os.environ.get("XEVDB_NO_CACHE") != "1"


def make_key(prompt_name: str, args: dict[str, Any]) -> tuple[str, str]:
    """Compute (cache_key, canonical_args_json) for a prompt invocation."""
    canonical = json.dumps(args, sort_keys=True, separators=(",", ":"))
    h = hashlib.sha256()
    h.update(prompt_name.encode("utf-8"))
    h.update(b"\x1f")
    h.update(canonical.encode("utf-8"))
    return h.hexdigest(), canonical


def get(con: sqlite3.Connection, prompt_name: str, args: dict[str, Any]) -> list[dict] | None:
    if not enabled():
        return None
    key, _ = make_key(prompt_name, args)
    now = time.time()
    row = con.execute(
        "SELECT result_json, created_at, ttl_seconds FROM cache WHERE key = ?",
        (key,),
    ).fetchone()
    if row is None:
        return None
    result_json, created_at, ttl_seconds = row[0], row[1], row[2]
    if ttl_seconds and (now - created_at) > ttl_seconds:
        con.execute("DELETE FROM cache WHERE key = ?", (key,))
        con.commit()
        return None
    con.execute(
        "UPDATE cache SET hits = hits + 1, last_hit_at = ? WHERE key = ?",
        (now, key),
    )
    con.commit()
    return json.loads(result_json)


def put(
    con: sqlite3.Connection,
    prompt_name: str,
    args: dict[str, Any],
    result: list[dict],
    ttl_seconds: int = 0,
) -> None:
    if not enabled():
        return
    key, args_json = make_key(prompt_name, args)
    now = time.time()
    con.execute(
        "INSERT OR REPLACE INTO cache "
        "(key, prompt_name, args_json, result_json, created_at, hits, last_hit_at, ttl_seconds) "
        "VALUES (?, ?, ?, ?, ?, 0, NULL, ?)",
        (key, prompt_name, args_json, json.dumps(result), now, ttl_seconds),
    )
    con.commit()


def stats(con: sqlite3.Connection) -> dict[str, Any]:
    n = con.execute("SELECT COUNT(*) FROM cache").fetchone()[0]
    total_hits = con.execute("SELECT COALESCE(SUM(hits), 0) FROM cache").fetchone()[0]
    bytes_used = con.execute(
        "SELECT COALESCE(SUM(length(result_json)), 0) FROM cache"
    ).fetchone()[0]
    by_prompt = {
        row[0]: row[1]
        for row in con.execute(
            "SELECT prompt_name, COUNT(*) FROM cache GROUP BY prompt_name"
        )
    }
    return {
        "enabled": enabled(),
        "entries": n,
        "total_hits": total_hits,
        "result_bytes": bytes_used,
        "by_prompt": by_prompt,
    }


def list_entries(con: sqlite3.Connection, prompt: str | None = None, limit: int = 50) -> list[dict]:
    sql = (
        "SELECT key, prompt_name, args_json, length(result_json) AS bytes, "
        "       created_at, hits, last_hit_at, ttl_seconds "
        "FROM cache"
    )
    params: tuple = ()
    if prompt:
        sql += " WHERE prompt_name = ?"
        params = (prompt,)
    sql += " ORDER BY created_at DESC LIMIT ?"
    params = params + (limit,)
    return [
        {
            "key": row[0][:16],
            "prompt": row[1],
            "args": json.loads(row[2]),
            "bytes": row[3],
            "created_at": row[4],
            "hits": row[5],
            "last_hit_at": row[6],
            "ttl_seconds": row[7],
        }
        for row in con.execute(sql, params)
    ]


def clear(con: sqlite3.Connection, prompt: str | None = None) -> int:
    if prompt is None:
        cur = con.execute("DELETE FROM cache")
    else:
        cur = con.execute("DELETE FROM cache WHERE prompt_name = ?", (prompt,))
    n = cur.rowcount
    con.commit()
    return n
