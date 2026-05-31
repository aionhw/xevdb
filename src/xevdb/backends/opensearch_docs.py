"""Turn parsed xevdb data into OpenSearch documents (Phase 3 write path).

Dependency-free on purpose: this module owns the *transformation* — including
the Strategy-B denormalization that lets the document store answer cross-side
prompts without joins — so it can be unit-tested without `opensearch-py` or a
cluster. The thin `helpers.bulk` call that ships these to a cluster lives in
``opensearch_backend.py``.

Each producer yields ``Action(table, id, source)``:
* ``table``  — logical table name (→ an index via ``opensearch_schema``)
* ``id``     — document _id, or None to let OpenSearch auto-assign
* ``source`` — the document body (a plain dict)
"""
from __future__ import annotations

from typing import Any, Iterable, Iterator, NamedTuple

from ..parser import VCD, bare_name


class Action(NamedTuple):
    table: str
    id: str | None
    source: dict[str, Any]


def xz_dirty(value: str) -> bool:
    """True when a VCD value carries any x/X/z/Z bit (mirrors the SQL LIKE set)."""
    return any(c in "xXzZ" for c in value)


def _fullname(hier: str, name: str) -> str:
    bare = bare_name(name)
    return f"{hier}.{bare}" if hier else bare


def module_key(file: str, line_start: int, name: str) -> str:
    """Stable document id for a module (replaces SQLite's autoincrement)."""
    return f"{file}::{line_start}::{name}"


# ----------------------------------------------------------------------------
# Waveform
# ----------------------------------------------------------------------------

def vcd_actions(vcd: VCD, *, source: str = "") -> Iterator[Action]:
    """Signals, denormalized changes, and meta for one parsed VCD."""
    for s in vcd.signals.values():
        full = _fullname(s.hier, s.name)
        yield Action("signals", s.id, {
            "id": s.id, "hier": s.hier, "name": s.name,
            "fullname": full, "width": s.width, "kind": s.kind,
        })

    for c in vcd.changes:
        s = vcd.signals.get(c.sig_id)
        # Strategy B: copy the signal's identity + a precomputed xz flag onto
        # every change so signal_transitions / xz_signals need no join.
        doc: dict[str, Any] = {
            "sig_id": c.sig_id, "t": c.t, "value": c.value, "xz": xz_dirty(c.value),
        }
        if s is not None:
            doc.update({
                "fullname": _fullname(s.hier, s.name), "name": s.name,
                "hier": s.hier, "width": s.width, "kind": s.kind,
            })
        yield Action("changes", None, doc)

    t_min = min((c.t for c in vcd.changes), default=0)
    t_max = max((c.t for c in vcd.changes), default=0)
    meta = {
        "source": source, "date": vcd.date, "version": vcd.version,
        "timescale": vcd.timescale, "t_min": str(t_min), "t_max": str(t_max),
        "n_signals": str(len(vcd.signals)), "n_changes": str(len(vcd.changes)),
        "xevdb_version": "0.1.0",
    }
    for key, value in meta.items():
        yield Action("meta", key, {"key": key, "value": value})


# ----------------------------------------------------------------------------
# RTL
# ----------------------------------------------------------------------------

def rtl_actions(
    files: Iterable[tuple[Any, list[Any], str]],
    *,
    now: float = 0.0,
) -> Iterator[Action]:
    """Source files + modules + (denormalized) ports/signals/instances.

    `files` is the iterable produced by `sv.walk_rtl`: (path, modules, raw_src).
    """
    import hashlib

    for path, modules, raw_src in files:
        spath = str(path)
        h = hashlib.sha256(raw_src.encode("utf-8")).hexdigest()
        yield Action("source_files", spath, {
            "path": spath, "content": raw_src, "hash": h, "ingested_at": now,
        })
        for m in modules:
            mfile = getattr(m, "file", spath) or spath
            mid = module_key(mfile, m.line_start, m.name)
            yield Action("modules", mid, {
                "id": mid, "name": m.name, "kind": m.kind, "file": mfile,
                "line_start": m.line_start, "line_end": m.line_end,
                "leading_comment": m.leading_comment, "body_summary": m.body_summary,
                "params_json": _json(m.parameters), "ast_json": _json(m.ast),
                "ingested_at": now,
            })
            for i, p in enumerate(m.ports):
                yield Action("module_ports", None, {
                    "module_id": mid, "position": i, "name": p.name,
                    "direction": p.direction, "width": p.width, "kind": p.kind,
                    "module_name": m.name,           # denorm
                })
            for sig in m.signals:
                yield Action("module_signals", None, {
                    "module_id": mid, "name": sig.name, "kind": sig.kind,
                    "line": sig.line, "width": sig.width, "decl_text": sig.decl_text,
                    "module_name": m.name, "file": mfile,   # denorm
                })
            for inst in m.instances:
                yield Action("module_instances", None, {
                    "parent_module_id": mid, "child_module_name": inst.module_name,
                    "instance_name": inst.instance_name, "line": inst.line,
                    "parent_module_name": m.name,    # denorm
                })


# ----------------------------------------------------------------------------
# Simulator log
# ----------------------------------------------------------------------------

def sim_actions(
    events: Iterable[Any],
    *,
    run_name: str,
    source: str,
    now: float,
    line_count: int,
    counts: dict[str, int],
) -> Iterator[Action]:
    """One sim_runs doc + one sim_events doc per event."""
    events = list(events)
    run_id = f"{run_name}::{now}"
    n_fatal = sum(counts.get(s, 0) for s in ("FATAL", "UVM_FATAL"))
    n_error = sum(counts.get(s, 0) for s in ("ERROR", "UVM_ERROR", "ASSERTION"))
    n_warning = sum(counts.get(s, 0) for s in ("WARNING", "UVM_WARNING"))
    yield Action("sim_runs", run_id, {
        "id": run_id, "name": run_name, "source": source, "ingested_at": now,
        "line_count": line_count, "n_events": len(events),
        "n_fatal": n_fatal, "n_error": n_error, "n_warning": n_warning,
        "severity_json": _json(counts),
    })
    for e in events:
        yield Action("sim_events", None, {
            "run_id": run_id, "line_no": e.line_no, "severity": e.severity,
            "t": e.t, "ref_file": e.ref_file, "ref_line": e.ref_line,
            "message": e.message,
        })


# ----------------------------------------------------------------------------
# Prompts (seed library)
# ----------------------------------------------------------------------------

def prompt_actions(seed_prompts: Iterable[Any], *, now: float = 0.0) -> Iterator[Action]:
    """One doc per seed prompt, keyed by name (idempotent)."""
    for p in seed_prompts:
        yield Action("prompts", p.name, {
            "name": p.name, "description": p.description, "sql": p.sql,
            "dsl_json": getattr(p, "dsl", "") or "", "params_json": _json(p.params),
            "created_at": now, "updated_at": now,
        })


def _json(obj: Any) -> str:
    import json
    return json.dumps(obj)
