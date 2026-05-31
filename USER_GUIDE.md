# xevdb User Guide

A practical walkthrough of `xevdb` — from a raw `.vcd` to a queryable
debug database. The [README](README.md) is the reference; this guide is
the tutorial.

---

## 1. The mental model

`xevdb` turns three separate debug artifacts into **one SQLite file**:

| Side | Input | What it gives you |
|---|---|---|
| **Waveform** | a `.vcd` dump | every signal value-change, queryable by time |
| **RTL** | `.v` / `.sv` files | parsed modules, ports, signal declarations, *full source text* |
| **Simulator** | a sim log | severity-classified events with `file:line` refs |

The output is a `.xevdb` file. It is a *plain SQLite database* — you can
open it with `sqlite3`, hand it to a teammate, or pipe queries through
`jq`. Once built, **the original `.vcd` / `.sv` / `.log` are no longer
needed** — their content lives inside the database.

The point: when you are debugging a sim hang, the question is never just
"what is signal X at time T" — it is "what is X at T, *and* show me the
always-block that drives it, *and* the error the sim printed nearby."
`xevdb` answers all three from one file.

---

## 2. Install

```sh
bash install.sh           # venv + clone/build the SV parser + smoke-test
source .venv/bin/activate
```

Requires Python ≥ 3.10, Rust ≥ 1.75 (`cargo`), and `git`. The
SystemVerilog parser is not vendored — `install.sh` clones it from
`aionhw/xezim-core` into `./xezim-core/` and builds it; re-running the
installer `git pull`s that checkout. Override the source with the
`XEZIM_CORE_REPO` / `XEZIM_CORE_REF` environment variables. For very
large dumps there is an optional DuckDB backend:
`bash install.sh --with-duckdb`.

No Rust? `bash install.sh --no-rust` still installs the waveform and
X/Z sides; only the RTL features are skipped.

Verify:

```sh
xevdb --help
```

---

## 3. Your first database

Everything starts with `build`:

```sh
xevdb build examples/simple/counter.vcd
# → writes examples/simple/counter.vcd.xevdb
```

`build` parses the VCD's signal definitions and every value change, then
seeds a library of ~20 stored query prompts. Useful flags:

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
# row_counts: signals 4, changes 16, prompts 20 ...
```

---

## 4. Workflow A — waveform-only debugging

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

This uses the **correct waveform semantic**: the value *in effect* at
`t=25` is the last change with `change.t <= 25`. A naive `grep` of the
VCD would miss it whenever the signal didn't happen to change exactly
at 25.

### `window` — changes over a range

```sh
xevdb window counter.vcd.xevdb count --from 0 --to 40
xevdb window counter.vcd.xevdb count --from 0 --to 40 --limit 200 --json
```

### `stats` — the lay of the land

```sh
xevdb stats counter.vcd.xevdb
```

Signal name resolution, used by `at` / `window` / `xz signal`:

1. exact `sig_id` (the VCD short code), then
2. exact `fullname` (`top.u_cnt.count`), then
3. unique bare `name` (`count`), then
4. unique `*.suffix` match.

If a bare name is ambiguous (`clk` in three scopes) the command fails
and asks for a fully-qualified `hier.name`.

---

## 5. Workflow B — add the RTL

Waveform values gain meaning when joined to the source that produced
them. `ingest-rtl` parses `.v` / `.sv` files into the same database:

```sh
xevdb ingest-rtl counter.vcd.xevdb examples/simple/        # a dir, or a single file
```

It stores parsed modules/ports/signals/instances **and the full source
text**. Now you can ask RTL questions:

```sh
xevdb modules counter.vcd.xevdb                     # list every module
xevdb modules counter.vcd.xevdb --filter cnt        # name filter

xevdb show counter.vcd.xevdb counter                # module header
xevdb show counter.vcd.xevdb counter --full         # whole body
xevdb show counter.vcd.xevdb en                     # a signal → owning module
xevdb show counter.vcd.xevdb examples/simple/counter.sv:14 # a file:line window
```

`xevdb show` slices code straight out of the `source_files` table — the
`.sv` files do **not** need to be on disk at query time.

---

## 6. Workflow C — add the simulator log

```sh
xevdb ingest-sim counter.vcd.xevdb run.log
```

The log parser canonicalizes severity (`UVM_FATAL` / `UVM_ERROR` /
`UVM_WARNING` / `UVM_INFO` / `ERROR` / `WARNING` / `ASSERTION` / `FATAL`
/ `NOTE` / `INFO`), recovers the simulation time from common encodings
(`@ 200:`, `[t=200]`, `# 100:`, `at time 100`, `t=200ns`), and extracts
any `file.sv:NNN` reference. Each ingest adds one `sim_runs` row and one
`sim_events` row per matched line. `--keep-all` retains non-severity
lines too.

Query the sim side through the seed prompts (`sim_errors`,
`sim_around_time`, …) or raw SQL.

---

## 7. X/Z tracing

The most common "my sim is full of red" debug. A value is *X/Z-dirty*
when it carries any `x` / `X` / `z` / `Z` bit. The `xz` command group
turns that into the questions you actually ask.

### Step 1 — is there X/Z, and where does it start?

```sh
xevdb xz summary picorv32.vcd.xevdb
# X/Z signals    :     8870 / 11164 (79.5%)
# X/Z changes    :     8987 / 35876 (25.1%)
# first X/Z at t : 0
# last  X/Z at t : 1620000
# root-cause set : 50 signal(s) go X/Z at t=0
#     testbench.top.mem.latched_raddr
#     ...
```

The **root-cause set** is every signal that goes X/Z at the earliest
X/Z timestamp. Everything else inherits from it.

### Step 2 — rank the root causes

```sh
xevdb xz first picorv32.vcd.xevdb --limit 10
#    first_t  kind    w    #xz  signal
#          0  x      32      1  testbench.top.mem.latched_raddr
#          0  x       1      1  testbench.top.mem.latched_rinsn
#          ...
```

Signals sorted by *when* they first went dirty. The top of the list is
where to look first.

### Step 3 — one signal's history

```sh
xevdb xz signal picorv32.vcd.xevdb reg_pc
# testbench.top.uut.picorv32_core.reg_pc   <never X/Z>

xevdb xz signal picorv32.vcd.xevdb mem_la_addr
# ...mem_la_addr  (2 X/Z interval(s))
#   [x ] t=0 → t=1010000 (1010000 ticks)  enter=x00
#   [x ] t=1030000 → (end of trace)  enter=x00
```

Each line is an enter→leave interval. `→ (end of trace)` means the
signal was still X/Z when the dump ended — usually a real bug.

### Step 4 — snapshot at an instant

```sh
xevdb xz at picorv32.vcd.xevdb --time 200000
# 6 signal(s) in X/Z at t=200000:
#    since_t  kind    w  signal = value
#          0  x      32  ...latched_raddr = x
```

`since_t` is when that X/Z value was set — so a small `since_t` with a
large query time means the signal has been stuck for a long time.

### Step 5 — trace the propagation

```sh
xevdb xz propagate picorv32.vcd.xevdb testbench.top.mem_axi_araddr --window 30000
# seed testbench.top.mem_axi_araddr first X/Z at t=0
# 12 propagation candidate(s):
#       +dt     first_t  kind  signal  [rtl]
#         0           0  x     ...latched_raddr  [axi4_memory @ testbench.v:358]
#         ...
```

Give it a seed; it lists every signal that turns X/Z **at or after** the
seed, ranked by how soon (`+dt`). `--window W` limits to
`[seed_t, seed_t + W]`. If RTL was ingested, each candidate shows the
`module @ file:line` that declares a same-named signal — the bridge from
"this went X" straight to the RTL that drives it.

All `xz` subcommands accept `--json`.

---

## 8. Stored prompts

A prompt is a named, parameterized SQL query stored *inside* the
`.xevdb`. `build` seeds ~20.

```sh
xevdb prompt list  counter.vcd.xevdb              # all prompts
xevdb prompt show  counter.vcd.xevdb xz_signals   # description + SQL + params
xevdb prompt run   counter.vcd.xevdb xz_signals --arg limit=20
xevdb prompt run   counter.vcd.xevdb signal_history --arg name=count --json
```

Seeded prompts span all three sides — `signal_transitions`, `stuck_at`,
`clock_period`, `xz_signals` (wave); `ports_of_module`, `instance_tree`
(rtl); `sim_errors`, `sim_around_time` (sim); plus two cross-cutting
ones, `xz_signals_with_rtl` and `sim_with_rtl`.

Write your own:

```sh
xevdb prompt add counter.vcd.xevdb wide_buses \
  --sql "SELECT fullname, width FROM signals WHERE width >= :min ORDER BY width DESC" \
  --params-json '[{"name":"min","default":8,"type":"int"}]'

xevdb prompt run counter.vcd.xevdb wide_buses --arg min=16
xevdb prompt remove counter.vcd.xevdb wide_buses
```

Prompts travel with the file: hand someone a `.xevdb` and they get your
queries too.

---

## 9. The result cache

Every `prompt run` result is cached in the database, keyed by prompt
name + arguments.

```sh
xevdb cache stats counter.vcd.xevdb
xevdb cache list  counter.vcd.xevdb --prompt xz_signals
xevdb cache clear counter.vcd.xevdb --prompt xz_signals --yes
xevdb cache clear counter.vcd.xevdb --yes            # all entries
```

Bypass it for one invocation with `XEVDB_NO_CACHE=1`, or per-prompt with
`prompt run --no-cache`. `--ttl N` sets a per-result expiry.

---

## 10. Raw SQL

A `.xevdb` is a normal SQLite file. When the commands and prompts do not
fit, drop to SQL:

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
module_ports(module_id, position, name, direction, width, kind)
module_signals(module_id, name, kind, line, width, decl_text)
module_instances(parent_module_id, child_module_name, instance_name, line)
sim_runs(id, name, source, n_events, n_fatal, n_error, ...)
sim_events(id, run_id, line_no, severity, t, ref_file, ref_line, message)
prompts(name, description, sql, params_json, ...)
cache(key, prompt_name, args_json, result_json, hits, ...)
```

To turn a good ad-hoc query into a reusable one, `prompt add` it.

---

## 11. Debugging cookbook

**"My sim hangs — what is the CPU PC doing?"**
```sh
xevdb find  d.xevdb '*reg_pc*'
xevdb window d.xevdb reg_pc --from 0 --to 100000
xevdb show  d.xevdb reg_pc                 # the RTL that declares it
```

**"Half my waveform is red."**
```sh
xevdb xz summary d.xevdb                   # how bad, and the root set
xevdb xz first   d.xevdb --limit 10        # earliest offenders
xevdb xz propagate d.xevdb <top offender>  # what it poisoned
```

**"The sim printed an error — show me the RTL."**
```sh
xevdb ingest-sim d.xevdb run.log
xevdb prompt run d.xevdb sim_errors
xevdb prompt run d.xevdb sim_with_rtl      # errors joined to parsed modules
xevdb show d.xevdb <file>:<line>           # the exact source window
```

**"Which signals never toggle?" (tie-off / dead-logic hunt)**
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

## 12. Command reference

```
build      <vcd> [--db PATH] [--reset] [--no-seed]
at         <db> <signal> --time T [--json]
window     <db> <signal> [--from T0] [--to T1] [--limit N] [--json]
find       <db> <pattern> [--limit N] [--json]
stats      <db> [--json]

ingest-rtl <db> <path> [--reset]
modules    <db> [--filter NAME] [--limit N] [--json]
show       <db> <target> [--full] [--context N] [--no-line-numbers]

ingest-sim <db> <log> [--name N] [--keep-all] [--reset]

xz summary   <db> [--json]
xz first     <db> [--limit N] [--json]
xz signal    <db> <signal> [--json]
xz at        <db> --time T [--limit N] [--json]
xz propagate <db> <seed> [--window W] [--limit N] [--json]

prompt list/show/run/add/remove
cache  stats/list/clear
```

Every command takes `-h` / `--help`. `XEVDB_NO_CACHE=1` disables the
cache process-wide.
