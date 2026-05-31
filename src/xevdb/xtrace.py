"""Parse the lightweight XTrace text format used by xevdb_ai_debug.

XTrace is sample-oriented rather than VCD-oriented:

    xtrace.version 1.0
    session DBG-AXI-001
    source chipscopy
    timescale sample
    signal awvalid width=1
    signal awaddr width=32

    @0 awvalid=0 awaddr=0x0
    @1 awvalid=1 awaddr=0x1000

The parser converts it into the same VCD dataclasses used by xevdb's waveform
storage layer. Signal rows are declared from `signal` lines; value-change rows
are emitted only when a sampled value differs from the previous sample.
"""
from __future__ import annotations

import re
from pathlib import Path

from .parser import Change, Signal, VCD


_WIDTH_RE = re.compile(r"\bwidth=(\d+)\b")
_SAFE_RE = re.compile(r"[^A-Za-z0-9_$]+")


def _safe_part(value: str, fallback: str) -> str:
    value = _SAFE_RE.sub("_", value.strip()).strip("_")
    return value or fallback


def _sig_id(index: int) -> str:
    return f"x{index}"


def _parse_int(raw: str) -> int:
    raw = raw.strip().replace("_", "")
    return int(raw, 0)


def _format_value(raw: str, width: int) -> str:
    if raw.lower() in {"x", "z"}:
        return raw.lower()
    if set(raw.lower()) <= {"0", "1", "x", "z"} and not raw.startswith("0"):
        bits = raw.lower()
    else:
        bits = f"{_parse_int(raw):0{width}b}"
    return bits[-width:] if width > 1 else bits[-1]


def parse_file(path: str | Path) -> VCD:
    path = Path(path)
    session = "session"
    source = str(path)
    timescale = "sample"
    signals: list[tuple[str, int]] = []
    changes: list[tuple[int, str, str]] = []

    name_to_id: dict[str, str] = {}
    widths: dict[str, int] = {}
    last: dict[str, str] = {}

    for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("xtrace.version"):
            continue
        if line.startswith("session "):
            session = line.split(None, 1)[1].strip()
            continue
        if line.startswith("source "):
            source = line.split(None, 1)[1].strip()
            continue
        if line.startswith("timescale "):
            timescale = line.split(None, 1)[1].strip()
            continue
        if line.startswith("signal "):
            parts = line.split()
            if len(parts) < 3:
                raise ValueError(f"malformed signal line in {path}: {raw_line!r}")
            name = parts[1]
            match = _WIDTH_RE.search(line)
            width = int(match.group(1)) if match else 1
            if name not in name_to_id:
                name_to_id[name] = _sig_id(len(name_to_id))
                widths[name] = width
                signals.append((name, width))
            continue
        if line.startswith("@"):
            parts = line.split()
            t = int(parts[0][1:])
            for assignment in parts[1:]:
                if assignment.startswith("trigger=") or "=" not in assignment:
                    continue
                name, value_raw = assignment.split("=", 1)
                if name not in name_to_id:
                    name_to_id[name] = _sig_id(len(name_to_id))
                    widths[name] = 1
                    signals.append((name, 1))
                value = _format_value(value_raw, widths[name])
                if last.get(name) != value:
                    last[name] = value
                    changes.append((t, name_to_id[name], value))

    hier = f"top.{_safe_part(session, 'session')}"
    vcd = VCD(
        timescale=timescale,
        date="",
        version=f"xtrace source={source}",
    )
    for name, width in signals:
        sig_id = name_to_id[name]
        vcd.signals[sig_id] = Signal(
            id=sig_id,
            name=_safe_part(name, sig_id),
            width=width,
            hier=hier,
            kind="wire",
        )
    vcd.changes = [Change(t=t, sig_id=sig_id, value=value) for t, sig_id, value in changes]
    return vcd
