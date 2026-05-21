"""Simulator-log parser.

Reads UVM / iverilog / VCS / Questa-style sim logs line by line and extracts
a record per line that looks like a status message: severity, simulation
time (if embedded), any `file:line` reference, and the original message.

The goal is structured queries — "show every UVM_ERROR around t=200ns" —
not perfect coverage. Lines that don't match any severity pattern are
skipped (set `keep_all=True` on the CLI to retain them as severity='INFO').
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator


# ----------------------------------------------------------------------------
# Patterns
# ----------------------------------------------------------------------------

# Recognized severity keywords, ordered most-specific → least.
_SEVERITY_PATTERN = re.compile(
    r"\b("
    r"UVM_(?:FATAL|ERROR|WARNING|INFO)"
    r"|FATAL"
    r"|Fatal"
    r"|ERROR"
    r"|Error"
    r"|WARNING"
    r"|WARN"
    r"|Warning"
    r"|ASSERT(?:ION)?\s+FAIL(?:ED)?"
    r"|Assertion\s+failed"
    r"|FAILED"
    r"|NOTE"
    r"|Note"
    r")\b"
)

# Simulation time, several common encodings:
#   '@ 1500:'             (UVM_INFO @ 5: …)
#   '@  1500ns:'
#   '[1500]'              (some testbench monitors)
#   '[t=1500]' '[t = 1500]'
#   '# 100:'              (Questa default)
#   'at time 100'
#   'at 100 ps'
_TIME_PATTERNS: list[re.Pattern] = [
    re.compile(r"@\s*(\d+)(?:\s*(?:ns|ps|us|fs|s))?\s*:"),
    re.compile(r"\[\s*t\s*=\s*(\d+)\s*\]"),
    re.compile(r"\[(\d+)\]"),
    re.compile(r"#\s*(\d+)\s*:"),
    re.compile(r"\bat\s+time\s+(\d+)\b", re.IGNORECASE),
    re.compile(r"\bat\s+(\d+)\s*(?:ns|ps|us|fs|s)\b", re.IGNORECASE),
    # 'at t=200', 't=200ns', 'time=15' — common in UVM message bodies.
    re.compile(r"\b(?:t|time)\s*=\s*(\d+)(?:\s*(?:ns|ps|us|fs|s))?", re.IGNORECASE),
]

# `path/to/file.{v,sv,svh,vh,vhd,vhdl}:NNN` embedded in a message.
_REF_PATTERN = re.compile(
    r"([A-Za-z0-9_./\\-]+\.(?:sv|svh|v|vh|vhd|vhdl))\s*:\s*(\d+)",
    re.IGNORECASE,
)


# ----------------------------------------------------------------------------
# Records
# ----------------------------------------------------------------------------

@dataclass
class SimEvent:
    line_no: int                 # 1-based line in the log
    severity: str                # canonicalized; "" if keep_all and no severity
    t: int | None                # simulation time if recovered
    ref_file: str                # source ref embedded in the message
    ref_line: int
    message: str                 # the raw line (trimmed)


# ----------------------------------------------------------------------------
# Canonicalization
# ----------------------------------------------------------------------------

def _canonical_severity(raw: str) -> str:
    """Map matched severity text to a canonical token."""
    s = raw.strip()
    up = s.upper().replace(" ", "_")
    # UVM messages stay verbatim
    if up.startswith("UVM_"):
        return up
    # Generic severities
    if up in ("FATAL",):
        return "FATAL"
    if up in ("ERROR",):
        return "ERROR"
    if up in ("WARN", "WARNING"):
        return "WARNING"
    if up in ("NOTE",):
        return "NOTE"
    if up.startswith("ASSERT") or up.startswith("ASSERTION"):
        return "ASSERTION"
    if up == "FAILED":
        return "ERROR"   # bare "FAILED" usually means a check failed
    return up


def _extract_time(text: str) -> int | None:
    for pat in _TIME_PATTERNS:
        m = pat.search(text)
        if m:
            try:
                return int(m.group(1))
            except (ValueError, IndexError):
                continue
    return None


def _extract_ref(text: str) -> tuple[str, int]:
    m = _REF_PATTERN.search(text)
    if not m:
        return ("", 0)
    try:
        return (m.group(1), int(m.group(2)))
    except (ValueError, IndexError):
        return ("", 0)


# ----------------------------------------------------------------------------
# Public entry
# ----------------------------------------------------------------------------

def parse_lines(lines: Iterable[str], keep_all: bool = False) -> Iterator[SimEvent]:
    """Yield SimEvent records for matching lines in `lines`.

    `keep_all=True` retains every non-blank line (those without a recognized
    severity get severity="INFO"); the default skips them.
    """
    for i, raw in enumerate(lines):
        text = raw.strip()
        if not text:
            continue
        sev_match = _SEVERITY_PATTERN.search(text)
        if sev_match is None:
            if not keep_all:
                continue
            severity = "INFO"
        else:
            severity = _canonical_severity(sev_match.group(1))
        t = _extract_time(text)
        ref_file, ref_line = _extract_ref(text)
        yield SimEvent(
            line_no=i + 1,
            severity=severity,
            t=t,
            ref_file=ref_file,
            ref_line=ref_line,
            message=text,
        )


def parse_file(path: str | Path, keep_all: bool = False) -> list[SimEvent]:
    """Parse a sim log on disk into a list of events."""
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    return list(parse_lines(text.splitlines(), keep_all=keep_all))


def severity_counts(events: list[SimEvent]) -> dict[str, int]:
    out: dict[str, int] = {}
    for e in events:
        out[e.severity] = out.get(e.severity, 0) + 1
    return out
