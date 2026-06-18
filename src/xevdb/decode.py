"""Decode a RISC-V instruction word into a mnemonic + operands.

The bridge between the bundled ISA reference and an actual trace: given a 32-bit
(or 16-bit compressed) instruction word, find the matching instruction by
``(word & mask) == match`` over the bundled instruction table, extract the
operand fields per format, and render assembly (``addi a0, a1, 4``).

Uses the bundled data (``riscv.load()``) — no OpenSearch cluster or dataset
needed — so ``xevdb riscv-decode 0x...`` works offline. The trace-aware command
(``xevdb decode <db> <signal> --time T``) reads a word off a waveform and feeds
it here. Dependency-free.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from . import riscv


@dataclass
class Decoded:
    word: int
    width: int                       # 16 (compressed) or 32
    name: str | None                 # mnemonic, or None if unknown
    extension: str | None
    format: str | None
    asm: str                         # rendered assembly
    fields: dict[str, Any] = field(default_factory=dict)
    description: str = ""
    matches: list[str] = field(default_factory=list)  # all matching mnemonics

    def to_dict(self) -> dict[str, Any]:
        return {"word": f"0x{self.word:08x}", "width": self.width,
                "name": self.name, "extension": self.extension,
                "format": self.format, "asm": self.asm,
                "fields": self.fields, "description": self.description,
                "matches": self.matches}


def _sx(value: int, bits: int) -> int:
    """Sign-extend a `bits`-wide value."""
    sign = 1 << (bits - 1)
    return (value & (sign - 1)) - (value & sign)


def _popcount(n: int) -> int:
    return bin(n).count("1")


class Decoder:
    """Mask/match instruction decoder over the bundled RISC-V ISA table."""

    def __init__(self, data: riscv.RiscvData | None = None):
        self.data = data or riscv.load()
        # (mask, match, width, Instruction)
        self._table = [(int(i.mask, 16), int(i.match, 16), i.width, i)
                       for i in self.data.instructions]
        self._abi = {r.number: r.abi for r in self.data.registers
                     if r.group == "GPR"}

    def _reg(self, n: int) -> str:
        return self._abi.get(n, f"x{n}")

    def _fields_32(self, w: int, fmt: str) -> dict[str, Any]:
        f: dict[str, Any] = {
            "rd": self._reg((w >> 7) & 0x1f),
            "rs1": self._reg((w >> 15) & 0x1f),
            "rs2": self._reg((w >> 20) & 0x1f),
        }
        if fmt == "I":
            f["imm"] = _sx(w >> 20, 12)
            f["shamt"] = (w >> 20) & 0x3f
            f["csr"] = (w >> 20) & 0xfff
            f["zimm"] = (w >> 15) & 0x1f
        elif fmt == "S":
            f["imm"] = _sx(((w >> 25) << 5) | ((w >> 7) & 0x1f), 12)
        elif fmt == "B":
            imm = (((w >> 31) & 1) << 12) | (((w >> 7) & 1) << 11) \
                | (((w >> 25) & 0x3f) << 5) | (((w >> 8) & 0xf) << 1)
            f["imm"] = _sx(imm, 13)
        elif fmt == "U":
            f["imm"] = (w >> 12) & 0xfffff
        elif fmt == "J":
            imm = (((w >> 31) & 1) << 20) | (((w >> 12) & 0xff) << 12) \
                | (((w >> 20) & 1) << 11) | (((w >> 21) & 0x3ff) << 1)
            f["imm"] = _sx(imm, 21)
        return f

    def _render(self, instr, f: dict[str, Any]) -> str:
        ops = instr.operands
        if not ops:
            return instr.name
        repl = {
            "rd": str(f.get("rd", "")), "rs1": str(f.get("rs1", "")),
            "rs2": str(f.get("rs2", "")), "shamt": str(f.get("shamt", "")),
            "zimm": str(f.get("zimm", "")),
            "csr": (f"0x{f['csr']:x}" if "csr" in f else "csr"),
            "imm": str(f.get("imm", "")), "label": str(f.get("imm", "")),
        }
        if instr.format == "U" and "imm" in f:        # lui/auipc: hex upper-imm
            repl["imm"] = f"0x{f['imm']:x}"
        rendered = re.sub(r"[a-z][a-z0-9]*",
                          lambda m: repl.get(m.group(0), m.group(0)), ops)
        return f"{instr.name} {rendered}"

    def decode(self, word: int) -> Decoded:
        word &= 0xffffffff
        compressed = (word & 0b11) != 0b11
        width = 16 if compressed else 32
        w = word & 0xffff if compressed else word

        hits = [instr for (mask, match, iw, instr) in self._table
                if iw == width and (w & mask) == match]
        if not hits:
            return Decoded(word=w, width=width, name=None, extension=None,
                           format=None, asm="<unknown>", matches=[])
        # most specific = most fixed bits
        hits.sort(key=lambda i: _popcount(int(i.mask, 16)), reverse=True)
        instr = hits[0]
        if width == 16:
            # compressed operand layouts vary; report the mnemonic, not operands
            fields: dict[str, Any] = {}
            asm = instr.name
        else:
            fields = self._fields_32(w, instr.format)
            asm = self._render(instr, fields)
        return Decoded(word=w, width=width, name=instr.name,
                       extension=instr.extension, format=instr.format,
                       asm=asm, fields=fields, description=instr.description,
                       matches=[i.name for i in hits])


_DEFAULT: Decoder | None = None


def decode(word: int) -> Decoded:
    """Decode one instruction word using a shared default decoder."""
    global _DEFAULT
    if _DEFAULT is None:
        _DEFAULT = Decoder()
    return _DEFAULT.decode(word)


def parse_word(text: str) -> int:
    """Parse a hex (0x..), binary (0b.. or a bit-string with x/z rejected) or
    decimal instruction word from a string."""
    s = text.strip().lower()
    if s.startswith("0x"):
        return int(s, 16)
    if s.startswith("0b"):
        return int(s, 2)
    # bare token: a VCD vector value (binary bit-string, possibly x/z) or decimal
    if any(c in s for c in "xz"):
        raise ValueError(f"value has unknown (x/z) bits: {text!r}")
    if set(s) <= {"0", "1"} and len(s) > 2:
        return int(s, 2)            # a bare bit-string (VCD vector value)
    return int(s, 10)
