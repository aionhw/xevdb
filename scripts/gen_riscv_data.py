#!/usr/bin/env python3
"""Generate the bundled RISC-V ISA reference JSON for xevdb.

Self-contained: the ISA knowledge lives here as curated Python tables and is
emitted as deterministic JSON under ``src/xevdb/data/riscv/``. No network or
external repo (e.g. riscv/riscv-opcodes) is required, so the data ships with the
package and ``xevdb ingest-riscv`` needs nothing at run time.

Encodings (``mask``/``match``, both hex) are computed from the constant fields
of each instruction the same way riscv-opcodes does: ``mask`` marks the bits
that are fixed for the mnemonic, ``match`` is their value. Variable fields
(rd/rs1/rs2/imm, and AMO aq/rl, FP rm) are left out of the mask.

Run:  python scripts/gen_riscv_data.py
"""
from __future__ import annotations

import json
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "src" / "xevdb" / "data" / "riscv"
SPEC_VERSION = "20240411"  # ratified unpriv/priv snapshot this data tracks


# ---------------------------------------------------------------------------
# Encoding helpers — set constant bit fields, accumulate mask/match.
# ---------------------------------------------------------------------------
def _bits(fields: list[tuple[int, int, int]]) -> tuple[int, int]:
    """fields = [(hi, lo, value), ...] -> (mask, match)."""
    mask = match = 0
    for hi, lo, val in fields:
        width = hi - lo + 1
        m = ((1 << width) - 1) << lo
        mask |= m
        match |= (val << lo) & m
    return mask, match


def _hex(v: int, width: int = 32) -> str:
    return f"0x{v:0{width // 4}x}"


_INSTRS: list[dict] = []


def _add(name, ext, fmt, fields, operands, syntax, desc, *, width=32, pseudo=False):
    mask, match = _bits(fields)
    _INSTRS.append({
        "id": f"{ext}::{name}",
        "name": name,
        "mnemonic": name,
        "extension": ext,
        "format": fmt,
        "width": width,
        "mask": _hex(mask, width),
        "match": _hex(match, width),
        "operands": operands,
        "syntax": syntax,
        "description": desc,
        "pseudo": pseudo,
    })


OPC = 6, 0          # opcode bits [6:0]
F3 = 14, 12         # funct3 [14:12]
F7 = 31, 25         # funct7 [31:25]


def R(name, ext, opcode, f3, f7, desc, operands="rd, rs1, rs2"):
    _add(name, ext, "R", [(*OPC, opcode), (*F3, f3), (*F7, f7)],
         operands, f"{name} {operands}", desc)


def I(name, ext, opcode, f3, desc, operands="rd, rs1, imm"):
    _add(name, ext, "I", [(*OPC, opcode), (*F3, f3)],
         operands, f"{name} {operands}", desc)


def Ishift(name, ext, opcode, f3, upper, desc, *, upper_hi=31, upper_lo=26):
    # shift-amount immediates: low bits are shamt, upper field is constant
    _add(name, ext, "I", [(*OPC, opcode), (*F3, f3), (upper_hi, upper_lo, upper)],
         "rd, rs1, shamt", f"{name} rd, rs1, shamt", desc)


def Iload(name, ext, opcode, f3, desc):
    _add(name, ext, "I", [(*OPC, opcode), (*F3, f3)],
         "rd, imm(rs1)", f"{name} rd, imm(rs1)", desc)


def S(name, ext, opcode, f3, desc):
    _add(name, ext, "S", [(*OPC, opcode), (*F3, f3)],
         "rs2, imm(rs1)", f"{name} rs2, imm(rs1)", desc)


def B(name, ext, opcode, f3, desc):
    _add(name, ext, "B", [(*OPC, opcode), (*F3, f3)],
         "rs1, rs2, label", f"{name} rs1, rs2, label", desc)


def U(name, ext, opcode, desc):
    _add(name, ext, "U", [(*OPC, opcode)],
         "rd, imm", f"{name} rd, imm", desc)


def J(name, ext, opcode, desc, operands="rd, label"):
    _add(name, ext, "J", [(*OPC, opcode)],
         operands, f"{name} {operands}", desc)


def SYS(name, ext, funct12, desc):
    # opcode 0x73, funct3 0, rs1/rd 0, funct12 fixed in [31:20]
    _add(name, ext, "I", [(*OPC, 0x73), (*F3, 0), (31, 20, funct12),
                          (19, 15, 0), (11, 7, 0)],
         "", name, desc)


def CSR(name, ext, f3, desc, operands="rd, csr, rs1"):
    _add(name, ext, "I", [(*OPC, 0x73), (*F3, f3)],
         operands, f"{name} {operands}", desc)


def AMO(name, ext, width_f3, funct5, desc):
    # opcode 0x2f, funct3=width, funct5 in [31:27]; aq/rl [26:25] variable
    _add(name, ext, "R", [(*OPC, 0x2f), (*F3, width_f3), (31, 27, funct5)],
         "rd, rs2, (rs1)", f"{name} rd, rs2, (rs1)", desc)


def LR(name, ext, width_f3, desc):
    _add(name, ext, "R", [(*OPC, 0x2f), (*F3, width_f3), (31, 27, 0x02),
                          (24, 20, 0)],
         "rd, (rs1)", f"{name} rd, (rs1)", desc)


def FPR(name, ext, f7, desc, operands="rd, rs1, rs2"):
    # FP op, opcode 0x53, funct7 fixed, rm in funct3 (variable) unless fixed
    _add(name, ext, "R", [(*OPC, 0x53), (*F7, f7)],
         operands, f"{name} {operands}", desc)


def FPRf3(name, ext, f7, f3, desc, operands="rd, rs1, rs2"):
    _add(name, ext, "R", [(*OPC, 0x53), (*F7, f7), (*F3, f3)],
         operands, f"{name} {operands}", desc)


# ===========================================================================
# RV32I base integer
# ===========================================================================
U("lui", "RV32I", 0x37, "Load upper immediate: rd = imm << 12.")
U("auipc", "RV32I", 0x17, "Add upper immediate to pc: rd = pc + (imm << 12).")
J("jal", "RV32I", 0x6f, "Jump and link: rd = pc+4; pc += imm.")
_add("jalr", "RV32I", "I", [(*OPC, 0x67), (*F3, 0)],
     "rd, rs1, imm", "jalr rd, imm(rs1)",
     "Jump and link register: rd = pc+4; pc = (rs1+imm) & ~1.")
for nm, f3, d in [("beq", 0, "branch if equal"), ("bne", 1, "branch if not equal"),
                  ("blt", 4, "branch if less than (signed)"),
                  ("bge", 5, "branch if greater-or-equal (signed)"),
                  ("bltu", 6, "branch if less than (unsigned)"),
                  ("bgeu", 7, "branch if greater-or-equal (unsigned)")]:
    B(nm, "RV32I", 0x63, f3, f"Conditional {d}: if (rs1 ? rs2) pc += imm.")
for nm, f3, d in [("lb", 0, "load byte (sign-extended)"),
                  ("lh", 1, "load halfword (sign-extended)"),
                  ("lw", 2, "load word"),
                  ("lbu", 4, "load byte (zero-extended)"),
                  ("lhu", 5, "load halfword (zero-extended)")]:
    Iload(nm, "RV32I", 0x03, f3, f"{d.capitalize()}: rd = mem[rs1+imm].")
for nm, f3, d in [("sb", 0, "store byte"), ("sh", 1, "store halfword"),
                  ("sw", 2, "store word")]:
    S(nm, "RV32I", 0x23, f3, f"{d.capitalize()}: mem[rs1+imm] = rs2.")
for nm, f3, d in [("addi", 0, "rd = rs1 + imm"),
                  ("slti", 2, "rd = (rs1 < imm) signed"),
                  ("sltiu", 3, "rd = (rs1 < imm) unsigned"),
                  ("xori", 4, "rd = rs1 ^ imm"),
                  ("ori", 6, "rd = rs1 | imm"),
                  ("andi", 7, "rd = rs1 & imm")]:
    I(nm, "RV32I", 0x13, f3, f"Immediate ALU op: {d}.")
Ishift("slli", "RV32I", 0x13, 1, 0x00, "Shift left logical immediate.")
Ishift("srli", "RV32I", 0x13, 5, 0x00, "Shift right logical immediate.")
Ishift("srai", "RV32I", 0x13, 5, 0x10, "Shift right arithmetic immediate.")
for nm, f3, f7, d in [("add", 0, 0x00, "rd = rs1 + rs2"),
                      ("sub", 0, 0x20, "rd = rs1 - rs2"),
                      ("sll", 1, 0x00, "shift left logical"),
                      ("slt", 2, 0x00, "set less than (signed)"),
                      ("sltu", 3, 0x00, "set less than (unsigned)"),
                      ("xor", 4, 0x00, "rd = rs1 ^ rs2"),
                      ("srl", 5, 0x00, "shift right logical"),
                      ("sra", 5, 0x20, "shift right arithmetic"),
                      ("or", 6, 0x00, "rd = rs1 | rs2"),
                      ("and", 7, 0x00, "rd = rs1 & rs2")]:
    R(nm, "RV32I", 0x33, f3, f7, f"Register ALU op: {d}.")
_add("fence", "RV32I", "I", [(*OPC, 0x0f), (*F3, 0)],
     "pred, succ", "fence pred, succ",
     "Order device I/O and memory accesses across the fence.")
SYS("ecall", "RV32I", 0x000, "Environment call: trap to the execution environment.")
SYS("ebreak", "RV32I", 0x001, "Breakpoint: trap to a debugger.")

# ===========================================================================
# RV64I additions
# ===========================================================================
Iload("lwu", "RV64I", 0x03, 6, "Load word (zero-extended) into rd.")
Iload("ld", "RV64I", 0x03, 3, "Load doubleword: rd = mem[rs1+imm].")
S("sd", "RV64I", 0x23, 3, "Store doubleword: mem[rs1+imm] = rs2.")
I("addiw", "RV64I", 0x1b, 0, "32-bit add immediate, sign-extended to 64.")
Ishift("slliw", "RV64I", 0x1b, 1, 0x00, "32-bit shift left logical immediate.",
       upper_hi=31, upper_lo=25)
Ishift("srliw", "RV64I", 0x1b, 5, 0x00, "32-bit shift right logical immediate.",
       upper_hi=31, upper_lo=25)
Ishift("sraiw", "RV64I", 0x1b, 5, 0x20, "32-bit shift right arithmetic immediate.",
       upper_hi=31, upper_lo=25)
for nm, f3, f7, d in [("addw", 0, 0x00, "32-bit add"),
                      ("subw", 0, 0x20, "32-bit subtract"),
                      ("sllw", 1, 0x00, "32-bit shift left logical"),
                      ("srlw", 5, 0x00, "32-bit shift right logical"),
                      ("sraw", 5, 0x20, "32-bit shift right arithmetic")]:
    R(nm, "RV64I", 0x3b, f3, f7, f"{d}, result sign-extended to 64 bits.")

# ===========================================================================
# Zicsr
# ===========================================================================
CSR("csrrw", "Zicsr", 1, "Atomic read/write CSR: rd = csr; csr = rs1.")
CSR("csrrs", "Zicsr", 2, "Atomic read & set bits: rd = csr; csr |= rs1.")
CSR("csrrc", "Zicsr", 3, "Atomic read & clear bits: rd = csr; csr &= ~rs1.")
CSR("csrrwi", "Zicsr", 5, "Atomic read/write CSR immediate.", operands="rd, csr, zimm")
CSR("csrrsi", "Zicsr", 6, "Atomic read & set bits immediate.", operands="rd, csr, zimm")
CSR("csrrci", "Zicsr", 7, "Atomic read & clear bits immediate.", operands="rd, csr, zimm")

# ===========================================================================
# Zifencei
# ===========================================================================
_add("fence.i", "Zifencei", "I", [(*OPC, 0x0f), (*F3, 1)],
     "", "fence.i", "Synchronize the instruction and data streams.")

# ===========================================================================
# M — multiply / divide
# ===========================================================================
for nm, f3, d in [("mul", 0, "low XLEN bits of rs1*rs2"),
                  ("mulh", 1, "high bits of signed*signed"),
                  ("mulhsu", 2, "high bits of signed*unsigned"),
                  ("mulhu", 3, "high bits of unsigned*unsigned"),
                  ("div", 4, "signed division"),
                  ("divu", 5, "unsigned division"),
                  ("rem", 6, "signed remainder"),
                  ("remu", 7, "unsigned remainder")]:
    R(nm, "M", 0x33, f3, 0x01, f"Multiply/divide: {d}.")
for nm, f3, d in [("mulw", 0, "32-bit multiply"), ("divw", 4, "32-bit signed div"),
                  ("divuw", 5, "32-bit unsigned div"), ("remw", 6, "32-bit signed rem"),
                  ("remuw", 7, "32-bit unsigned rem")]:
    R(nm, "M", 0x3b, f3, 0x01, f"{d}, result sign-extended to 64 bits.")

# ===========================================================================
# A — atomics (.w and .d)
# ===========================================================================
_AMO = [("amoswap", 0x01, "swap"), ("amoadd", 0x00, "add"), ("amoxor", 0x04, "xor"),
        ("amoand", 0x0c, "and"), ("amoor", 0x08, "or"), ("amomin", 0x10, "min signed"),
        ("amomax", 0x14, "max signed"), ("amominu", 0x18, "min unsigned"),
        ("amomaxu", 0x1c, "max unsigned")]
LR("lr.w", "A", 2, "Load-reserved word.")
_add("sc.w", "A", "R", [(*OPC, 0x2f), (*F3, 2), (31, 27, 0x03)],
     "rd, rs2, (rs1)", "sc.w rd, rs2, (rs1)", "Store-conditional word.")
LR("lr.d", "A", 3, "Load-reserved doubleword.")
_add("sc.d", "A", "R", [(*OPC, 0x2f), (*F3, 3), (31, 27, 0x03)],
     "rd, rs2, (rs1)", "sc.d rd, rs2, (rs1)", "Store-conditional doubleword.")
for base, f5, d in _AMO:
    AMO(f"{base}.w", "A", 2, f5, f"Atomic memory {d} (word).")
    AMO(f"{base}.d", "A", 3, f5, f"Atomic memory {d} (doubleword).")

# ===========================================================================
# F — single-precision float
# ===========================================================================
Iload("flw", "F", 0x07, 2, "Load single-precision float into fd.")
S("fsw", "F", 0x27, 2, "Store single-precision float fs2.")
for nm, f7, d in [("fadd.s", 0x00, "add"), ("fsub.s", 0x04, "subtract"),
                  ("fmul.s", 0x08, "multiply"), ("fdiv.s", 0x0c, "divide")]:
    FPR(nm, "F", f7, f"Single-precision floating-point {d}.")
_add("fsqrt.s", "F", "R", [(*OPC, 0x53), (*F7, 0x2c), (24, 20, 0)],
     "rd, rs1", "fsqrt.s rd, rs1", "Single-precision square root.")
FPRf3("fsgnj.s", "F", 0x10, 0, "Sign-inject: copy rs1 with sign of rs2.")
FPRf3("fsgnjn.s", "F", 0x10, 1, "Sign-inject negated.")
FPRf3("fsgnjx.s", "F", 0x10, 2, "Sign-inject xor.")
FPRf3("fmin.s", "F", 0x14, 0, "Single-precision minimum.")
FPRf3("fmax.s", "F", 0x14, 1, "Single-precision maximum.")
FPRf3("feq.s", "F", 0x50, 2, "Set if equal.", operands="rd, rs1, rs2")
FPRf3("flt.s", "F", 0x50, 1, "Set if less than.", operands="rd, rs1, rs2")
FPRf3("fle.s", "F", 0x50, 0, "Set if less-or-equal.", operands="rd, rs1, rs2")
FPRf3("fclass.s", "F", 0x70, 1, "Classify the float into a 10-bit mask.",
      operands="rd, rs1")

# ===========================================================================
# D — double-precision float
# ===========================================================================
Iload("fld", "D", 0x07, 3, "Load double-precision float into fd.")
S("fsd", "D", 0x27, 3, "Store double-precision float fs2.")
for nm, f7, d in [("fadd.d", 0x01, "add"), ("fsub.d", 0x05, "subtract"),
                  ("fmul.d", 0x09, "multiply"), ("fdiv.d", 0x0d, "divide")]:
    FPR(nm, "D", f7, f"Double-precision floating-point {d}.")
_add("fsqrt.d", "D", "R", [(*OPC, 0x53), (*F7, 0x2d), (24, 20, 0)],
     "rd, rs1", "fsqrt.d rd, rs1", "Double-precision square root.")
FPRf3("fmin.d", "D", 0x15, 0, "Double-precision minimum.")
FPRf3("fmax.d", "D", 0x15, 1, "Double-precision maximum.")
FPRf3("feq.d", "D", 0x51, 2, "Set if equal (double).", operands="rd, rs1, rs2")
FPRf3("flt.d", "D", 0x51, 1, "Set if less than (double).", operands="rd, rs1, rs2")
FPRf3("fle.d", "D", 0x51, 0, "Set if less-or-equal (double).", operands="rd, rs1, rs2")

# ===========================================================================
# C — compressed (16-bit). mask/match over a 16-bit encoding.
#   quadrant = op[1:0], funct3 = [15:13].
# ===========================================================================
def C(name, funct3, quadrant, fields_extra, operands, desc):
    fields = [(1, 0, quadrant), (15, 13, funct3)] + fields_extra
    _add(name, "C", "C", fields, operands, f"{name} {operands}", desc, width=16)


C("c.addi4spn", 0, 0, [], "rd', nzuimm", "rd' = sp + nzuimm (expands to addi).")
C("c.lw", 2, 0, [], "rd', uimm(rs1')", "Load word (compressed).")
C("c.sw", 6, 0, [], "rs2', uimm(rs1')", "Store word (compressed).")
C("c.nop", 0, 1, [(12, 2, 0)], "", "No-op (expands to addi x0,x0,0).")
C("c.addi", 0, 1, [], "rd, nzimm", "rd += nzimm (compressed).")
C("c.li", 2, 1, [], "rd, imm", "rd = imm (expands to addi rd,x0,imm).")
C("c.lui", 3, 1, [], "rd, nzimm", "rd = nzimm<<12 (compressed lui).")
C("c.j", 5, 1, [], "label", "Unconditional jump (compressed).")
C("c.beqz", 6, 1, [], "rs1', label", "Branch if rs1' == 0 (compressed).")
C("c.bnez", 7, 1, [], "rs1', label", "Branch if rs1' != 0 (compressed).")
C("c.slli", 0, 2, [], "rd, shamt", "Shift left logical immediate (compressed).")
C("c.lwsp", 2, 2, [], "rd, uimm(sp)", "Load word from stack pointer.")
C("c.swsp", 6, 2, [], "rs2, uimm(sp)", "Store word to stack pointer.")
_add("c.jr", "C", "C", [(1, 0, 2), (15, 13, 4), (12, 12, 0), (6, 2, 0)],
     "rs1", "c.jr rs1", "Jump register (expands to jalr x0,0(rs1)).", width=16)
_add("c.mv", "C", "C", [(1, 0, 2), (15, 13, 4), (12, 12, 0)],
     "rd, rs2", "c.mv rd, rs2", "rd = rs2 (expands to add rd,x0,rs2).", width=16)
_add("c.ebreak", "C", "C", [(1, 0, 2), (15, 13, 4), (12, 12, 1), (11, 2, 0)],
     "", "c.ebreak", "Breakpoint (compressed).", width=16)
_add("c.jalr", "C", "C", [(1, 0, 2), (15, 13, 4), (12, 12, 1), (6, 2, 0)],
     "rs1", "c.jalr rs1", "Jump and link register (compressed).", width=16)
_add("c.add", "C", "C", [(1, 0, 2), (15, 13, 4), (12, 12, 1)],
     "rd, rs2", "c.add rd, rs2", "rd += rs2 (compressed).", width=16)


# ===========================================================================
# Registers — GPR x0-x31 + FPR f0-f31
# ===========================================================================
_GPR = [
    ("x0", "zero", "Hard-wired zero", "—"),
    ("x1", "ra", "Return address", "Caller"),
    ("x2", "sp", "Stack pointer", "Callee"),
    ("x3", "gp", "Global pointer", "—"),
    ("x4", "tp", "Thread pointer", "—"),
    ("x5", "t0", "Temporary / alternate link", "Caller"),
    ("x6", "t1", "Temporary", "Caller"),
    ("x7", "t2", "Temporary", "Caller"),
    ("x8", "s0", "Saved register / frame pointer (fp)", "Callee"),
    ("x9", "s1", "Saved register", "Callee"),
    ("x10", "a0", "Function argument / return value 0", "Caller"),
    ("x11", "a1", "Function argument / return value 1", "Caller"),
    ("x12", "a2", "Function argument 2", "Caller"),
    ("x13", "a3", "Function argument 3", "Caller"),
    ("x14", "a4", "Function argument 4", "Caller"),
    ("x15", "a5", "Function argument 5", "Caller"),
    ("x16", "a6", "Function argument 6", "Caller"),
    ("x17", "a7", "Function argument 7 / syscall number", "Caller"),
    ("x18", "s2", "Saved register", "Callee"),
    ("x19", "s3", "Saved register", "Callee"),
    ("x20", "s4", "Saved register", "Callee"),
    ("x21", "s5", "Saved register", "Callee"),
    ("x22", "s6", "Saved register", "Callee"),
    ("x23", "s7", "Saved register", "Callee"),
    ("x24", "s8", "Saved register", "Callee"),
    ("x25", "s9", "Saved register", "Callee"),
    ("x26", "s10", "Saved register", "Callee"),
    ("x27", "s11", "Saved register", "Callee"),
    ("x28", "t3", "Temporary", "Caller"),
    ("x29", "t4", "Temporary", "Caller"),
    ("x30", "t5", "Temporary", "Caller"),
    ("x31", "t6", "Temporary", "Caller"),
]
_FABI = (["ft0", "ft1", "ft2", "ft3", "ft4", "ft5", "ft6", "ft7"]
         + ["fs0", "fs1"] + [f"fa{i}" for i in range(8)]
         + [f"fs{i}" for i in range(2, 12)] + ["ft8", "ft9", "ft10", "ft11"])
_FSAVE = (["Caller"] * 8 + ["Callee"] * 2 + ["Caller"] * 8
          + ["Callee"] * 10 + ["Caller"] * 4)


def _registers() -> list[dict]:
    rows = []
    for i, (name, abi, role, saver) in enumerate(_GPR):
        rows.append({"id": name, "name": name, "abi": abi, "number": i,
                     "group": "GPR", "role": role, "saver": saver})
    for i, abi in enumerate(_FABI):
        name = f"f{i}"
        rows.append({"id": name, "name": name, "abi": abi, "number": i,
                     "group": "FPR", "role": "Floating-point register",
                     "saver": _FSAVE[i]})
    return rows


# ===========================================================================
# CSRs — common machine / supervisor / user control & status registers
# ===========================================================================
_CSRS = [
    # User floating-point
    (0x001, "fflags", "U", "RW", "Floating-point accrued exception flags."),
    (0x002, "frm", "U", "RW", "Floating-point dynamic rounding mode."),
    (0x003, "fcsr", "U", "RW", "Floating-point control and status register."),
    # User counters
    (0xC00, "cycle", "U", "RO", "Cycle counter for RDCYCLE."),
    (0xC01, "time", "U", "RO", "Wall-clock time for RDTIME."),
    (0xC02, "instret", "U", "RO", "Instructions-retired counter for RDINSTRET."),
    # Supervisor
    (0x100, "sstatus", "S", "RW", "Supervisor status register."),
    (0x104, "sie", "S", "RW", "Supervisor interrupt-enable register."),
    (0x105, "stvec", "S", "RW", "Supervisor trap-handler base address."),
    (0x106, "scounteren", "S", "RW", "Supervisor counter enable."),
    (0x140, "sscratch", "S", "RW", "Scratch register for supervisor trap handlers."),
    (0x141, "sepc", "S", "RW", "Supervisor exception program counter."),
    (0x142, "scause", "S", "RW", "Supervisor trap cause."),
    (0x143, "stval", "S", "RW", "Supervisor bad address or instruction."),
    (0x144, "sip", "S", "RW", "Supervisor interrupt pending."),
    (0x180, "satp", "S", "RW", "Supervisor address translation and protection."),
    # Machine information
    (0xF11, "mvendorid", "M", "RO", "Vendor ID."),
    (0xF12, "marchid", "M", "RO", "Architecture ID."),
    (0xF13, "mimpid", "M", "RO", "Implementation ID."),
    (0xF14, "mhartid", "M", "RO", "Hardware thread ID."),
    # Machine trap setup
    (0x300, "mstatus", "M", "RW", "Machine status register."),
    (0x301, "misa", "M", "RW", "ISA and extensions."),
    (0x302, "medeleg", "M", "RW", "Machine exception delegation."),
    (0x303, "mideleg", "M", "RW", "Machine interrupt delegation."),
    (0x304, "mie", "M", "RW", "Machine interrupt-enable register."),
    (0x305, "mtvec", "M", "RW", "Machine trap-handler base address."),
    (0x306, "mcounteren", "M", "RW", "Machine counter enable."),
    # Machine trap handling
    (0x340, "mscratch", "M", "RW", "Scratch register for machine trap handlers."),
    (0x341, "mepc", "M", "RW", "Machine exception program counter."),
    (0x342, "mcause", "M", "RW", "Machine trap cause."),
    (0x343, "mtval", "M", "RW", "Machine bad address or instruction."),
    (0x344, "mip", "M", "RW", "Machine interrupt pending."),
    # Physical memory protection
    (0x3A0, "pmpcfg0", "M", "RW", "Physical memory protection configuration."),
    (0x3B0, "pmpaddr0", "M", "RW", "Physical memory protection address register 0."),
    # Debug
    (0x7B0, "dcsr", "M", "RW", "Debug control and status register."),
    (0x7B1, "dpc", "M", "RW", "Debug program counter."),
]


def _csrs() -> list[dict]:
    return [{"id": f"0x{addr:03x}", "addr": f"0x{addr:03x}", "name": name,
             "privilege": priv, "access": acc, "description": desc}
            for addr, name, priv, acc, desc in _CSRS]


# ===========================================================================
# Extensions
# ===========================================================================
_EXT = [
    ("I", "Base Integer Instruction Set", "2.1",
     "Loads/stores, integer ALU, branches, jumps — the mandatory base."),
    ("M", "Integer Multiplication and Division", "2.0",
     "mul/mulh/div/rem family."),
    ("A", "Atomic Instructions", "2.1",
     "Load-reserved/store-conditional and atomic memory operations."),
    ("F", "Single-Precision Floating-Point", "2.2",
     "32-bit IEEE-754 float register file and ops."),
    ("D", "Double-Precision Floating-Point", "2.2",
     "64-bit IEEE-754 float, extends F."),
    ("C", "Compressed Instructions", "2.0",
     "16-bit encodings for common instructions to cut code size."),
    ("Zicsr", "Control and Status Register Access", "2.0",
     "csrrw/csrrs/csrrc and immediate forms."),
    ("Zifencei", "Instruction-Fetch Fence", "2.0",
     "fence.i to synchronize instruction and data streams."),
    ("G", "General-purpose (IMAFD + Zicsr + Zifencei)", "—",
     "Shorthand for the common general-purpose ISA bundle."),
]


def _extensions() -> list[dict]:
    return [{"id": letter, "letter": letter, "name": name, "version": ver,
             "description": desc} for letter, name, ver, desc in _EXT]


# ===========================================================================
# Pseudo-instructions
# ===========================================================================
_PSEUDO = [
    ("nop", "addi x0, x0, 0", "addi", "No operation."),
    ("li", "lui/addi sequence", "addi", "Load immediate into rd."),
    ("mv", "addi rd, rs1, 0", "addi", "Copy register rs1 to rd."),
    ("not", "xori rd, rs1, -1", "xori", "Bitwise NOT."),
    ("neg", "sub rd, x0, rs1", "sub", "Two's-complement negate."),
    ("seqz", "sltiu rd, rs1, 1", "sltiu", "Set rd = (rs1 == 0)."),
    ("snez", "sltu rd, x0, rs1", "sltu", "Set rd = (rs1 != 0)."),
    ("j", "jal x0, label", "jal", "Unconditional jump."),
    ("jr", "jalr x0, 0(rs1)", "jalr", "Jump to register."),
    ("ret", "jalr x0, 0(ra)", "jalr", "Return from subroutine."),
    ("call", "auipc/jalr sequence", "jalr", "Call a far subroutine."),
    ("tail", "auipc/jalr sequence", "jalr", "Tail-call a far subroutine."),
    ("beqz", "beq rs1, x0, label", "beq", "Branch if rs1 == 0."),
    ("bnez", "bne rs1, x0, label", "bne", "Branch if rs1 != 0."),
    ("blez", "bge x0, rs1, label", "bge", "Branch if rs1 <= 0."),
    ("bgez", "bge rs1, x0, label", "bge", "Branch if rs1 >= 0."),
    ("bltz", "blt rs1, x0, label", "blt", "Branch if rs1 < 0."),
    ("bgtz", "blt x0, rs1, label", "blt", "Branch if rs1 > 0."),
    ("csrr", "csrrs rd, csr, x0", "csrrs", "Read a CSR into rd."),
    ("csrw", "csrrw x0, csr, rs1", "csrrw", "Write rs1 to a CSR."),
    ("fence", "fence iorw, iorw", "fence", "Full memory fence."),
]


def _pseudo() -> list[dict]:
    return [{"id": name, "name": name, "expansion": exp, "base": base,
             "description": desc} for name, exp, base, desc in _PSEUDO]


# ===========================================================================
# Emit
# ===========================================================================
def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    datasets = {
        "instructions": _INSTRS,
        "registers": _registers(),
        "csrs": _csrs(),
        "extensions": _extensions(),
        "pseudo": _pseudo(),
    }
    for name, rows in datasets.items():
        path = OUT / f"{name}.json"
        path.write_text(json.dumps(
            {"spec_version": SPEC_VERSION, "count": len(rows), name: rows},
            indent=2, sort_keys=False) + "\n", encoding="utf-8")
        print(f"wrote {path.relative_to(OUT.parents[2])}  ({len(rows)} rows)")


if __name__ == "__main__":
    main()
