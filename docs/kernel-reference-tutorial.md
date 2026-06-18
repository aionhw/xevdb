# Tutorial: the RISC-V Linux kernel architecture database

A companion to the [RISC-V ISA reference](riscv-reference-tutorial.md). Where the
ISA DB knows *instructions, registers, and CSRs*, this one knows the **software
architecture layered on top** — the pieces you need to read what a RISC-V Linux
system is doing in a trace:

- **syscalls** — the number in `a7` at an `ecall` → name + `sys_*` entry
- **trap causes** — `scause`/`mcause` exception and interrupt codes
- **SBI** — the Supervisor↔Machine ABI (extension IDs + function IDs)
- **memory map** — the Sv39/Sv48/Sv57 virtual-memory layout + boot register ABI

It is **parsed from a real kernel source tree** (only the architecture/ABI
surfaces — no drivers or internals). A snapshot ships with the package, and you
can re-parse any `linux/` checkout.

---

## 1. Prerequisites

The same OpenSearch setup as the ISA tutorial, and a pointer file (reuse the same
one — the ISA and kernel data happily share a dataset):

```sh
export XEVDB_OPENSEARCH_HOSTS=localhost:9200
# reuse riscv.ptr.json from the ISA tutorial, or create one (see §2 there)
```

---

## 2. Build it

From the bundled snapshot (no kernel tree needed):

```sh
xevdb ingest-kernel riscv.ptr.json --reset
```
```
ingested kernel architecture into riscv.ptr.json: 338 syscalls, 29 traps, 53 sbi, 32 memmap
```

…or parse a **real kernel checkout** — handy to pin the data to the exact kernel
your target runs:

```sh
xevdb ingest-kernel riscv.ptr.json --kernel-tree ~/src/linux --reset
```

Either way it seeds the `kernel_*` search prompts:

```sh
xevdb prompt list riscv.ptr.json | grep kernel_
```
```
kernel_memmap_by_mode  List the VM-layout regions for a paging mode (Sv39/Sv48/Sv57).
kernel_memmap_lookup   Search the kernel virtual-memory layout / boot ABI by name or text.
kernel_sbi_functions   List the function IDs of one SBI extension (e.g. HSM, TIME, RFENCE).
kernel_sbi_search      Search SBI extensions and functions (the S<->M ABI).
kernel_syscall_by_nr   Decode a syscall number (the value in a7 at an ecall) to its name.
kernel_syscall_search  Search Linux syscalls by name, entry symbol, or description.
kernel_trap_by_code    Decode an scause/mcause code to its trap cause (kind = exception or interrupt).
kernel_trap_search     Search trap causes (exceptions + interrupts) by name or text.
```

---

## 3. Syscalls

The RISC-V calling convention puts the syscall number in `a7`; decode it:

```sh
xevdb prompt run riscv.ptr.json kernel_syscall_by_nr --arg nr=64
```
```
nr  name   entry      abi      description
64  write  sys_write  generic  Write to a file descriptor.
```

Or search by name / entry / description:

```sh
xevdb prompt run riscv.ptr.json kernel_syscall_search --arg query=openat
```
```
nr  name    entry       abi      description
56  openat  sys_openat  generic  Open a file relative to a directory fd.
```

> RISC-V is a *generic-unistd* architecture, so these numbers come straight from
> `include/uapi/asm-generic/unistd.h` (with `__NR3264_*` forms resolved to their
> 64-bit values, e.g. `mmap` = 222).

---

## 4. Trap causes

When the core traps, `scause`/`mcause` holds the cause code; its top bit selects
**interrupt** vs **exception**, so pass `kind` along with the code:

```sh
xevdb prompt run riscv.ptr.json kernel_trap_by_code --arg code=13 --arg kind=exception
```
```
code  kind       name                 label            description
13    exception  EXC_LOAD_PAGE_FAULT  Load page fault  Load page fault.
```

Search across all causes (e.g. every page-fault flavour):

```sh
xevdb prompt run riscv.ptr.json kernel_trap_search --arg query=page
```
```
code  kind       name                       label                  description
12    exception  EXC_INST_PAGE_FAULT        Inst page fault        Instruction page fault.
13    exception  EXC_LOAD_PAGE_FAULT        Load page fault        Load page fault.
15    exception  EXC_STORE_PAGE_FAULT       Store page fault       Store/AMO page fault.
20    exception  EXC_INST_GUEST_PAGE_FAULT  Inst guest page fault  Instruction guest-page fault.
...
```

---

## 5. SBI (the S↔M ABI)

The kernel calls into M-mode firmware via SBI `ecall`s, selecting an extension in
`a7` and a function in `a6`. List a known extension's functions:

```sh
xevdb prompt run riscv.ptr.json kernel_sbi_functions --arg extension=HSM
```
```
name                      fid  description
SBI_EXT_HSM_HART_START    0    Hart start
SBI_EXT_HSM_HART_STOP     1    Hart stop
SBI_EXT_HSM_HART_STATUS   2    Hart status
SBI_EXT_HSM_HART_SUSPEND  3    Hart suspend
```

…or search extensions + functions together (the extension ID is also the ASCII
of its name — `0x54494d45` = "TIME"):

```sh
xevdb prompt run riscv.ptr.json kernel_sbi_search --arg query=timer
```
```
kind       extension  name                eid         fid  description
extension  TIME       SBI_EXT_TIME        0x54494d45  -1   Timer extension (set the timer).
function   TIME       SBI_EXT_TIME_SET_TIMER          0    Set timer
```

---

## 6. Memory map

List the virtual-memory layout for a paging mode:

```sh
xevdb prompt run riscv.ptr.json kernel_memmap_by_mode --arg mode=Sv39
```

…or find one region across all modes:

```sh
xevdb prompt run riscv.ptr.json kernel_memmap_lookup --arg query=vmalloc
```
```
mode   region   start               end                 size   description
Sv39   vmalloc  0xffffffc600000000  0xffffffd5ffffffff  64 GB  vmalloc/ioremap space
Sv48   vmalloc  0xffff8f8000000000  0xffffaf7fffffffff  32 TB  vmalloc/ioremap space
Sv57   vmalloc  0xff20000000000000  0xff5fffffffffffff  16 PB  vmalloc/ioremap space
```

The boot register ABI is in the same table (`category = boot-abi`): `a0` = hartid,
`a1` = devicetree address at kernel entry. `kernel_memmap_lookup --arg query=hartid`
surfaces it.

---

## 7. Worked example — a userspace fault, end to end

A RISC-V Linux core traps mid-trace. Read the raw values; the DB explains them:

| Trace value | Query | Meaning |
| --- | --- | --- |
| `ecall`, `a7 = 64` | `kernel_syscall_by_nr --arg nr=64` | userspace called **write** |
| `scause = 0x0d` | `kernel_trap_by_code --arg code=13 --arg kind=exception` | **load page fault** |
| faulting addr in `0xffffffc6…` | `kernel_memmap_lookup --arg query=vmalloc` | address is in the **vmalloc** region |
| later `ecall`, `a7 = 0x48534d`, `a6 = 0` | `kernel_sbi_functions --arg extension=HSM` | kernel asked SBI to **start a hart** |

No memorised tables, no manual grepping through the kernel — four deterministic
lookups reconstruct the story.

Combine with the ISA DB (same pointer) for the full picture: `riscv_reg_lookup
--arg query=a7` confirms `a7` = x17 (the syscall/SBI number register).

---

## 8. Pin it to your kernel / regenerate

The data is parsed by `scripts/gen_kernel_data.py`, which reads, from a tree:

```
include/uapi/asm-generic/unistd.h            # syscalls
arch/riscv/include/asm/csr.h                 # trap causes (EXC_*/IRQ_*)
arch/riscv/include/asm/sbi.h                 # SBI extension + function enums
Documentation/arch/riscv/vm-layout.rst       # VM layout
Documentation/arch/riscv/boot.rst            # boot register ABI
```

```sh
python scripts/gen_kernel_data.py --kernel-tree ~/src/linux   # rewrites the bundled JSON
xevdb ingest-kernel riscv.ptr.json --reset                    # re-ingest
```

> Kernels before ~6.8 keep these docs under `Documentation/riscv/` instead of
> `Documentation/arch/riscv/`; the parser falls back automatically.

---

## 9. JSON + AI

As with the ISA DB, add `--json` for structured rows and feed those to a model —
evidence first, interpretation second. The agent never recalls a syscall number
or trap code from memory; it gets the proven row and narrates it:

```sh
xevdb prompt run riscv.ptr.json kernel_trap_by_code --arg code=13 --arg kind=exception --json
```

---

## 10. Troubleshooting

- **`ConnectionTimeout`** — raise the pointer's `extra.timeout` (see the ISA
  tutorial §2).
- **Stale rows after re-ingest** — `xevdb cache clear riscv.ptr.json`.
- **`opensearch-only` error** — kernel reference is OpenSearch-only; go through a
  pointer file (or `--backend opensearch`).
- **Docs 404 when fetching a tree** — make sure you have the full tree; the parser
  needs `Documentation/arch/riscv/` (or the pre-6.8 `Documentation/riscv/`).
