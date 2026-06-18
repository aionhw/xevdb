"""OpenSearch index design for a xevdb dataset (Phase 2).

This module is deliberately free of any `opensearch-py` dependency: it is pure
data + string helpers describing *how* a dataset maps onto a set of OpenSearch
indices, so it can be unit-tested without a cluster or the optional package.
The client-bound backend lives in `opensearch_backend.py`.

Model
-----
One SQLite ``.xevdb`` file becomes one *index set* on a cluster, identified by
a ``dump_id`` slug. Every logical table becomes one index named
``f"{prefix}-{dump_id}-{table}"`` (OpenSearch requires lowercase index names).

Because a cluster is not a file you can email, the on-disk artifact the user
passes to the CLI is replaced by a tiny **pointer file** — JSON naming the
cluster + dump_id. That keeps the "hand someone a small thing" ergonomic of a
``.xevdb`` while the bulk data lives in the cluster.

Strategy B (dual representation)
--------------------------------
Stored prompts carry both ``sql`` (run by SQLite) and an optional ``dsl_json``
(run here). OpenSearch does not do relational joins well, so the cross-side
prompts are made join-free by **denormalizing at ingest**: e.g. each
``changes`` document copies ``fullname``/``width``/``kind`` from its signal,
and each X/Z-relevant doc copies the RTL declaration site. The extra fields
live in the mappings below (see ``denorm`` markers) and are populated by the
writer in Phase 3.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_PREFIX = "xevdb"

# Logical tables, in build/ingest order. Each maps to one index.
TABLES: tuple[str, ...] = (
    "meta",
    "signals",
    "changes",
    "source_files",
    "modules",
    "module_ports",
    "module_signals",
    "module_instances",
    "sim_runs",
    "sim_events",
    "prompts",
    "cache",
    "bugs",
    # RISC-V ISA reference (a standalone, waveform-independent knowledge base)
    "riscv_instructions",
    "riscv_registers",
    "riscv_csrs",
    "riscv_extensions",
    "riscv_pseudo",
    # RISC-V Linux kernel architecture/ABI (parsed from a kernel source tree)
    "kernel_syscalls",
    "kernel_traps",
    "kernel_sbi",
    "kernel_memmap",
)

# RISC-V reference tables, as a group for targeted `ingest-riscv --reset`
# (mirrors RTL_TABLES / SIM_TABLES). These hold static ISA knowledge and are
# not tied to any VCD/RTL/sim dump.
RISCV_TABLES: tuple[str, ...] = (
    "riscv_instructions", "riscv_registers", "riscv_csrs",
    "riscv_extensions", "riscv_pseudo",
)

# RISC-V Linux kernel architecture tables, as a group for `ingest-kernel`.
# Also standalone, waveform-independent (syscall numbers, trap causes, the SBI
# S<->M ABI, and the virtual-memory layout / boot register ABI).
KERNEL_TABLES: tuple[str, ...] = (
    "kernel_syscalls", "kernel_traps", "kernel_sbi", "kernel_memmap",
)

# Bug-link kind -> the denormalized array field it lives in on a bug document.
# (SQLite keeps these in the bug_links side table; the doc store embeds them.)
BUG_LINK_FIELD: dict[str, str] = {
    "signal": "signals",
    "module": "modules",
    "ref": "refs",
    "event": "linked_events",
    "assertion": "linked_assertions",
    "txn": "linked_txns",
    "coverage": "linked_coverage",
}

# Tables written by each ingest entry point — used for per-group --reset so
# `ingest-rtl --reset` doesn't wipe the waveform side, etc.
VCD_TABLES: tuple[str, ...] = ("signals", "changes")
RTL_TABLES: tuple[str, ...] = (
    "source_files", "modules", "module_ports", "module_signals", "module_instances",
)
SIM_TABLES: tuple[str, ...] = ("sim_runs", "sim_events")

# Shared index settings. Single shard is plenty for one dump and keeps doc
# ordering / counts predictable; refresh tuned for bulk ingest at write time.
_SETTINGS = {"index": {"number_of_shards": 1, "number_of_replicas": 0}}

# Field mappings per table. `keyword` for exact match + sorting + term aggs
# (signal ids, names, hier, raw values, severities). `text` only where we want
# tokenized full-text (source content, raw log message). `long`/`double` for
# numerics. Strategy-B denormalized fields are marked with a `# denorm` note.
#
# `_BLOB` is for arbitrary-length JSON strings kept only for retrieval from
# `_source` (never searched or aggregated): index AND doc_values off, so they
# dodge Lucene's 32 766-byte keyword/doc-values limit (a full module AST or a
# cached result set easily exceeds it).
_BLOB = {"type": "keyword", "index": False, "doc_values": False}

_PROPS: dict[str, dict[str, Any]] = {
    "meta": {
        "key": {"type": "keyword"},
        "value": {"type": "keyword"},
    },
    "signals": {
        "id": {"type": "keyword"},
        "hier": {"type": "keyword"},
        "name": {"type": "keyword"},
        "fullname": {"type": "keyword"},
        "width": {"type": "long"},
        "kind": {"type": "keyword"},
    },
    "changes": {
        "sig_id": {"type": "keyword"},
        "t": {"type": "long"},
        "value": {"type": "keyword"},
        # denorm from signals — lets signal_transitions/xz_signals skip the join
        "fullname": {"type": "keyword"},
        "name": {"type": "keyword"},
        "hier": {"type": "keyword"},
        "width": {"type": "long"},
        "kind": {"type": "keyword"},
        # precomputed at ingest: does `value` carry any x/X/z/Z bit?
        "xz": {"type": "boolean"},
    },
    "source_files": {
        "path": {"type": "keyword"},
        "content": {"type": "text"},
        "hash": {"type": "keyword"},
        "ingested_at": {"type": "double"},
    },
    "modules": {
        # Stable string key `file::line_start::name` (not a SQLite autoincrement)
        # so re-ingest is idempotent and children link without a sequence.
        "id": {"type": "keyword"},
        "name": {"type": "keyword"},
        "kind": {"type": "keyword"},
        "file": {"type": "keyword"},
        "line_start": {"type": "long"},
        "line_end": {"type": "long"},
        "leading_comment": {"type": "text"},
        "body_summary": {"type": "text"},
        "params_json": _BLOB,
        "ast_json": _BLOB,
        "ingested_at": {"type": "double"},
    },
    "module_ports": {
        "module_id": {"type": "keyword"},
        "position": {"type": "long"},
        "name": {"type": "keyword"},
        "direction": {"type": "keyword"},
        "width": {"type": "keyword"},
        "kind": {"type": "keyword"},
        "module_name": {"type": "keyword"},   # denorm from modules
    },
    "module_signals": {
        "module_id": {"type": "keyword"},
        "name": {"type": "keyword"},
        "kind": {"type": "keyword"},
        "line": {"type": "long"},
        "width": {"type": "keyword"},
        "decl_text": {"type": "text"},
        "module_name": {"type": "keyword"},   # denorm from modules
        "file": {"type": "keyword"},          # denorm from modules
    },
    "module_instances": {
        "parent_module_id": {"type": "keyword"},
        "child_module_name": {"type": "keyword"},
        "instance_name": {"type": "keyword"},
        "line": {"type": "long"},
        "parent_module_name": {"type": "keyword"},  # denorm from modules
    },
    "sim_runs": {
        "id": {"type": "keyword"},   # stable string `name::ingested_at`
        "name": {"type": "keyword"},
        "source": {"type": "keyword"},
        "ingested_at": {"type": "double"},
        "line_count": {"type": "long"},
        "n_events": {"type": "long"},
        "n_fatal": {"type": "long"},
        "n_error": {"type": "long"},
        "n_warning": {"type": "long"},
        "severity_json": _BLOB,
    },
    "sim_events": {
        "run_id": {"type": "keyword"},
        "line_no": {"type": "long"},
        "severity": {"type": "keyword"},
        "t": {"type": "long"},
        "ref_file": {"type": "keyword"},
        "ref_line": {"type": "long"},
        "message": {"type": "text"},
    },
    "prompts": {
        "name": {"type": "keyword"},
        "description": {"type": "text"},
        "sql": _BLOB,
        "dsl_json": _BLOB,
        "params_json": _BLOB,
        "created_at": {"type": "double"},
        "updated_at": {"type": "double"},
    },
    "cache": {
        "key": {"type": "keyword"},
        "prompt_name": {"type": "keyword"},
        "args_json": _BLOB,
        "result_json": _BLOB,
        "created_at": {"type": "double"},
        "hits": {"type": "long"},
        "last_hit_at": {"type": "double"},
        "ttl_seconds": {"type": "long"},
    },
    "bugs": {
        "name": {"type": "keyword"},
        "title": {"type": "text", "fields": {"raw": {"type": "keyword"}}},
        "status": {"type": "keyword"},
        "severity": {"type": "keyword"},
        "symptom": {"type": "text"},
        "root_cause": {"type": "text"},
        "fix": {"type": "text"},
        "fix_ref": {"type": "keyword"},
        "keywords": {"type": "keyword"},
        "tags": {"type": "keyword"},
        # denormalized link arrays (vs SQLite's bug_links side table)
        "signals": {"type": "keyword"},
        "modules": {"type": "keyword"},
        "refs": {"type": "keyword"},
        "linked_events": {"type": "keyword"},
        "linked_assertions": {"type": "keyword"},
        "linked_txns": {"type": "keyword"},
        "linked_coverage": {"type": "keyword"},
        "created_at": {"type": "double"},
        "updated_at": {"type": "double"},
    },
    # -- RISC-V ISA reference -------------------------------------------------
    "riscv_instructions": {
        "id": {"type": "keyword"},            # `<extension>::<name>`
        "name": {"type": "keyword"},          # exact mnemonic (jalr, addi, ...)
        "mnemonic": {"type": "keyword"},
        "extension": {"type": "keyword"},     # RV32I/RV64I/M/A/F/D/C/Zicsr/...
        "format": {"type": "keyword"},        # R/I/S/B/U/J/C
        "width": {"type": "long"},            # 32 or 16 (compressed)
        "mask": {"type": "keyword"},          # hex: fixed-bit mask
        "match": {"type": "keyword"},         # hex: fixed-bit value
        "operands": {"type": "keyword"},
        "syntax": {"type": "text"},
        "description": {"type": "text"},
        "pseudo": {"type": "boolean"},
        "ingested_at": {"type": "double"},
    },
    "riscv_registers": {
        "id": {"type": "keyword"},
        "name": {"type": "keyword"},          # x0..x31 / f0..f31
        "abi": {"type": "keyword"},           # zero/ra/sp/a0/...
        "number": {"type": "long"},
        "group": {"type": "keyword"},         # GPR / FPR
        "role": {"type": "text"},
        "saver": {"type": "keyword"},         # Caller / Callee / —
        "ingested_at": {"type": "double"},
    },
    "riscv_csrs": {
        "id": {"type": "keyword"},
        "addr": {"type": "keyword"},          # 0xNNN
        "name": {"type": "keyword"},          # mstatus/mtvec/mepc/satp/...
        "privilege": {"type": "keyword"},     # M / S / U
        "access": {"type": "keyword"},        # RW / RO
        "description": {"type": "text"},
        "ingested_at": {"type": "double"},
    },
    "riscv_extensions": {
        "id": {"type": "keyword"},
        "letter": {"type": "keyword"},        # I/M/A/F/D/C/Zicsr/...
        "name": {"type": "text", "fields": {"raw": {"type": "keyword"}}},
        "version": {"type": "keyword"},
        "description": {"type": "text"},
        "ingested_at": {"type": "double"},
    },
    "riscv_pseudo": {
        "id": {"type": "keyword"},
        "name": {"type": "keyword"},          # nop/mv/ret/li/...
        "expansion": {"type": "keyword"},     # real-instruction expansion
        "base": {"type": "keyword"},          # underlying real instruction
        "description": {"type": "text"},
        "ingested_at": {"type": "double"},
    },
    # -- RISC-V Linux kernel architecture / ABI ------------------------------
    "kernel_syscalls": {
        "id": {"type": "keyword"},
        "nr": {"type": "long"},               # syscall number (the value in a7)
        "name": {"type": "keyword"},          # openat / read / write / ...
        "entry": {"type": "keyword"},         # sys_xxx kernel entry symbol
        "abi": {"type": "keyword"},           # generic / riscv
        "description": {"type": "text"},
        "ingested_at": {"type": "double"},
    },
    "kernel_traps": {
        "id": {"type": "keyword"},
        "code": {"type": "long"},             # scause/mcause exception/interrupt code
        "kind": {"type": "keyword"},          # exception / interrupt
        "name": {"type": "keyword"},          # EXC_SYSCALL / IRQ_S_TIMER
        "label": {"type": "text"},
        "description": {"type": "text"},
        "ingested_at": {"type": "double"},
    },
    "kernel_sbi": {
        "id": {"type": "keyword"},
        "kind": {"type": "keyword"},          # extension / function
        "extension": {"type": "keyword"},     # TIME / IPI / HSM / ...
        "name": {"type": "keyword"},          # SBI_EXT_HSM_HART_START
        "eid": {"type": "keyword"},           # hex extension id (extensions)
        "fid": {"type": "long"},              # function id (functions; -1 for ext)
        "description": {"type": "text"},
        "ingested_at": {"type": "double"},
    },
    "kernel_memmap": {
        "id": {"type": "keyword"},
        "category": {"type": "keyword"},      # vm-layout / boot-abi
        "mode": {"type": "keyword"},          # Sv39 / Sv48 / Sv57
        "region": {"type": "keyword"},        # vmalloc / kernel / a0 / ...
        "start": {"type": "keyword"},
        "end": {"type": "keyword"},
        "size": {"type": "keyword"},
        "description": {"type": "text"},
        "ingested_at": {"type": "double"},
    },
}


def slugify(name: str) -> str:
    """Turn an arbitrary name into a valid lowercase index-name component."""
    s = re.sub(r"[^a-z0-9_-]+", "-", name.lower()).strip("-_")
    return s or "dump"


def index_name(prefix: str, dump_id: str, table: str) -> str:
    if table not in _PROPS:
        raise KeyError(f"unknown table {table!r}")
    return f"{prefix}-{dump_id}-{table}".lower()


def all_index_names(prefix: str, dump_id: str) -> dict[str, str]:
    return {t: index_name(prefix, dump_id, t) for t in TABLES}


def mapping_for(table: str) -> dict[str, Any]:
    """The full create-index body (settings + mappings) for one table."""
    return {"settings": _SETTINGS, "mappings": {"properties": _PROPS[table]}}


# ----------------------------------------------------------------------------
# Pointer file — the small artifact that stands in for the .xevdb file
# ----------------------------------------------------------------------------

@dataclass
class Pointer:
    """Locates a dataset's index set on a cluster.

    Serialized as a small JSON file the CLI accepts wherever it accepts a
    ``.xevdb`` path. ``backend: "opensearch"`` lets the registry route a bare
    path to this backend even without an explicit ``--backend`` flag.
    """
    hosts: list[str]
    dump_id: str
    prefix: str = DEFAULT_PREFIX
    backend: str = "opensearch"
    extra: dict[str, Any] = field(default_factory=dict)  # auth/ssl knobs

    def index(self, table: str) -> str:
        return index_name(self.prefix, self.dump_id, table)

    def indices(self) -> dict[str, str]:
        return all_index_names(self.prefix, self.dump_id)

    def to_json(self) -> str:
        return json.dumps({
            "backend": self.backend,
            "hosts": self.hosts,
            "dump_id": self.dump_id,
            "prefix": self.prefix,
            **({"extra": self.extra} if self.extra else {}),
        }, indent=2)


def write_pointer(path: str | Path, ptr: Pointer) -> None:
    Path(path).write_text(ptr.to_json() + "\n", encoding="utf-8")


def read_pointer(path: str | Path) -> Pointer:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return Pointer(
        hosts=data["hosts"],
        dump_id=data["dump_id"],
        prefix=data.get("prefix", DEFAULT_PREFIX),
        backend=data.get("backend", "opensearch"),
        extra=data.get("extra", {}),
    )


def looks_like_pointer(path: str | Path) -> bool:
    """True if `path` is a JSON pointer file naming the opensearch backend.

    Lets the registry auto-route a bare dataset path to the right backend.
    """
    p = Path(path)
    if not p.is_file():
        return False
    try:
        head = p.read_text(encoding="utf-8", errors="ignore").lstrip()
        if not head.startswith("{"):
            return False
        data = json.loads(head)
    except (ValueError, OSError):
        return False
    return isinstance(data, dict) and data.get("backend") == "opensearch"
