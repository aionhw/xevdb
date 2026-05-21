# xevdb picorv32 demo

End-to-end run of `xevdb` against the picorv32 RISC-V testbench:

- ~600 KB VCD → 11 K signals / 36 K value changes
- picorv32.v + testbench.v → 11 modules / 171 ports / 323 internal signals
- all in **one 5 MB `.xevdb` SQLite file**, source text included

## Prerequisites

- xevdb installed (`bash ../../install.sh`)
- A picorv32 checkout next to `xevdb/` with `iv.vcd` already generated
  (`make test` in YosysHQ/picorv32). Override with `PICORV32_REPO=/path`
  or `VCD_PATH=/path/to/run.vcd`.

## Run

```sh
bash demos/picorv32/run.sh
```

About 5–10 seconds total. The two RTL files are staged into
`demos/picorv32/rtl_src/` so the file paths recorded in the database stay
readable afterwards.

## What you get (real output)

### 1. Build (waveform) — 0.7 s
```
built picorv32.xevdb: 11164 signals, 35876 changes, t in [0, 21000000]
```

### 2. Ingest RTL — 0.6 s
```
ingested 2 files: 11 modules, 171 ports, 323 internal signals, 9 instantiations
```

### 3. Stats — one file, both sides
```
row_counts:
  signals           11164    ← waveform side
  changes           35876
  source_files      2        ← RTL side
  modules           11
  module_ports      171
  module_signals    323
  module_instances  9
  prompts           15
```

### 4. Where is `reg_pc` declared? (`signal_declaration` prompt)
```
where_  module    file              line  kind  width    decl_text
signal  picorv32  rtl_src/picorv32.v  176   reg   [31:0]  reg [31:0] reg_pc, reg_next_pc, reg_op1, reg_op2, reg_out;
```

One row, exact line, with the full declaration text. This is the answer to
*"why is this signal stuck at X — show me how it's defined"* without
opening any editor.

### 5. `xevdb show picorv32` — render the module header from the database
```
== module picorv32 @ rtl_src/picorv32.v:62-2167  (kind=module ports=27 params=26 always=20 …) ==
      55  `define PICORV32_V
      56
      57
      58  /***************************************************************
      59   * picorv32
      60   ***************************************************************/
      61
>>    62  module picorv32 #(
      63      parameter [ 0:0] ENABLE_COUNTERS = 1,
      64      parameter [ 0:0] ENABLE_COUNTERS64 = 1,
      ...
```

The `>>` marker is on the declaration line. Source comes from the
`source_files` table — works even after the demo cleans up its staging
directory.

### 6. The waveform → RTL bridge (`xz_signals_with_rtl`)

The most useful cross-cutting prompt. Joins VCD signals carrying `x`/`z`
with their RTL declaration:

```
fullname                          width  module       file              decl_line  decl_text
testbench.top.mem.latched_raddr   32     axi4_memory  testbench.v       358        reg [31:0] latched_raddr;
testbench.top.mem.latched_rinsn   1      axi4_memory  testbench.v       362        reg        latched_rinsn;
testbench.top.mem.latched_waddr   32     axi4_memory  testbench.v       359        reg [31:0] latched_waddr;
testbench.top.mem.latched_wdata   32     axi4_memory  testbench.v       360        reg [31:0] latched_wdata;
testbench.top.mem.latched_wstrb   4      axi4_memory  testbench.v       361        reg [ 3:0] latched_wstrb;
```

Each row is "VCD said this signal was X; here is exactly where it's
declared in the SystemVerilog." Five real testbench-memory latches that
don't have a reset clause — the kind of reset-coverage finding that
usually requires `grep -n latched_raddr testbench.v` plus reading.

## Inspect the .xevdb file directly

It's plain SQLite:

```sh
sqlite3 demos/picorv32/picorv32.xevdb 'SELECT name, line_start, line_end FROM modules;'
sqlite3 demos/picorv32/picorv32.xevdb '
    SELECT m.name, COUNT(*) AS n_signals
    FROM module_signals ms JOIN modules m ON m.id = ms.module_id
    GROUP BY m.id ORDER BY n_signals DESC;'

# pull the full source of one module straight from the DB
sqlite3 demos/picorv32/picorv32.xevdb \
    'SELECT substr(content, 1, 200) FROM source_files LIMIT 1;'
```
