"""Parse RISC-V Linux kernel *architecture* facts into searchable records.

Four datasets, all parsed from a real kernel source tree (no kernel internals /
driver implementation — only the architecture/ABI surfaces useful when reading a
RISC-V trace):

* ``syscalls``  — syscall number (the value in ``a7`` at an ``ecall``) -> name +
  entry point, from ``include/uapi/asm-generic/unistd.h`` (RISC-V is a
  generic-unistd architecture).
* ``traps``     — scause/mcause exception + interrupt cause codes, from
  ``arch/riscv/include/asm/csr.h`` (``EXC_*`` / ``IRQ_*``).
* ``sbi``       — Supervisor Binary Interface extension IDs + function IDs (the
  S<->M ABI), from ``arch/riscv/include/asm/sbi.h``.
* ``memmap``    — virtual-memory layout regions (Sv39/Sv48/Sv57) + the boot
  register ABI, from ``Documentation/arch/riscv/{vm-layout,boot}.rst``.

``parse_tree(root)`` reads these from any kernel checkout; ``load(data_dir)``
reads the bundled JSON snapshot emitted by ``scripts/gen_kernel_data.py``. This
module is dependency-free (no ``opensearch-py``), like the other parsers.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any

CATEGORIES: tuple[str, ...] = ("syscalls", "traps", "sbi", "memmap")

# Source files within a kernel tree (new Documentation/arch/ layout, with a
# fallback to the pre-6.8 Documentation/riscv/ location).
SRC = {
    "syscalls": "include/uapi/asm-generic/unistd.h",
    "traps": "arch/riscv/include/asm/csr.h",
    "sbi": "arch/riscv/include/asm/sbi.h",
    "vm_layout": "Documentation/arch/riscv/vm-layout.rst",
    "boot": "Documentation/arch/riscv/boot.rst",
}
_DOC_FALLBACK = {
    "Documentation/arch/riscv/vm-layout.rst": "Documentation/riscv/vm-layout.rst",
    "Documentation/arch/riscv/boot.rst": "Documentation/riscv/boot.rst",
}


@dataclass
class Syscall:
    id: str
    nr: int
    name: str
    entry: str
    abi: str
    description: str


@dataclass
class Trap:
    id: str
    code: int
    kind: str          # exception | interrupt
    name: str          # EXC_SYSCALL / IRQ_S_TIMER
    label: str         # humanized
    description: str


@dataclass
class SbiCall:
    id: str
    kind: str          # extension | function
    extension: str     # TIME / IPI / HSM / ...
    name: str          # SBI_EXT_HSM_HART_START
    eid: str           # hex extension id (extensions only)
    fid: int           # function id (functions only; -1 for extensions)
    description: str


@dataclass
class MemRegion:
    id: str
    category: str      # vm-layout | boot-abi
    mode: str          # Sv39 / Sv48 / Sv57 / "" (boot-abi)
    region: str        # short name (vmalloc, kernel, a0, ...)
    start: str
    end: str
    size: str
    description: str


@dataclass
class KernelData:
    kernel_version: str
    syscalls: list[Syscall]
    traps: list[Trap]
    sbi: list[SbiCall]
    memmap: list[MemRegion]

    def counts(self) -> dict[str, int]:
        return {c: len(getattr(self, c)) for c in CATEGORIES}


# --------------------------------------------------------------------------- #
# Curated descriptions (the headers carry no prose; humanize + a small map).
# --------------------------------------------------------------------------- #
_SYS_DESC = {
    "read": "Read from a file descriptor.", "write": "Write to a file descriptor.",
    "openat": "Open a file relative to a directory fd.", "close": "Close a fd.",
    "lseek": "Reposition a file offset.", "mmap": "Map files/devices into memory.",
    "munmap": "Unmap a memory region.", "mprotect": "Set memory protection.",
    "brk": "Change the program break (heap end).", "ioctl": "Device control.",
    "clone": "Create a child process/thread.", "execve": "Execute a program.",
    "exit": "Terminate the calling thread.", "exit_group": "Terminate all threads.",
    "wait4": "Wait for process state change.", "kill": "Send a signal to a process.",
    "fork": "Create a child process.", "futex": "Fast user-space locking.",
    "nanosleep": "High-resolution sleep.", "clock_gettime": "Read a clock.",
    "gettimeofday": "Get time of day.", "getpid": "Get process ID.",
    "getppid": "Get parent process ID.", "getuid": "Get real user ID.",
    "geteuid": "Get effective user ID.", "fstat": "Get file status by fd.",
    "newfstatat": "Get file status relative to a dir fd.", "statx": "Extended file status.",
    "pipe2": "Create a pipe.", "dup": "Duplicate a fd.", "dup3": "Duplicate a fd (flags).",
    "socket": "Create a socket.", "connect": "Connect a socket.",
    "sendto": "Send on a socket.", "recvfrom": "Receive on a socket.",
    "rt_sigaction": "Set a signal handler.", "rt_sigprocmask": "Change blocked signals.",
    "rt_sigreturn": "Return from a signal handler.", "set_tid_address": "Set clear_child_tid.",
    "tgkill": "Send a signal to a thread.", "prlimit64": "Get/set resource limits.",
    "getrandom": "Fill a buffer with random bytes.", "membarrier": "Issue memory barriers.",
    "faccessat": "Check file access relative to a dir fd.",
    "readlinkat": "Read a symlink relative to a dir fd.",
    "mkdirat": "Create a directory relative to a dir fd.",
    "renameat2": "Rename a file (flags).", "unlinkat": "Remove a directory entry.",
}
_TRAP_DESC = {
    "EXC_INST_MISALIGNED": "Instruction address misaligned.",
    "EXC_INST_ACCESS": "Instruction access fault.",
    "EXC_INST_ILLEGAL": "Illegal instruction.",
    "EXC_BREAKPOINT": "Breakpoint (ebreak).",
    "EXC_LOAD_MISALIGNED": "Load address misaligned.",
    "EXC_LOAD_ACCESS": "Load access fault.",
    "EXC_STORE_MISALIGNED": "Store/AMO address misaligned.",
    "EXC_STORE_ACCESS": "Store/AMO access fault.",
    "EXC_SYSCALL": "Environment call (ecall) from U-mode.",
    "EXC_HYPERVISOR_SYSCALL": "Environment call from VS-mode.",
    "EXC_SUPERVISOR_SYSCALL": "Environment call from S-mode.",
    "EXC_INST_PAGE_FAULT": "Instruction page fault.",
    "EXC_LOAD_PAGE_FAULT": "Load page fault.",
    "EXC_STORE_PAGE_FAULT": "Store/AMO page fault.",
    "EXC_INST_GUEST_PAGE_FAULT": "Instruction guest-page fault.",
    "EXC_LOAD_GUEST_PAGE_FAULT": "Load guest-page fault.",
    "EXC_VIRTUAL_INST_FAULT": "Virtual instruction fault.",
    "EXC_STORE_GUEST_PAGE_FAULT": "Store/AMO guest-page fault.",
    "IRQ_S_SOFT": "Supervisor software interrupt.",
    "IRQ_VS_SOFT": "Virtual supervisor software interrupt.",
    "IRQ_M_SOFT": "Machine software interrupt.",
    "IRQ_S_TIMER": "Supervisor timer interrupt.",
    "IRQ_VS_TIMER": "Virtual supervisor timer interrupt.",
    "IRQ_M_TIMER": "Machine timer interrupt.",
    "IRQ_S_EXT": "Supervisor external interrupt.",
    "IRQ_VS_EXT": "Virtual supervisor external interrupt.",
    "IRQ_M_EXT": "Machine external interrupt.",
    "IRQ_S_GEXT": "Supervisor guest external interrupt.",
    "IRQ_PMU_OVF": "Performance counter overflow interrupt.",
}
_SBI_EXT_DESC = {
    "BASE": "Base extension (probe extensions, get impl/spec info).",
    "TIME": "Timer extension (set the timer).",
    "IPI": "Inter-processor interrupt extension.",
    "RFENCE": "Remote fence extension (remote TLB/instruction fences).",
    "HSM": "Hart state management (start/stop/suspend harts).",
    "SRST": "System reset (shutdown/cold/warm reboot).",
    "SUSP": "System suspend extension.",
    "PMU": "Performance monitoring unit extension.",
    "DBCN": "Debug console extension.",
    "STA": "Steal-time accounting extension.",
}


def _humanize(macro: str, *prefixes: str) -> str:
    s = macro
    for p in prefixes:
        if s.startswith(p):
            s = s[len(p):]
            break
    return s.replace("_", " ").strip().capitalize()


# --------------------------------------------------------------------------- #
# Parsers
# --------------------------------------------------------------------------- #
def _read(root: Path, rel: str) -> str:
    p = root / rel
    if not p.is_file() and rel in _DOC_FALLBACK:
        p = root / _DOC_FALLBACK[rel]
    return p.read_text(encoding="utf-8", errors="replace")


_RE_NR3264 = re.compile(r"#define\s+(__NR3264_\w+)\s+(\d+)")
_RE_NR = re.compile(r"#define\s+(__NR_\w+)\s+(\S+)")
_RE_SYSCALL = re.compile(r"__SYSCALL\(\s*(__NR_\w+)\s*,\s*(\w+)")
_RE_SC_3264 = re.compile(r"__SC_3264\(\s*(__NR_?\w+)\s*,\s*(\w+)\s*,\s*(\w+)")
_RE_SC_COMP = re.compile(r"__SC_COMP\w*\(\s*(__NR_\w+)\s*,\s*(\w+)\s*,\s*(\w+)")
_SKIP_NR = {"__NR_syscalls", "__NR_arch_specific_syscall"}


def parse_syscalls(root: Path) -> list[Syscall]:
    text = _read(root, SRC["syscalls"])
    three64 = {m.group(1): int(m.group(2)) for m in _RE_NR3264.finditer(text)}
    # entry map: name -> entry symbol (prefer the 64-bit / non-compat form)
    entry: dict[str, str] = {}
    for m in _RE_SYSCALL.finditer(text):
        entry.setdefault(m.group(1), m.group(2))
    for m in _RE_SC_3264.finditer(text):
        entry[m.group(1)] = m.group(3)          # 64-bit entry
    for m in _RE_SC_COMP.finditer(text):
        entry.setdefault(m.group(1), m.group(2))

    rows: list[Syscall] = []
    seen: set[int] = set()
    for m in _RE_NR.finditer(text):
        macro, val = m.group(1), m.group(2)
        if macro in _SKIP_NR or macro.startswith("__NR3264_"):
            continue
        if val.isdigit():
            nr = int(val)
        elif val in three64:
            nr = three64[val]
        else:
            continue
        if nr in seen:
            continue
        seen.add(nr)
        name = macro[len("__NR_"):]
        ent = entry.get(macro, f"sys_{name}")
        rows.append(Syscall(id=f"nr:{nr}", nr=nr, name=name, entry=ent,
                            abi="generic", description=_SYS_DESC.get(name, "")))
    rows.sort(key=lambda s: s.nr)
    return rows


_RE_DEFINE_INT = re.compile(r"#define\s+((?:EXC|IRQ)_\w+)\s+(\d+)\s*$", re.M)


def parse_traps(root: Path) -> list[Trap]:
    text = _read(root, SRC["traps"])
    rows: list[Trap] = []
    for m in _RE_DEFINE_INT.finditer(text):
        name, code = m.group(1), int(m.group(2))
        if name.endswith(("_MAX", "_MASK", "_FLAG")):
            continue
        kind = "exception" if name.startswith("EXC_") else "interrupt"
        rows.append(Trap(id=f"{kind}:{code}", code=code, kind=kind, name=name,
                         label=_humanize(name, "EXC_", "IRQ_"),
                         description=_TRAP_DESC.get(name, _humanize(name, "EXC_", "IRQ_"))))
    rows.sort(key=lambda t: (t.kind, t.code))
    return rows


_RE_ENUM = re.compile(r"enum\s+(\w+)\s*\{(.*?)\}\s*;", re.S)
_RE_EXTID = re.compile(r"(SBI_EXT_\w+)\s*=\s*(0x[0-9a-fA-F]+|\d+)")


def parse_sbi(root: Path) -> list[SbiCall]:
    text = _read(root, SRC["sbi"])
    rows: list[SbiCall] = []
    enums = {m.group(1): m.group(2) for m in _RE_ENUM.finditer(text)}

    # extension IDs
    body = enums.get("sbi_ext_id", "")
    for m in _RE_EXTID.finditer(body):
        name, val = m.group(1), m.group(2)
        if name.endswith(("_START", "_END")):
            continue
        eid = hex(int(val, 0))
        ext = name[len("SBI_EXT_"):]
        rows.append(SbiCall(id=f"ext:{ext}", kind="extension", extension=ext,
                            name=name, eid=eid, fid=-1,
                            description=_SBI_EXT_DESC.get(ext, _humanize(name, "SBI_EXT_"))))

    # function IDs: enum sbi_ext_<x>_fid { MEMBER [= n], ... }
    for ename, ebody in enums.items():
        mfid = re.fullmatch(r"sbi_ext_(\w+)_fid", ename)
        if not mfid:
            continue
        ext = mfid.group(1).upper()
        counter = 0
        for line in ebody.splitlines():
            mm = re.match(r"\s*(SBI_EXT_\w+)\s*(?:=\s*(\d+))?\s*,?", line)
            if not mm or not mm.group(1):
                continue
            fid = int(mm.group(2)) if mm.group(2) else counter
            counter = fid + 1
            rows.append(SbiCall(id=f"fn:{ext}:{fid}", kind="function", extension=ext,
                                name=mm.group(1), eid="", fid=fid,
                                description=_humanize(mm.group(1), f"SBI_EXT_{ext}_")))
    return rows


_RE_VM_MODE = re.compile(r"RISC-V Linux Kernel S[Vv](\d+)")
_RE_VM_ROW = re.compile(
    r"^\s*([0-9a-fA-F]{8,16})\s*\|([^|]*)\|\s*([0-9a-fA-F]{8,16})\s*\|([^|]*)\|\s*(.+?)\s*$")
_RE_BOOT = re.compile(r"^\s*\*\s*``\$?(\w+)``\s*(?:to contain|=)?\s*(.+?)\.?\s*$")


def parse_memmap(root: Path) -> list[MemRegion]:
    rows: list[MemRegion] = []
    # vm-layout regions, tagged by the current SvNN section
    mode = ""
    for line in _read(root, SRC["vm_layout"]).splitlines():
        mm = _RE_VM_MODE.search(line)
        if mm:
            mode = f"Sv{mm.group(1)}"
            continue
        r = _RE_VM_ROW.match(line)
        if not r:
            continue
        start, _off, end, size, desc = (g.strip() for g in r.groups())
        region = re.split(r"[ ,/]", desc, 1)[0] or desc
        rows.append(MemRegion(
            id=f"vm:{mode}:{start}", category="vm-layout", mode=mode, region=region,
            start="0x" + start.lower(), end="0x" + end.lower(),
            size=size, description=desc))
    # boot register ABI (a0/a1/satp ...)
    for line in _read(root, SRC["boot"]).splitlines():
        b = _RE_BOOT.match(line)
        if b and ("$" in line or b.group(1) in {"a0", "a1", "satp", "pc", "sp"}):
            reg = b.group(1)
            rows.append(MemRegion(
                id=f"boot:{reg}", category="boot-abi", mode="", region=reg,
                start="", end="", size="", description=b.group(2).strip()))
    return rows


def _version(root: Path) -> str:
    vf = root / ".xevdb_kernel_version"
    if vf.is_file():
        return vf.read_text(encoding="utf-8").strip()
    mk = root / "Makefile"
    if mk.is_file():
        head = mk.read_text(encoding="utf-8", errors="replace")[:400]
        v = dict(re.findall(r"^(VERSION|PATCHLEVEL|SUBLEVEL)\s*=\s*(\d+)", head, re.M))
        if {"VERSION", "PATCHLEVEL"} <= set(v):
            return f"v{v['VERSION']}.{v['PATCHLEVEL']}" + (
                f".{v['SUBLEVEL']}" if v.get("SUBLEVEL", "0") != "0" else "")
    return "unknown"


def parse_tree(root: str | Path) -> KernelData:
    root = Path(root)
    return KernelData(
        kernel_version=_version(root),
        syscalls=parse_syscalls(root),
        traps=parse_traps(root),
        sbi=parse_sbi(root),
        memmap=parse_memmap(root),
    )


# --------------------------------------------------------------------------- #
# Bundled-JSON load (for ingest without a kernel tree)
# --------------------------------------------------------------------------- #
_CTOR = {"syscalls": Syscall, "traps": Trap, "sbi": SbiCall, "memmap": MemRegion}


def load(data_dir: str | Path | None = None) -> KernelData:
    def read(cat: str) -> tuple[str, list[dict]]:
        if data_dir is not None:
            text = (Path(data_dir) / f"{cat}.json").read_text(encoding="utf-8")
        else:
            text = (resources.files("xevdb").joinpath("data", "kernel", f"{cat}.json")
                    .read_text(encoding="utf-8"))
        obj: dict[str, Any] = json.loads(text)
        return obj.get("kernel_version", ""), obj.get(cat, [])

    version = ""
    kw: dict[str, list[Any]] = {}
    for cat in CATEGORIES:
        v, rows = read(cat)
        version = version or v
        kw[cat] = [_CTOR[cat](**row) for row in rows]
    return KernelData(kernel_version=version, **kw)
