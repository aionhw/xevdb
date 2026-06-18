"""Phase 1: the bundled RISC-V reference data loads and is well-formed."""
from __future__ import annotations

from xevdb import riscv


def test_load_bundled():
    data = riscv.load()
    assert data.spec_version
    c = data.counts()
    assert c["instructions"] >= 120
    assert c["registers"] == 64        # 32 GPR + 32 FPR
    assert c["csrs"] >= 30
    assert c["extensions"] >= 8
    assert c["pseudo"] >= 15


def test_unique_ids():
    data = riscv.load()
    for cat in ("instructions", "registers", "csrs", "extensions", "pseudo"):
        rows = getattr(data, cat)
        ids = [r.id for r in rows]
        assert len(ids) == len(set(ids)), f"duplicate ids in {cat}"


def test_registers_abi():
    regs = {r.name: r for r in riscv.load().registers}
    assert regs["x0"].abi == "zero"
    assert regs["x1"].abi == "ra" and regs["x1"].saver == "Caller"
    assert regs["x2"].abi == "sp" and regs["x2"].saver == "Callee"
    assert regs["x10"].abi == "a0"          # arg/return 0
    assert regs["x8"].abi == "s0"           # frame pointer
    gpr = [r for r in riscv.load().registers if r.group == "GPR"]
    fpr = [r for r in riscv.load().registers if r.group == "FPR"]
    assert len(gpr) == 32 and len(fpr) == 32


def test_key_csrs_present():
    csrs = {c.name: c for c in riscv.load().csrs}
    assert csrs["mstatus"].addr == "0x300"
    assert csrs["mtvec"].addr == "0x305"
    assert csrs["mepc"].addr == "0x341"
    assert csrs["mcause"].addr == "0x342"
    assert csrs["satp"].addr == "0x180" and csrs["satp"].privilege == "S"
    # addr lookup table is unambiguous
    by_addr = {c.addr: c for c in riscv.load().csrs}
    assert by_addr["0x305"].name == "mtvec"


def test_instruction_encodings():
    ins = {i.name: i for i in riscv.load().instructions}
    # base ops
    assert ins["addi"].mask == "0x0000707f" and ins["addi"].match == "0x00000013"
    assert ins["add"].match == "0x00000033" and ins["sub"].match == "0x40000033"
    assert ins["ecall"].mask == "0xffffffff"
    # extensions are populated
    exts = {i.extension for i in riscv.load().instructions}
    for e in ("RV32I", "RV64I", "M", "A", "F", "D", "C", "Zicsr"):
        assert e in exts, f"missing extension {e}"
    # M extension has mul/div
    m = {i.name for i in riscv.load().instructions if i.extension == "M"}
    assert {"mul", "div", "rem", "mulw"} <= m
    # compressed are 16-bit
    cinstr = [i for i in riscv.load().instructions if i.extension == "C"]
    assert cinstr and all(i.width == 16 for i in cinstr)


def test_pseudo_expansions():
    p = {x.name: x for x in riscv.load().pseudo}
    assert p["ret"].expansion == "jalr x0, 0(ra)"
    assert p["nop"].base == "addi"
    assert p["mv"].base == "addi"


def test_load_from_dir(tmp_path):
    """load(data_dir) reads the same content as the bundled package."""
    import importlib.resources as res
    src = res.files("xevdb").joinpath("data", "riscv")
    for cat in riscv.CATEGORIES:
        (tmp_path / f"{cat}.json").write_text(
            src.joinpath(f"{cat}.json").read_text(encoding="utf-8"), encoding="utf-8")
    data = riscv.load(tmp_path)
    assert data.counts() == riscv.load().counts()
