# xevdb

**VCD + SystemVerilog → single SQLite file.** Query signal values, show
source code, run stored SQL "prompts", cache results — all in one
self-contained `.xevdb` file. Zero AI dependencies.

> **New here?** Start with **[INTRODUCTION.md](INTRODUCTION.md)** — a guided tour
> from a single waveform up to AI-assisted FPGA debug.

Combines `vcdb` (waveform ingest) with the `xezim-parser` Rust
SystemVerilog parser (cloned + built by `install.sh`) so the same
`.xevdb` file holds:

- VCD signal definitions and value changes
- Parsed RTL modules, ports, internal signal declarations, instantiations
- **Full source text** of every ingested `.sv` / `.v` file (so the file
  stays portable — the original RTL is not required after build)
- **Simulator output** — UVM/iverilog/Questa log lines parsed into severity +
  simulation time + `file:line` ref columns (joinable with RTL modules)
- A library of parameterized prompts (42 seeded — waveform/RTL/sim, the bug KB, and the RISC-V/kernel reference)
- A per-database query-result cache

## Why

You're debugging a sim hang, you have a VCD and the RTL. Existing options:

- **`grep` the VCD** → wrong (last-value-before-T semantic gets lost)
- **Open it in GTKWave** → no programmatic access; no integration with RTL
- **Hand-roll SQL** → starts to look like xevdb after a week

xevdb does both: a waveform query (*what is `reg_pc` at t=200?*) and an
RTL display (*show me the always block that drives it*), from one
queryable file you can hand to a teammate or pipe through `jq`.

## Install

```sh
bash install.sh                   # venv + clone/build sv-parse + smoke-test
bash install.sh --with-opensearch # opt-in: team-shared / large dumps + the
                                  # RISC-V & kernel reference (OpenSearch backend)
source .venv/bin/activate
```

`install.sh` runs five idempotent steps: prerequisite checks → Python
venv + `pip install -e .` → **clone the SystemVerilog parser** → build
it → smoke-test the full pipeline.

The parser (`sv-parse`) is **not vendored** — `install.sh` clones it
from [`aionhw/xezim-core`](https://github.com/aionhw/xezim-core) into
`./xezim-core/` and `cargo build`s it there. Re-running the installer
`git pull`s that checkout, so a stale parser is one `bash install.sh`
away from current. `./xezim-core/` is git-ignored — it is an
installer-managed clone, not part of this repo.

**Requirements:** Python ≥ 3.10, Rust ≥ 1.75 (`cargo`), and `git`.
`click` is the only third-party Python dependency.

**Without Rust** (`bash install.sh --no-rust`, or `cargo` absent): the
clone + build are skipped and the install still succeeds — but the RTL
features (`ingest-rtl`, `show`, `modules`) are unavailable. The
waveform and X/Z sides work regardless.

Two environment knobs control the parser checkout:

| Variable | Default | Purpose |
| --- | --- | --- |
| `XEZIM_CORE_REPO` | `https://github.com/aionhw/xezim-core.git` | git URL to clone |
| `XEZIM_CORE_REF`  | `main` | branch / tag / SHA to check out |

`xevdb` finds the built binary at
`./xezim-core/xezim-parser/target/release/sv-parse`; override with the
`XEVDB_SV_PARSE` environment variable to point at a parser elsewhere.

## Quick start

### Tiny fixture (4 signals, 2 modules)

```sh
xevdb build       examples/simple/counter.vcd
xevdb ingest-rtl  examples/simple/counter.vcd.xevdb  examples/simple/

# Waveform side
xevdb at     examples/simple/counter.vcd.xevdb  top.u_cnt.count --time 25
# top.u_cnt.count   @25   last_t=25   value=00000010

# RTL side: show the module header that declares it.
xevdb show   examples/simple/counter.vcd.xevdb  counter
# == module counter @ examples/simple/counter.sv:3-19  (kind=module ports=4 params=1 always=1 …) ==
#        1  // 8-bit synchronous counter with synchronous active-high reset.
#        2  // Used as a trudbg smoke-test target.
# >>     3  module counter #(
#        4      parameter WIDTH = 8
# …

# Or show the whole module body.
xevdb show   examples/simple/counter.vcd.xevdb  counter --full

# Or look up a single signal by bare name.
xevdb show   examples/simple/counter.vcd.xevdb  en
# == logic  en in module top @ examples/simple/counter.sv:27 ==
# >>    27      logic en;
# …
```

### Real-world demo — picorv32

```sh
bash demos/picorv32/run.sh
```

Builds the `.xevdb` from `iv.vcd`, ingests `picorv32.v` + `testbench.v`,
then exercises both sides: clock-period measurement, X/Z signal hunt,
signal-declaration lookup for `reg_pc`, and `xevdb show picorv32` to print
the core's source straight from the database. See
[`demos/picorv32/README.md`](demos/picorv32/README.md).

## Surface

### Waveform commands (inherited from vcdb)

```
xevdb build  <vcd> [--db out.xevdb] [--reset] [--no-seed]
xevdb build-xtrace <xtrace> [--db out.xevdb] [--reset] [--no-seed]
xevdb at     <db> <signal> --time <t>           [--json]
xevdb window <db> <signal> --from <t0> --to <t1> [--limit N] [--json]
xevdb find   <db> <pattern>                      [--limit N] [--json]
xevdb stats  <db>                                [--json]
```

### RTL commands

```
xevdb ingest-rtl <db> <path> [--reset]   # parse .v/.sv into the DB
xevdb modules    <db> [--filter NAME] [--limit N] [--json]
xevdb show       <db> <target> [--full] [--context N] [--no-line-numbers]
xevdb ingest-riscv <ptr> [--reset]       # build the standalone RISC-V ISA reference (OpenSearch)
xevdb ingest-kernel <ptr> [--kernel-tree DIR] [--reset]  # RISC-V Linux kernel arch (OpenSearch)
```

### X/Z tracing

Find and trace unknown (`x`) / high-impedance (`z`) states — the
classic "my sim is full of red" debug. A value is X/Z-dirty when it
carries any `x`/`X`/`z`/`Z` bit.

```
xevdb xz summary   <db>                              [--json]
xevdb xz first     <db> [--limit N]                  [--json]
xevdb xz signal    <db> <signal>                     [--json]
xevdb xz at        <db> --time T [--limit N]         [--json]
xevdb xz propagate <db> <seed> [--window W] [--limit N] [--json]
```

- **`summary`** — how widespread the X/Z is (signals %, changes %),
  when it starts, and the *root-cause set*: every signal that goes
  X/Z at the earliest X/Z timestamp.
- **`first`** — all X/Z signals ranked by the time they FIRST went
  dirty. The top of the list is the root cause; everything else
  inherits it.
- **`signal`** — the enter/leave X/Z intervals for one signal over
  the whole trace. An open interval means it was still X/Z at the end.
- **`at`** — every signal sitting in X/Z at instant `T`, using the
  correct waveform semantic (last change with `t <= T`, not a grep).
- **`propagate`** — give it a seed signal; it lists every signal that
  turns X/Z at or after the seed, ranked by how soon (`+dt`). If the
  `.xevdb` also has RTL ingested, each candidate shows the module +
  `file:line` where a same-named signal is declared — the bridge from
  "this went X" to "here is the RTL that drives it".

```sh
# Where does the X/Z come from?
xevdb xz summary  picorv32.xevdb
xevdb xz first    picorv32.xevdb --limit 10

# When was reg_pc unknown?
xevdb xz signal   picorv32.xevdb reg_pc

# What inherited X/Z from the AXI address bus within 30 ns of it?
xevdb xz propagate picorv32.xevdb testbench.top.mem_axi_araddr --window 30000
```

A bare signal name that is unique resolves automatically; an ambiguous
one (`mem_axi_araddr` exists in several scopes) must be given as a
fully-qualified `hier.name`.

### Simulator output

```
xevdb ingest-sim <db> <log> [--name N] [--keep-all] [--reset]
```

Parses a sim log (UVM, iverilog, Questa, VCS-style) into structured events:
canonicalized severity (`UVM_FATAL`/`UVM_ERROR`/`UVM_WARNING`/`UVM_INFO`/
`ERROR`/`WARNING`/`ASSERTION`/`FATAL`/`NOTE`/`INFO`), simulation time
recovered from common encodings (`@ 200:`, `[t=200]`, `[100]`, `# 100:`,
`at time 100`, `t=200ns`), and any `file.sv:NNN` reference embedded in the
message. Each ingest inserts one row into `sim_runs` and one row per matched
line into `sim_events`. Use `--keep-all` to retain non-severity lines as
`severity='INFO'`.

Query the sim side via the seed prompts (`sim_summary`, `sim_errors`,
`sim_around_time`, `sim_by_ref_file`) or with hand-rolled SQL — the schema
is plain SQLite.

`xevdb show` accepts:
- a module name (`counter`) — shows the declaration header (or body with `--full`)
- a signal or port name (`reg_pc`, `clk`) — finds the owning module(s)
- a `file:line` (`examples/simple/counter.sv:14`) — shows a context window

All code is sliced from the `source_files` table inside the `.xevdb`. The
original `.sv` files do not need to be on disk at query time.

### Prompts

Same surface as vcdb:

```
xevdb prompt list   <db>
xevdb prompt show   <db> <name>
xevdb prompt run    <db> <name> [--arg k=v]* [--no-cache] [--ttl N] [--json]
xevdb prompt add    <db> <name> --sql "..." [--params-json "[...]"]
xevdb prompt remove <db> <name>
```

**42 prompts are seeded.** The waveform / RTL / sim core is below; the bug-KB
prompts are in [Bug knowledge base](#bug-knowledge-base), and the RISC-V and
kernel prompts in their reference sections. `xevdb prompt list <db>` shows them
all. On the OpenSearch backend a prompt runs only if it carries a `dsl_json`
(24 of 42 do today); the SQL-only ones — RTL `*_of_module`, the `sim_*` family,
and the cross-index joins — are SQLite-only and report so.

**Seeded prompts — waveform / RTL / sim core (20):**

| Name | Side | Purpose |
| --- | --- | --- |
| `signal_transitions` | wave | Busiest signals in a window (top N). |
| `change_count` | wave | Total transitions per signal. |
| `stuck_at` | wave | Signals that never changed. |
| `xz_signals` | wave | Signals that ever carried `x`/`z`. |
| `value_at_many` | wave | Snapshot — last value of every signal matching a pattern at time T. |
| `signal_history` | wave | Full change history of one signal. |
| `clock_period` | wave | Estimate clock period from rising edges. |
| `signals_in_scope` | wave | List signals under a hier prefix. |
| `list_modules` | rtl | List every parsed RTL module. |
| `ports_of_module` | rtl | All ports of one module, in declaration order. |
| `signals_of_module` | rtl | Internal wires/regs/logic of one module. |
| `signal_declaration` | rtl | Find where a name is declared (signal or port). |
| `modules_in_file` | rtl | All modules in one source file. |
| `instance_tree` | rtl | One-level parent → child instantiation map. |
| `sim_summary` | sim | Per-run line count and severity breakdown. |
| `sim_errors` | sim | Every UVM_FATAL/UVM_ERROR/ERROR/FATAL/ASSERTION event. |
| `sim_around_time` | sim | Sim events in a `[t0, t1]` window. |
| `sim_by_ref_file` | sim | Sim events whose embedded `file:line` ref matches a path. |
| `xz_signals_with_rtl` | wave + rtl | Cross — VCD X/Z signals joined with their RTL declaration. |
| `sim_with_rtl` | sim + rtl | Cross — sim events whose `ref_file:ref_line` lands in a parsed module. |

**Bug KB, RISC-V, and kernel prompts (22):**

| Name | Side | Purpose |
| --- | --- | --- |
| `bug_search` | bug | Full-text search the bug KB. |
| `bugs_by_status` | bug | Bugs with a given status, newest first. |
| `bugs_for_signal` | bug | Bugs linked to a signal. |
| `bugs_for_module` | bug | Bugs linked to a module. |
| `bugs_with_rtl` | bug + rtl | Bugs whose linked module exists in the RTL. |
| `xz_signals_with_open_bugs` | bug + wave | X/Z signals that have an open bug. |
| `riscv_instr_search` | riscv | Search instructions by mnemonic / syntax / description. |
| `riscv_instr_by_name` | riscv | Exact instruction lookup (encoding + format). |
| `riscv_by_extension` | riscv | Instructions in an extension (RV32I/M/A/F/D/C/…). |
| `riscv_csr_lookup` | riscv | Find a CSR by name or description. |
| `riscv_csr_by_addr` | riscv | Decode a CSR number (e.g. `0x305` → `mtvec`). |
| `riscv_reg_lookup` | riscv | Resolve a register (`a0` → x10, caller-saved). |
| `riscv_pseudo_search` | riscv | Pseudo-instruction → real expansion. |
| `riscv_ext_overview` | riscv | Instruction count per extension. |
| `kernel_syscall_by_nr` | kernel | Decode a syscall number (the `a7` value). |
| `kernel_syscall_search` | kernel | Search syscalls by name / entry / description. |
| `kernel_trap_by_code` | kernel | Decode an `scause`/`mcause` code. |
| `kernel_trap_search` | kernel | Search trap causes. |
| `kernel_sbi_search` | kernel | Search SBI extensions + functions. |
| `kernel_sbi_functions` | kernel | Function IDs of one SBI extension. |
| `kernel_memmap_lookup` | kernel | Search the VM layout / boot ABI. |
| `kernel_memmap_by_mode` | kernel | VM-layout regions for a paging mode. |

The bug prompts run on both backends; the `riscv_*`/`kernel_*` prompts are
OpenSearch-only (they query the reference indices). See
[USER_GUIDE](USER_GUIDE.md) §10–12 for worked examples.

### Bug knowledge base

Record what went wrong and how it was fixed, then find it again later. A bug
has a unique slug name, free-text symptom / root-cause / fix, searchable
keywords + tags, and typed links to the signals, modules, and `file:line`
refs it touches.

```
xevdb bug add    <db> <name> [--symptom ...] [--fix ...] [--keyword K]*
                             [--signal S]* [--module M]* [--ref FILE:LINE]*
xevdb bug show   <db> <name>                       [--json]
xevdb bug list   <db> [--status open] [--tag T]    [--json]
xevdb bug search <db> <query>                      [--json]   # full-text
xevdb bug link   <db> <name> --signal S | --module M | --ref FILE:LINE
xevdb bug close  <db> <name> [--fix ...] [--fix-ref PR#42]
xevdb bug remove <db> <name>
```

`bug search` uses SQLite FTS5 ranking when available (falling back to a
portable `LIKE` scan), or an OpenSearch `multi_match` on that backend. Bugs
join into the rest of the data through the `bugs_for_signal`,
`bugs_for_module`, `bugs_with_rtl`, and `xz_signals_with_open_bugs` prompts.

```sh
xevdb bug add picorv32.xevdb "axi-fifo-xprop" --severity error \
  --symptom "fifo_full goes X after reset" \
  --root-cause "uninitialized temp array read past populated range" \
  --fix "pre-init temp arrays before \$readmemh" \
  --signal testbench.top.mem_axi_araddr --ref picorv32.v:176
xevdb bug search picorv32.xevdb "uninitialized reset"
```

### RISC-V ISA reference (OpenSearch only)

A standalone, **waveform-independent** knowledge base of the RISC-V ISA —
instructions (IMAFDC + Zicsr + Zifencei, with encodings), the register file
(x0–x31 / f0–f31 + ABI names), control/status registers, extensions, and
pseudo-instructions. It's its own xevdb dataset (its own pointer file), built
once and queried forever; it does not need a VCD. The bundled data is generated
by `scripts/gen_riscv_data.py` and ships with the package, so ingest needs no
network.

Step-by-step walkthrough: [`docs/riscv-reference-tutorial.md`](docs/riscv-reference-tutorial.md).

```sh
export XEVDB_OPENSEARCH_HOSTS=localhost:9200
# build the reference DB into a fresh pointer (no waveform required)
xevdb --backend opensearch ingest-riscv riscv.ptr.json --reset
```

Search it with the seeded `riscv_*` prompts:

```sh
xevdb prompt run riscv.ptr.json riscv_instr_by_name  --arg name=jalr     # encoding + format
xevdb prompt run riscv.ptr.json riscv_instr_search   --arg query=jump    # full-text
xevdb prompt run riscv.ptr.json riscv_by_extension   --arg extension=M   # all M instructions
xevdb prompt run riscv.ptr.json riscv_csr_by_addr    --arg addr=0x305    # decode a CSR number -> mtvec
xevdb prompt run riscv.ptr.json riscv_csr_lookup     --arg query=trap    # CSRs by name/description
xevdb prompt run riscv.ptr.json riscv_reg_lookup     --arg query=a0      # a0 -> x10, caller-saved
xevdb prompt run riscv.ptr.json riscv_pseudo_search  --arg query=ret     # ret -> jalr x0, 0(ra)
xevdb prompt run riscv.ptr.json riscv_ext_overview                       # instruction count per extension
```

Indices `xevdb-<dump_id>-riscv_{instructions,registers,csrs,extensions,pseudo}`.
After re-ingesting changed data, `xevdb cache clear riscv.ptr.json` so stale
results aren't served. RISC-V ingest/search is OpenSearch-only; the SQLite
backend fails fast with a clear message.

### RISC-V Linux kernel architecture (OpenSearch only)

A companion knowledge base, **parsed from a real kernel source tree** (only the
architecture/ABI surfaces — no driver/internals): the **syscall table** (number
in `a7` → name + `sys_*` entry), **trap causes** (scause/mcause exception +
interrupt codes), the **SBI** S↔M ABI (extension IDs + function IDs), and the
**virtual-memory layout** (Sv39/Sv48/Sv57) + boot register ABI. The bundled
snapshot ships with the package; `--kernel-tree` re-parses any `linux/` checkout
(regenerate with `scripts/gen_kernel_data.py`).

```sh
xevdb --backend opensearch ingest-kernel kernel.ptr.json --reset            # bundled snapshot
xevdb --backend opensearch ingest-kernel kernel.ptr.json --kernel-tree ~/linux --reset
```

```sh
xevdb prompt run kernel.ptr.json kernel_syscall_by_nr  --arg nr=64                      # a7=64 -> write
xevdb prompt run kernel.ptr.json kernel_syscall_search --arg query=openat
xevdb prompt run kernel.ptr.json kernel_trap_by_code   --arg code=13 --arg kind=exception  # -> load page fault
xevdb prompt run kernel.ptr.json kernel_trap_search    --arg query=page
xevdb prompt run kernel.ptr.json kernel_sbi_functions  --arg extension=HSM              # hart start/stop/...
xevdb prompt run kernel.ptr.json kernel_sbi_search     --arg query=timer
xevdb prompt run kernel.ptr.json kernel_memmap_by_mode --arg mode=Sv39                  # VM layout
xevdb prompt run kernel.ptr.json kernel_memmap_lookup  --arg query=vmalloc
```

Indices `xevdb-<dump_id>-kernel_{syscalls,traps,sbi,memmap}`. The RISC-V ISA and
kernel reference can share one pointer/dataset (ISA + the software ABI on top of
it) or live in separate ones. OpenSearch-only, same as the ISA reference.

### Cache

```
xevdb cache stats <db>                       [--json]
xevdb cache list  <db> [--prompt NAME]       [--json]
xevdb cache clear <db> [--prompt NAME] [--yes]
```

Bypass entirely with `XEVDB_NO_CACHE=1`.

## Backends

xevdb stores a dataset behind a pluggable backend. The default is **SQLite** —
the self-contained `.xevdb` file everything above describes. An optional
**OpenSearch** backend serves the same dataset from a cluster, for dumps too
large for one file or shared across a team.

```sh
# SQLite (default) — nothing to choose
xevdb build wave.vcd

# OpenSearch — the data lives in a cluster; the on-disk artifact is a tiny
# JSON *pointer file* naming the cluster + dump id (created on first build).
export XEVDB_OPENSEARCH_HOSTS=localhost:9200
xevdb --backend opensearch build wave.vcd --db wave.xevdb
xevdb prompt run wave.xevdb change_count        # auto-routes: a pointer file → opensearch
```

Select the backend with `--backend sqlite|opensearch` or `$XEVDB_BACKEND`; a
pointer file auto-routes to OpenSearch with no flag. The two backends share one
logical surface (`build`, `ingest-*`, `at`, `window`, `find`, `prompt`,
`cache`, `bug`).

**What differs on OpenSearch:**

- **Prompts** run a prompt's `dsl_json` template, not its `sql`. Seeded prompts
  carry both where the query maps cleanly; cross-index-join prompts
  (`*_with_rtl`, `xz_signals_with_open_bugs`) are SQL-only and raise on
  OpenSearch. Add `dsl_json` to your own prompts with `prompt add --dsl-json`.
- **`modules`, `show`, and `xz`** read via hand-written SQL and are
  SQLite-only; they fail fast with a clear message on OpenSearch.
- Links/associations are denormalized into document arrays instead of a side
  table, so signal/module bug lookups need no join.

## Schema

A `.xevdb` is a standard SQLite file. Inspect it with `sqlite3`:

```sql
-- waveform side
CREATE TABLE signals       (id, hier, name, fullname, width, kind);
CREATE TABLE changes       (sig_id, t, value);            -- indexed (sig_id, t)
CREATE TABLE meta          (key, value);

-- simulator output
CREATE TABLE sim_runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT,        -- short identifier (default: log basename)
    source          TEXT,        -- absolute log path
    ingested_at     REAL,
    line_count      INTEGER,
    n_events        INTEGER,
    n_fatal, n_error, n_warning  INTEGER,
    severity_json   TEXT         -- {"UVM_ERROR": 3, ...}
);
CREATE TABLE sim_events (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id    INTEGER,
    line_no   INTEGER,           -- 1-based line in the source log
    severity  TEXT,               -- UVM_FATAL/UVM_ERROR/.../ASSERTION/INFO
    t         INTEGER,            -- simulation time if recovered (NULL otherwise)
    ref_file  TEXT,               -- 'picorv32.v' or similar, if embedded
    ref_line  INTEGER,
    message   TEXT                -- raw line
);
-- indices: (run_id), (severity), (t), (ref_file)

-- RTL side
CREATE TABLE source_files  (path PK, content, hash, ingested_at);
CREATE TABLE modules       (id PK, name, kind, file, line_start, line_end,
                            leading_comment, body_summary, params_json,
                            ast_json, ingested_at);
CREATE TABLE module_ports  (module_id, position, name, direction, width, kind);
CREATE TABLE module_signals    (module_id, name, kind, line, width, decl_text);
CREATE TABLE module_instances  (parent_module_id, child_module_name,
                                instance_name, line);

-- bug knowledge base
CREATE TABLE bugs       (name PK, title, status, severity, symptom,
                         root_cause, fix, fix_ref, keywords_json, tags_json,
                         created_at, updated_at);
CREATE TABLE bug_links  (bug_name, kind, value, extra);   -- signal/module/ref/...
-- bugs_fts: an FTS5 mirror of the text columns, when SQLite has FTS5

-- prompts + cache
CREATE TABLE prompts (name PK, description, sql, dsl_json, params_json,
                      created_at, updated_at);   -- dsl_json: OpenSearch query template
CREATE TABLE cache   (key PK, prompt_name, args_json, result_json,
                      created_at, hits, last_hit_at, ttl_seconds);
```

## MCP server (AI agents)

`xevdb mcp <db>` serves one dataset to an AI agent over the **Model Context
Protocol** (stdio, JSON-RPC) — so an agent queries the deterministic evidence
directly instead of guessing. Zero extra dependencies (the protocol is built in).

```sh
xevdb mcp counter.vcd.xevdb       # a waveform dump
xevdb mcp riscv.ptr.json          # or the RISC-V / kernel reference (OpenSearch)
```

Configure it in any MCP client (Claude Desktop, Claude Code, etc.):

```json
{
  "mcpServers": {
    "xevdb": { "command": "xevdb", "args": ["mcp", "/path/to/db.xevdb"] }
  }
}
```

Tools exposed: `stats`, `find_signals`, `signal_value_at`, `signal_window`,
`list_prompts`, `run_prompt` (the whole stored library — waveform/RTL/sim/bug
and RISC-V/kernel decode, e.g. `run_prompt riscv_csr_by_addr {addr:"0x305"}`),
`search_bugs`, and `show_source` (RTL, sqlite backend). One server serves one
dataset — point separate servers at a dump and at the reference DB. Tool errors
come back as content (the agent sees them), not as protocol failures.

## Testing

```sh
pip install -e '.[opensearch,dev]'    # pytest + ruff + opensearch-py
pytest -q                             # 134 tests, no live cluster needed
ruff check src tests scripts
```

The OpenSearch backend is exercised against an in-memory fake client, so the
suite runs offline. The one live-cluster test in `tests/test_opensearch_integration.py`
is skipped unless `XEVDB_OPENSEARCH_TEST_HOST` is set. CI runs `pytest` + `ruff`
on every push (`.github/workflows/ci.yml`).

## Relationship to vcdb and trudbg

| Package | Scope | AI? | Rust? |
| --- | --- | --- | --- |
| **vcdb** | VCD-only DB + prompts + cache | No | No |
| **xevdb** | VCD + SV (this package) — adds code display | No | Yes (sv-parse, cloned at install) |
| **trudbg** | VCD + SV + log corpus + Claude rerank + Code subagent | Yes | Yes |

`vcdb` is the right tool when you only have waveforms. `xevdb` adds RTL.
`trudbg` adds AI on top of `xevdb`-style storage.

## License

Apache 2.0.
