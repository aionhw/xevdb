# xevdb User Guide

A practical, end-to-end walkthrough of `xevdb` — from a raw `.vcd` to a
queryable debug database, the bug knowledge base, the RISC-V / kernel reference,
and serving it all to an AI agent over MCP. The [README](README.md) is the
terse reference; this guide is the tutorial.

**Contents**

1. [The mental model](#1-the-mental-model)
2. [Install](#2-install)
3. [Your first database](#3-your-first-database)
4. [Waveform-only debugging](#4-waveform-only-debugging)
5. [Add the RTL](#5-add-the-rtl)
6. [Add the simulator log](#6-add-the-simulator-log)
7. [X/Z tracing](#7-xz-tracing)
8. [Stored prompts](#8-stored-prompts)
9. [The result cache](#9-the-result-cache)
10. [The bug knowledge base](#10-the-bug-knowledge-base)
11. [The RISC-V ISA reference](#11-the-risc-v-isa-reference)
12. [The Linux kernel architecture reference](#12-the-linux-kernel-architecture-reference)
13. [The OpenSearch backend — team-shared & at scale](#13-the-opensearch-backend--team-shared--at-scale)
14. [MCP — query from an AI agent](#14-mcp--query-from-an-ai-agent)
15. [Raw SQL](#15-raw-sql)
16. [Debugging cookbook](#16-debugging-cookbook)
17. [Backends at a glance](#17-backends-at-a-glance)
18. [Troubleshooting](#18-troubleshooting)
19. [Command reference](#19-command-reference)

---

## 1. The mental model

`xevdb` turns separate debug artifacts into **one queryable database**:

| Side | Input | What it gives you |
|---|---|---|
| **Waveform** | a `.vcd` dump (or XTrace) | every signal value-change, queryable by time |
| **RTL** | `.v` / `.sv` files | parsed modules, ports, signal declarations, *full source text* |
| **Simulator** | a sim log | severity-classified events with `file:line` refs |

On top of those three "dump" sides, xevdb also hosts two **standalone
reference** databases that don't need a waveform at all:

| Reference | What it answers |
|---|---|
| **RISC-V ISA** | decode an instruction / register / CSR / extension / pseudo-op |
| **RISC-V Linux kernel arch** | decode a syscall number, trap cause, SBI call, or memory region |

The default output is a `.xevdb` file — a *plain SQLite database*. You can open
it with `sqlite3`, hand it to a teammate, or pipe queries through `jq`. Once
built, **the original `.vcd` / `.sv` / `.log` are no longer needed** — their
content lives inside the database. For team-shared or very large datasets, the
same data can live on an **OpenSearch** cluster instead (§13); the reference
databases are OpenSearch-only.

The point: when you debug a sim hang, the question is never just "what is signal
X at time T" — it's "what is X at T, *and* show me the always-block that drives
it, *and* the error the sim printed nearby, *and* what instruction that opcode
was." `xevdb` answers all of them from one place — by hand, or from an AI agent
over MCP (§14).

---

## 2. Install

```sh
bash install.sh           # venv + clone/build the SV parser + smoke-test
source .venv/bin/activate
xevdb --help              # verify
```

Requires Python ≥ 3.10, Rust ≥ 1.75 (`cargo`), and `git`. The SystemVerilog
parser is not vendored — `install.sh` clones it from `aionhw/xezim-core` into
`./xezim-core/` and builds it; re-running the installer `git pull`s that
checkout. Override the source with `XEZIM_CORE_REPO` / `XEZIM_CORE_REF`.

**Optional pieces:**

- `bash install.sh --no-rust` — skips the SV parser; the waveform, X/Z, prompt,
  and reference features still work, only the RTL parsing (`ingest-rtl`,
  `modules`, `show`) is unavailable.
- `bash install.sh --with-opensearch` — adds `opensearch-py` for the OpenSearch
  backend (team-shared / large dumps, and the RISC-V/kernel reference, §13).

**For development** (tests + linter):

```sh
pip install -e '.[opensearch,dev]'
pytest -q            # 134 tests, no live cluster needed
ruff check src tests scripts
```

---

## 3. Your first database

Everything starts with `build`:

```sh
xevdb build examples/simple/counter.vcd
# → writes examples/simple/counter.vcd.xevdb
```

`build` parses the VCD's signal definitions and every value change, then seeds
the stored-prompt library (42 prompts). Useful flags:

- `--db out.xevdb` — choose the output path.
- `--reset` — overwrite an existing database.
- `--no-seed` — skip the prompt library.

Check what landed:

```sh
xevdb stats examples/simple/counter.vcd.xevdb
# source        examples/simple/counter.vcd
# timescale     1ns
# n_signals     4
# n_changes     16
# t_min / t_max 0 / 40
# row_counts: signals 4, changes 16, prompts 42 ...
```

> Capturing transactions instead of raw signals? `xevdb build-xtrace cap.xtrace`
> parses an XTrace capture into the same schema.

---

## 4. Waveform-only debugging

You have a VCD and a question. Four commands cover most of it.

### `find` — locate signals

```sh
xevdb find counter.vcd.xevdb '*count*'      # glob
xevdb find counter.vcd.xevdb reg            # substring
```

Output columns: `sig_id  kind  width  fullname`.

### `at` — value at an instant

```sh
xevdb at counter.vcd.xevdb top.u_cnt.count --time 25
# top.u_cnt.count   @25   last_t=25   value=00000010
```

This uses the **correct waveform semantic**: the value *in effect* at `t=25` is
the last change with `change.t <= 25`. A naive `grep` of the VCD would miss it
whenever the signal didn't change exactly at 25.

### `window` — changes over a range

```sh
xevdb window counter.vcd.xevdb count --from 0 --to 40
xevdb window counter.vcd.xevdb count --from 0 --to 40 --limit 200 --json
```

### `stats` — the lay of the land

```sh
xevdb stats counter.vcd.xevdb
```

**Signal name resolution** (used by `at` / `window` / `xz signal`):

1. exact `sig_id` (the VCD short code), then
2. exact `fullname` (`top.u_cnt.count`), then
3. unique bare `name` (`count`), then
4. unique `*.suffix` match.

If a bare name is ambiguous (`clk` in three scopes) the command fails and asks
for a fully-qualified `hier.name`.

---

## 5. Add the RTL

Waveform values gain meaning when joined to the source that produced them.
`ingest-rtl` parses `.v` / `.sv` files into the same database:

```sh
xevdb ingest-rtl counter.vcd.xevdb examples/simple/    # a dir, or a single file
```

It stores parsed modules / ports / signals / instances **and the full source
text**. Now you can ask RTL questions:

```sh
xevdb modules counter.vcd.xevdb                 # list every module
xevdb modules counter.vcd.xevdb --filter cnt    # name filter

xevdb show counter.vcd.xevdb counter            # module header
xevdb show counter.vcd.xevdb counter --full     # whole body
xevdb show counter.vcd.xevdb en                 # a signal → owning module
xevdb show counter.vcd.xevdb examples/simple/counter.sv:14   # a file:line window
```

`xevdb show` slices code straight out of the `source_files` table — the `.sv`
files do **not** need to be on disk at query time.

---

## 6. Add the simulator log

```sh
xevdb ingest-sim counter.vcd.xevdb run.log
```

The log parser canonicalizes severity (`UVM_FATAL` / `UVM_ERROR` /
`UVM_WARNING` / `UVM_INFO` / `ERROR` / `WARNING` / `ASSERTION` / `FATAL` /
`NOTE` / `INFO`), recovers the simulation time from common encodings (`@ 200:`,
`[t=200]`, `# 100:`, `at time 100`, `t=200ns`), and extracts any `file.sv:NNN`
reference. Each ingest adds one `sim_runs` row and one `sim_events` row per
matched line. `--keep-all` retains non-severity lines too; `--name N` labels the
run; `--reset` replaces prior sim data.

Query the sim side through the seed prompts (`sim_errors`, `sim_around_time`,
`sim_with_rtl`, …) or raw SQL.

---

## 7. X/Z tracing

The most common "my sim is full of red" debug. A value is *X/Z-dirty* when it
carries any `x` / `X` / `z` / `Z` bit. The `xz` command group turns that into
the questions you actually ask. (X/Z tracing is a SQLite-backend feature.)

### Step 1 — is there X/Z, and where does it start?

```sh
xevdb xz summary picorv32.vcd.xevdb
# X/Z signals    :     8870 / 11164 (79.5%)
# first X/Z at t : 0
# root-cause set : 50 signal(s) go X/Z at t=0
#     testbench.top.mem.latched_raddr
```

The **root-cause set** is every signal that goes X/Z at the earliest X/Z
timestamp. Everything else inherits from it.

### Step 2 — rank the root causes

```sh
xevdb xz first picorv32.vcd.xevdb --limit 10
#    first_t  kind    w    #xz  signal
#          0  x      32      1  testbench.top.mem.latched_raddr
```

Signals sorted by *when* they first went dirty. The top of the list is where to
look first.

### Step 3 — one signal's history

```sh
xevdb xz signal picorv32.vcd.xevdb mem_la_addr
#   [x ] t=0 → t=1010000 (1010000 ticks)  enter=x00
#   [x ] t=1030000 → (end of trace)  enter=x00
```

Each line is an enter→leave interval. `→ (end of trace)` means the signal was
still X/Z when the dump ended — usually a real bug.

### Step 4 — snapshot at an instant

```sh
xevdb xz at picorv32.vcd.xevdb --time 200000
# 6 signal(s) in X/Z at t=200000:
#          0  x      32  ...latched_raddr = x
```

`since_t` is when that X/Z value was set — a small `since_t` with a large query
time means the signal has been stuck for a long time.

### Step 5 — trace the propagation

```sh
xevdb xz propagate picorv32.vcd.xevdb testbench.top.mem_axi_araddr --window 30000
#       +dt     first_t  kind  signal  [rtl]
#         0           0  x     ...latched_raddr  [axi4_memory @ testbench.v:358]
```

Give it a seed; it lists every signal that turns X/Z **at or after** the seed,
ranked by how soon (`+dt`). If RTL was ingested, each candidate shows the
`module @ file:line` that declares a same-named signal — the bridge from "this
went X" straight to the RTL that drives it. All `xz` subcommands accept `--json`.

---

## 8. Stored prompts

A prompt is a named, parameterized query stored *inside* the database. `build`
seeds **42** of them, spanning every side. Each carries a `sql` template and,
where portable, an OpenSearch `dsl_json` (so the same prompt runs on both
backends).

```sh
xevdb prompt list  counter.vcd.xevdb              # all prompts
xevdb prompt show  counter.vcd.xevdb xz_signals   # description + SQL + params
xevdb prompt run   counter.vcd.xevdb xz_signals --arg limit=20
xevdb prompt run   counter.vcd.xevdb signal_history --arg name=count --json
```

Representative prompts: `signal_transitions`, `stuck_at`, `clock_period`,
`signal_history`, `xz_signals` (wave); `ports_of_module`, `instance_tree`,
`signals_of_module` (rtl); `sim_errors`, `sim_around_time`, `sim_with_rtl`
(sim); `bug_search`, `bugs_for_signal` (bug KB); plus the `riscv_*` and
`kernel_*` reference prompts (§11–12). `prompt run --json` is the
machine-readable form.

Write your own:

```sh
xevdb prompt add counter.vcd.xevdb wide_buses \
  --sql "SELECT fullname, width FROM signals WHERE width >= :min ORDER BY width DESC" \
  --params-json '[{"name":"min","default":8,"type":"int"}]'

xevdb prompt run counter.vcd.xevdb wide_buses --arg min=16
xevdb prompt remove counter.vcd.xevdb wide_buses
```

Add `--dsl-json '{...}'` to make a prompt runnable on OpenSearch too. Prompts
travel with the file: hand someone a `.xevdb` and they get your queries.

---

## 9. The result cache

Every `prompt run` result is cached in the database, keyed by prompt name +
arguments.

```sh
xevdb cache stats counter.vcd.xevdb
xevdb cache list  counter.vcd.xevdb --prompt xz_signals
xevdb cache clear counter.vcd.xevdb --prompt xz_signals --yes
xevdb cache clear counter.vcd.xevdb --yes            # all entries
```

Bypass it for one invocation with `XEVDB_NO_CACHE=1`, or per-prompt with
`prompt run --no-cache`. `--ttl N` sets a per-result expiry. **Re-ingesting data
does not auto-clear the cache** — run `cache clear` after rebuilding a dataset so
stale results aren't served.

---

## 10. The bug knowledge base

Record what went wrong and how it was fixed, then find it again later — a
searchable team memory that lives inside the database. A bug has a unique slug
name, free-text symptom / root-cause / fix, searchable keywords + tags, and
typed links to the signals, modules, and `file:line` refs it touches.

```sh
xevdb bug add picorv32.xevdb axi-fifo-xprop --severity error \
  --symptom "fifo_full goes X after reset" \
  --root-cause "uninitialized temp array read past populated range" \
  --fix "pre-init temp arrays before \$readmemh" \
  --signal testbench.top.mem_axi_araddr --ref picorv32.v:176 --keyword reset

xevdb bug search picorv32.xevdb "uninitialized reset"   # full-text
xevdb bug list   picorv32.xevdb --status open
xevdb bug show   picorv32.xevdb axi-fifo-xprop --json
xevdb bug link   picorv32.xevdb axi-fifo-xprop --module axi4_memory
xevdb bug close  picorv32.xevdb axi-fifo-xprop --fix-ref PR#42
xevdb bug remove picorv32.xevdb axi-fifo-xprop
```

`bug search` uses SQLite FTS5 ranking when available, falling back to a portable
`LIKE` scan. Bugs join into the rest of the data through the `bugs_for_signal`,
`bugs_for_module`, `bugs_with_rtl`, and `xz_signals_with_open_bugs` prompts — so
"which open bugs touch a signal that's currently X/Z?" is one query.

---

## 11. The RISC-V ISA reference

A standalone, searchable RISC-V ISA database — **independent of any waveform**.
It is an OpenSearch dataset (see §13 for the cluster setup); build it once and
query it forever.

```sh
export XEVDB_OPENSEARCH_HOSTS=localhost:9200
xevdb --backend opensearch ingest-riscv riscv.ptr.json --reset
# ingested RISC-V ISA reference: 140 instructions, 64 registers, 36 csrs, 9 extensions, 21 pseudo
```

Query it with the seeded `riscv_*` prompts:

```sh
xevdb prompt run riscv.ptr.json riscv_instr_by_name  --arg name=jalr     # encoding + format
xevdb prompt run riscv.ptr.json riscv_reg_lookup     --arg query=a0      # a0 -> x10, caller-saved
xevdb prompt run riscv.ptr.json riscv_csr_by_addr    --arg addr=0x305    # 0x305 -> mtvec
xevdb prompt run riscv.ptr.json riscv_by_extension   --arg extension=M   # all M instructions
xevdb prompt run riscv.ptr.json riscv_pseudo_search  --arg query=ret     # ret -> jalr x0, 0(ra)
xevdb prompt run riscv.ptr.json riscv_ext_overview                       # count per extension
```

Covers RV32I/RV64I + M/A/F/D/C + Zicsr/Zifencei, with computed instruction
encodings (`mask`/`match`). The bundled data is curated in
`scripts/gen_riscv_data.py`; full walkthrough in
[`docs/riscv-reference-tutorial.md`](docs/riscv-reference-tutorial.md).

### Decoding instruction words

The encodings power a **decoder** — turn a raw instruction word into assembly
with `(word & mask) == match`. This uses the *bundled* ISA, so it needs **no
dataset or cluster**:

```sh
xevdb riscv-decode 0x00c58533 0x30529073
# 0x00c58533   add a0, a1, a2          [RV32I R]   Register ALU op: rd = rs1 + rs2.
# 0x30529073   csrrw zero, 0x305, t0   [Zicsr I]   Atomic read/write CSR ...
```

Words can be hex (`0x…`), binary (`0b…`), or a bare VCD bit-string; 32-bit and
16-bit compressed are both handled. The real payoff is reading the word straight
off a **waveform** — point it at an instruction-bus or fetched-opcode signal:

```sh
xevdb decode cpu.xevdb fetch_insn --time 41200
# top...fetch_insn  @41200 (set @41200)  0x00450513  addi a0, a0, 4  [RV32I I]
```

That's the bridge from a raw value in a trace to *what it means*. Both commands
take `--json`, and both are MCP tools (`decode_instruction` / `decode_signal`,
§14) so an agent can disassemble while it debugs.

---

## 12. The Linux kernel architecture reference

The software-architecture layer on top of the ISA, **parsed from a real kernel
source tree** (architecture/ABI only — no drivers): syscalls, trap causes, the
SBI ABI, and the virtual-memory layout. Shares the same pointer (or use its
own).

```sh
xevdb --backend opensearch ingest-kernel riscv.ptr.json --reset             # bundled snapshot
xevdb --backend opensearch ingest-kernel riscv.ptr.json --kernel-tree ~/linux --reset
# ingested kernel architecture: 338 syscalls, 29 traps, 53 sbi, 32 memmap
```

```sh
xevdb prompt run riscv.ptr.json kernel_syscall_by_nr  --arg nr=64                       # a7=64 -> write
xevdb prompt run riscv.ptr.json kernel_trap_by_code   --arg code=13 --arg kind=exception # -> load page fault
xevdb prompt run riscv.ptr.json kernel_sbi_functions  --arg extension=HSM               # hart start/stop/...
xevdb prompt run riscv.ptr.json kernel_memmap_by_mode --arg mode=Sv39                   # VM layout
```

Full walkthrough in
[`docs/kernel-reference-tutorial.md`](docs/kernel-reference-tutorial.md). Used
together with the ISA reference, four lookups turn an opaque trap sequence
(`ecall a7=64`, `scause=0x0d`, `csr 0x305`, `jalr x0,0(x1)`) into "userspace did
a write, took a load page fault, the handler set `mtvec`, then returned."

---

## 13. The OpenSearch backend — team-shared & at scale

The default `.xevdb` SQLite file is perfect for one person and one dump. When
you want a **team to query the same dataset from a browser**, or a dump too big
to email, the same data lives on an **OpenSearch** cluster instead.

A cluster isn't a file you can hand someone, so the on-disk artifact becomes a
tiny **pointer file** — JSON naming the cluster + dataset id. The CLI accepts it
anywhere it accepts a `.xevdb`, and a pointer auto-routes to the OpenSearch
backend with no `--backend` flag.

```sh
cat > c906.ptr.json <<'JSON'
{
  "backend": "opensearch",
  "hosts": ["localhost:9200"],
  "dump_id": "c906",
  "prefix": "xevdb",
  "extra": {"timeout": 180, "max_retries": 5, "retry_on_timeout": true}
}
JSON

# the first build needs --backend (the pointer is synthesized if absent);
# afterwards the pointer auto-routes.
export XEVDB_OPENSEARCH_HOSTS=localhost:9200
xevdb --backend opensearch build c906.vcd --db c906.ptr.json --reset
xevdb ingest-rtl c906.ptr.json rtl/
xevdb stats  c906.ptr.json
xevdb at     c906.ptr.json some.signal --time 1000
xevdb window c906.ptr.json some.signal --from 0 --to 5000
xevdb prompt run c906.ptr.json change_count --arg limit=20
```

> The `extra.timeout` knobs matter: a slow single node spends a few seconds per
> index-create, and a build makes several — without a raised timeout the client
> can give up mid-build.

**What works on each backend.** The waveform queries (`at`, `window`, `find`,
`stats`), the prompt library (for prompts that carry a `dsl_json`), the cache,
and the bug KB all run on OpenSearch. **SQLite-only** today: `show` / `modules`,
the whole `xz` group, and the 18 prompts without a `dsl_json` (the `sim_*`
family, the RTL `*_of_module` queries, and the cross-index joins) — these report
a clear "sqlite-only" message rather than returning wrong results. See §17.

---

## 14. MCP — query from an AI agent

`xevdb mcp <db>` serves one dataset to an AI agent over the **Model Context
Protocol** (stdio, JSON-RPC) — so Claude (or any MCP client) queries the
deterministic evidence directly instead of guessing. No extra dependency; the
protocol is built in.

```sh
xevdb mcp counter.vcd.xevdb       # a waveform dump
xevdb mcp riscv.ptr.json          # or the RISC-V / kernel reference
```

Register it in any MCP client (Claude Desktop / Claude Code / …):

```json
{
  "mcpServers": {
    "xevdb-counter": { "command": "xevdb", "args": ["mcp", "/abs/path/counter.vcd.xevdb"] },
    "xevdb-riscv":   { "command": "xevdb", "args": ["mcp", "/abs/path/riscv.ptr.json"] }
  }
}
```

**Tools exposed:** `stats`, `find_signals`, `signal_value_at`, `signal_window`,
`list_prompts`, `run_prompt` (the whole stored library — including RISC-V/kernel
decode, e.g. `run_prompt riscv_csr_by_addr {addr:"0x305"}`), `search_bugs`,
`show_source` (RTL, sqlite backend), and `decode_instruction` / `decode_signal`
(disassemble a word, or the word on a signal — §11). One server serves one
dataset, so point separate servers at a dump and at
the reference DB — the normal MCP multi-server pattern. Tool errors come back as
content (the agent sees them), not as protocol failures.

The principle is **evidence-first**: the agent calls a tool, gets exact rows
back, and reasons over *facts the tool proved* — it never has to recall a CSR
number or a signal's value from memory.

---

## 15. Raw SQL

A `.xevdb` is a normal SQLite file. When the commands and prompts do not fit,
drop to SQL:

```sh
sqlite3 counter.vcd.xevdb \
  "SELECT s.fullname, COUNT(*) n
     FROM changes c JOIN signals s ON s.id = c.sig_id
    GROUP BY c.sig_id ORDER BY n DESC LIMIT 10;"
```

Core schema (see the README for the full DDL):

```
signals(id, hier, name, fullname, width, kind)
changes(sig_id, t, value)                       -- indexed (sig_id, t)
source_files(path, content, hash, ingested_at)
modules(id, name, kind, file, line_start, line_end, ...)
module_ports / module_signals / module_instances(...)
sim_runs(id, name, source, n_events, n_fatal, ...)
sim_events(id, run_id, line_no, severity, t, ref_file, ref_line, message)
bugs(name, title, status, severity, symptom, root_cause, fix, ...)
bug_links(bug_name, kind, value, extra)
prompts(name, description, sql, dsl_json, params_json, ...)
cache(key, prompt_name, args_json, result_json, hits, ...)
```

To turn a good ad-hoc query into a reusable one, `prompt add` it. (Raw SQL is a
SQLite-backend feature; on OpenSearch, express the query as a `dsl_json` prompt.)

---

## 16. Debugging cookbook

**"My sim hangs — what is the CPU PC doing?"**
```sh
xevdb find   d.xevdb '*reg_pc*'
xevdb window d.xevdb reg_pc --from 0 --to 100000
xevdb show   d.xevdb reg_pc                 # the RTL that declares it
```

**"Half my waveform is red."**
```sh
xevdb xz summary   d.xevdb                  # how bad, and the root set
xevdb xz first     d.xevdb --limit 10       # earliest offenders
xevdb xz propagate d.xevdb <top offender>   # what it poisoned
```

**"The sim printed an error — show me the RTL."**
```sh
xevdb ingest-sim d.xevdb run.log
xevdb prompt run d.xevdb sim_errors
xevdb prompt run d.xevdb sim_with_rtl       # errors joined to parsed modules
xevdb show       d.xevdb <file>:<line>      # the exact source window
```

**"We've hit this before — what was it?"**
```sh
xevdb bug search d.xevdb "fifo_full after reset"
xevdb prompt run d.xevdb bugs_for_signal --arg signal=mem_axi_araddr
```

**"What does this trap/opcode mean?"** (with the reference DB)
```sh
xevdb prompt run riscv.ptr.json kernel_trap_by_code  --arg code=13 --arg kind=exception
xevdb prompt run riscv.ptr.json riscv_csr_by_addr    --arg addr=0x305
xevdb prompt run riscv.ptr.json kernel_syscall_by_nr --arg nr=64
xevdb riscv-decode 0x00450513                        # a raw instruction word
xevdb decode cpu.xevdb fetch_insn --time 41200       # the word on a signal at T
```

**"Which signals never toggle?"** (tie-off / dead-logic hunt)
```sh
xevdb prompt run d.xevdb stuck_at --arg limit=100
```

**"Is the clock even running?"**
```sh
xevdb prompt run d.xevdb clock_period
```

**"What instantiates what?"**
```sh
xevdb prompt run d.xevdb instance_tree --arg module=top
```

---

## 17. Backends at a glance

| Capability | SQLite (`.xevdb`) | OpenSearch (pointer) |
|---|---|---|
| `build` / `ingest-rtl` / `ingest-sim` | ✅ | ✅ |
| `at` / `window` / `find` / `stats` | ✅ | ✅ |
| Prompts with a `dsl_json` (24/42) | ✅ | ✅ |
| Prompts without `dsl_json` (`sim_*`, RTL joins) | ✅ | ⚠️ sqlite-only |
| `show` / `modules` (RTL source) | ✅ | ⚠️ sqlite-only |
| `xz` tracing | ✅ | ⚠️ sqlite-only |
| Bug KB | ✅ | ✅ |
| Raw SQL | ✅ | — (use `dsl_json` prompts) |
| RISC-V / kernel reference | — | ✅ |
| Result cache | ✅ | ✅ |
| MCP server | ✅ | ✅ |
| Artifact | one file you can email | a JSON pointer to the cluster |
| Best for | one person, one dump | a team, large dumps, the reference DB |

Pick with `--backend sqlite|opensearch`, `$XEVDB_BACKEND`, or automatically (a
JSON pointer file routes to OpenSearch).

---

## 18. Troubleshooting

- **`sv-parse binary not found`** — the SV parser isn't built. Run
  `bash install.sh` (or set `XEVDB_SV_PARSE` to the binary). Waveform/X/Z/prompt
  features still work without it.
- **`ConnectionTimeout` on an OpenSearch build/ingest** — the cluster is slow
  creating indices. Raise the pointer's `extra.timeout` (§13).
- **A prompt returns stale rows after re-ingesting** — the cache is keyed by
  prompt + args; run `xevdb cache clear <db>` (or `prompt run --no-cache`).
- **`... is only available on a relational (sqlite) backend`** — you ran `xz` /
  `show` / `modules`, or an SQL-only prompt, against OpenSearch. Use the SQLite
  backend for those (§17).
- **`the 'opensearch' backend is not available`** — install it with
  `bash install.sh --with-opensearch` (or `pip install -e '.[opensearch]'`).
- **Ambiguous signal name** — qualify it (`top.dut.clk` instead of `clk`); see
  the resolution order in §4.

---

## 19. Command reference

```
build         <vcd> [--db PATH] [--reset] [--no-seed]
build-xtrace  <xtrace> [--db PATH] [--reset] [--no-seed]
at            <db> <signal> --time T [--json]
window        <db> <signal> [--from T0] [--to T1] [--limit N] [--json]
find          <db> <pattern> [--limit N] [--json]
stats         <db> [--json]

ingest-rtl    <db> <path> [--reset]
modules       <db> [--filter NAME] [--limit N] [--json]      # sqlite
show          <db> <target> [--full] [--context N] [--no-line-numbers]  # sqlite

ingest-sim    <db> <log> [--name N] [--keep-all] [--reset]

ingest-riscv  <ptr> [--data DIR] [--reset] [--no-seed]                       # OpenSearch
ingest-kernel <ptr> [--kernel-tree DIR] [--data DIR] [--reset] [--no-seed]   # OpenSearch

riscv-decode  <word>... [--json]                             # decode literal instruction word(s)
decode        <db> <signal> --time T [--json]                # decode the word on a signal at T

xz summary    <db> [--json]                                  # sqlite
xz first      <db> [--limit N] [--json]                      # sqlite
xz signal     <db> <signal> [--json]                         # sqlite
xz at         <db> --time T [--limit N] [--json]             # sqlite
xz propagate  <db> <seed> [--window W] [--limit N] [--json]  # sqlite

prompt list/show/run/add/remove
cache  stats/list/clear
bug    add/show/list/search/link/close/remove

mcp    <db>                              # serve the dataset to AI agents over MCP (stdio)
```

Global: `--backend sqlite|opensearch` (or `$XEVDB_BACKEND`) selects the storage
backend. Every command takes `-h` / `--help`. `XEVDB_NO_CACHE=1` disables the
cache process-wide.
