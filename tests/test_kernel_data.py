"""Kernel architecture data: bundled snapshot loads + parsers work on a tree."""
from __future__ import annotations

import textwrap

from xevdb import kernel


# ---------------- bundled snapshot ----------------

def test_load_bundled_counts_and_facts():
    d = kernel.load()
    assert d.kernel_version
    c = d.counts()
    assert c["syscalls"] >= 300 and c["traps"] >= 25
    assert c["sbi"] >= 30 and c["memmap"] >= 10

    sysc = {s.name: s for s in d.syscalls}
    assert sysc["write"].nr == 64 and sysc["openat"].nr == 56
    assert sysc["mmap"].nr == 222                  # resolved through __NR3264_mmap

    traps = {t.name: t for t in d.traps}
    assert traps["EXC_SYSCALL"].code == 8 and traps["EXC_SYSCALL"].kind == "exception"
    assert traps["EXC_LOAD_PAGE_FAULT"].code == 13
    assert traps["IRQ_S_TIMER"].kind == "interrupt"

    sbi_ext = {s.extension: s for s in d.sbi if s.kind == "extension"}
    assert sbi_ext["HSM"].eid == "0x48534d"
    hsm = [s for s in d.sbi if s.kind == "function" and s.extension == "HSM"]
    assert any(s.name == "SBI_EXT_HSM_HART_START" and s.fid == 0 for s in hsm)

    kern = [m for m in d.memmap if m.mode == "Sv39" and m.region == "kernel"]
    assert kern and kern[0].end == "0xffffffffffffffff"
    boot = {m.region: m for m in d.memmap if m.category == "boot-abi"}
    assert "a0" in boot and "hartid" in boot["a0"].description


def test_unique_ids():
    d = kernel.load()
    for cat in kernel.CATEGORIES:
        ids = [r.id for r in getattr(d, cat)]
        assert len(ids) == len(set(ids)), f"dup ids in {cat}"


# ---------------- parsers on a synthetic mini-tree ----------------

def _mini_tree(root):
    (root / "include/uapi/asm-generic").mkdir(parents=True)
    (root / "arch/riscv/include/asm").mkdir(parents=True)
    (root / "Documentation/arch/riscv").mkdir(parents=True)
    (root / "include/uapi/asm-generic/unistd.h").write_text(textwrap.dedent("""
        #define __NR_read 63
        __SYSCALL(__NR_read, sys_read)
        #define __NR_write 64
        __SYSCALL(__NR_write, sys_write)
        #define __NR3264_mmap 222
        #define __NR_mmap __NR3264_mmap
        __SC_3264(__NR_mmap, sys_mmap2, sys_mmap)
        #define __NR_syscalls 463
    """))
    (root / "arch/riscv/include/asm/csr.h").write_text(textwrap.dedent("""
        #define IRQ_S_TIMER 5
        #define EXC_INST_MISALIGNED 0
        #define EXC_SYSCALL 8
        #define EXC_LOAD_PAGE_FAULT 13
        #define IRQ_LOCAL_MAX (IRQ_PMU_OVF + 1)
    """))
    (root / "arch/riscv/include/asm/sbi.h").write_text(textwrap.dedent("""
        enum sbi_ext_id {
            SBI_EXT_BASE = 0x10,
            SBI_EXT_HSM = 0x48534D,
            SBI_EXT_VENDOR_START = 0x09000000,
        };
        enum sbi_ext_hsm_fid {
            SBI_EXT_HSM_HART_START = 0,
            SBI_EXT_HSM_HART_STOP,
            SBI_EXT_HSM_HART_STATUS,
        };
    """))
    (root / "Documentation/arch/riscv/vm-layout.rst").write_text(textwrap.dedent("""
        RISC-V Linux Kernel SV39
        ------------------------
         ffffffff80000000 |   -2    GB | ffffffffffffffff |    2 GB | kernel
         ffffffc600000000 | -232    GB | ffffffd5ffffffff |   64 GB | vmalloc/ioremap space
    """))
    (root / "Documentation/arch/riscv/boot.rst").write_text(textwrap.dedent("""
          * ``$a0`` to contain the hartid of the current core.
          * ``$a1`` to contain the address of the devicetree in memory.
    """))


def test_parse_tree(tmp_path):
    _mini_tree(tmp_path)
    d = kernel.parse_tree(tmp_path)

    sysc = {s.name: s for s in d.syscalls}
    assert sysc["write"].nr == 64 and sysc["write"].entry == "sys_write"
    assert sysc["mmap"].nr == 222 and sysc["mmap"].entry == "sys_mmap"  # 64-bit form
    assert "__NR_syscalls" not in [s.name for s in d.syscalls]          # count skipped

    traps = {t.name: t for t in d.traps}
    assert traps["EXC_SYSCALL"].code == 8
    assert "IRQ_LOCAL_MAX" not in traps                                  # _MAX filtered

    ext = {s.extension: s for s in d.sbi if s.kind == "extension"}
    assert ext["HSM"].eid == "0x48534d"
    assert "VENDOR" not in [s.extension for s in d.sbi]                  # _START skipped
    fns = [s for s in d.sbi if s.kind == "function" and s.extension == "HSM"]
    assert [s.fid for s in fns] == [0, 1, 2]                             # implicit increment

    vm = [m for m in d.memmap if m.category == "vm-layout"]
    assert any(m.region == "kernel" and m.start == "0xffffffff80000000" for m in vm)
    boot = {m.region: m for m in d.memmap if m.category == "boot-abi"}
    assert "a0" in boot and "a1" in boot
