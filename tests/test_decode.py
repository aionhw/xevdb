"""RISC-V instruction decoder (bundled ISA, mask/match)."""
from __future__ import annotations

import pytest

from xevdb import decode as d


@pytest.mark.parametrize("word, name, asm, ext", [
    (0x00c58533, "add",   "add a0, a1, a2",   "RV32I"),
    (0x00450513, "addi",  "addi a0, a0, 4",   "RV32I"),
    (0x00b50463, "beq",   "beq a0, a1, 8",    "RV32I"),
    (0x0085a503, "lw",    "lw a0, 8(a1)",     "RV32I"),
    (0x00a5a423, "sw",    "sw a0, 8(a1)",     "RV32I"),
    (0x123450b7, "lui",   "lui ra, 0x12345",  "RV32I"),
    (0x00000073, "ecall", "ecall",            "RV32I"),
    (0x00100073, "ebreak", "ebreak",          "RV32I"),
    (0x02c58533, "mul",   "mul a0, a1, a2",   "M"),
])
def test_decode_known(word, name, asm, ext):
    r = d.decode(word)
    assert r.name == name and r.asm == asm and r.extension == ext
    assert r.width == 32


def test_decode_csr_fields():
    r = d.decode(0x30529073)            # csrrw, csr=0x305 (mtvec), rs1=t0
    assert r.name == "csrrw" and r.fields["csr"] == 0x305
    assert "0x305" in r.asm


def test_decode_compressed():
    r = d.decode(0x4501)                # low bits 01 -> 16-bit compressed
    assert r.width == 16 and r.name and r.extension == "C"


def test_decode_unknown():
    r = d.decode(0xffffffff)
    assert r.name is None and r.asm == "<unknown>"


def test_parse_word_forms():
    assert d.parse_word("0x13") == 0x13
    assert d.parse_word("0b10011") == 0b10011
    assert d.parse_word("00010011") == 0b10011        # bare VCD bit-string
    assert d.parse_word("19") == 19
    with pytest.raises(ValueError):
        d.parse_word("0001xz11")                       # X/Z rejected


def test_decode_from_bitstring_value():
    # a VCD vector value (addi a0,a0,4 = 0x00450513) as a 32-bit string
    word = d.parse_word(format(0x00450513, "032b"))
    assert d.decode(word).asm == "addi a0, a0, 4"
