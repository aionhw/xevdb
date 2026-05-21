"""Minimal VCD parser.

Handles the subset of IEEE 1364 that any conformant simulator emits:
$date / $version / $timescale headers, $scope/$upscope hierarchy, $var
declarations (wire/reg/integer/real/...), $enddefinitions, and the
three value-change forms (scalar `1!`, vector `b1010 !`, real `r1.5 !`).

Not a full IEEE 1364 implementation — VCD ids aliased across scopes keep
only the last `Signal` row (rare in practice; most testbenches give each
signal its own id). Comments, dumpall, and obscure directives are skipped.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, TextIO


@dataclass
class Signal:
    id: str           # VCD identifier code (e.g. "!", "#23")
    name: str         # bare signal name plus any range suffix (e.g. "count[7:0]")
    width: int
    hier: str         # dot-joined scope path (e.g. "top.dut")
    kind: str = "wire"


@dataclass
class Change:
    t: int
    sig_id: str
    value: str        # "0"/"1"/"x"/"z" or vector bit-string or "r<float>" for reals


@dataclass
class VCD:
    timescale: str = ""
    date: str = ""
    version: str = ""
    signals: dict[str, Signal] = field(default_factory=dict)
    changes: list[Change] = field(default_factory=list)


def _tokens(stream: TextIO) -> Iterator[str]:
    for line in stream:
        for tok in line.split():
            yield tok


def parse(stream: TextIO) -> VCD:
    """Parse a VCD stream into a VCD object."""
    vcd = VCD()
    it = _tokens(stream)
    scope_stack: list[str] = []
    current_time = 0

    def read_until_end() -> list[str]:
        out: list[str] = []
        for tok in it:
            if tok == "$end":
                return out
            out.append(tok)
        return out

    in_defs = True
    for tok in it:
        if in_defs:
            if tok == "$date":
                vcd.date = " ".join(read_until_end())
            elif tok == "$version":
                vcd.version = " ".join(read_until_end())
            elif tok == "$timescale":
                vcd.timescale = " ".join(read_until_end())
            elif tok == "$scope":
                parts = read_until_end()
                if len(parts) >= 2:
                    scope_stack.append(parts[1])
            elif tok == "$upscope":
                read_until_end()
                if scope_stack:
                    scope_stack.pop()
            elif tok == "$var":
                parts = read_until_end()
                # $var wire 1 ! clk $end                -> ["wire","1","!","clk"]
                # $var reg 8 " count [7:0] $end         -> ["reg","8",'"',"count","[7:0]"]
                if len(parts) >= 4:
                    kind = parts[0]
                    width = int(parts[1])
                    sig_id = parts[2]
                    name = parts[3]
                    if len(parts) >= 5:
                        name = name + parts[4]
                    hier = ".".join(scope_stack)
                    vcd.signals[sig_id] = Signal(
                        id=sig_id, name=name, width=width, hier=hier, kind=kind
                    )
            elif tok == "$comment":
                read_until_end()
            elif tok == "$enddefinitions":
                read_until_end()
                in_defs = False
            elif tok in ("$dumpvars", "$dumpon", "$dumpoff", "$dumpall", "$end"):
                continue
            elif tok.startswith("$"):
                read_until_end()
            continue

        # value-change section
        if tok.startswith("#"):
            current_time = int(tok[1:])
        elif tok in ("$dumpvars", "$dumpon", "$dumpoff", "$dumpall", "$end"):
            continue
        elif tok[0] in "01xXzZ" and len(tok) >= 2:
            value = tok[0].lower()
            sig_id = tok[1:]
            vcd.changes.append(Change(t=current_time, sig_id=sig_id, value=value))
        elif tok[0] in "bB":
            value = tok[1:]
            sig_id = next(it)
            vcd.changes.append(Change(t=current_time, sig_id=sig_id, value=value))
        elif tok[0] in "rR":
            value = "r" + tok[1:]
            sig_id = next(it)
            vcd.changes.append(Change(t=current_time, sig_id=sig_id, value=value))

    return vcd


def parse_file(path: str | Path) -> VCD:
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return parse(f)


def bare_name(name: str) -> str:
    """Strip trailing `[7:0]`-style range suffix from a VCD signal name."""
    i = name.find("[")
    return name[:i] if i != -1 else name
