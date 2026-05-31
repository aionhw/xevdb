# Tutorial: c906 "hello" on xezim → VCD → OpenSearch debug database

This walks through running the XuanTie **C906** RISC-V core's `hello` test on the
**xezim** simulator, capturing a waveform (VCD), and loading the waveform plus
debug information (RTL + simulator log) into **OpenSearch** using **xevdb**, so
you can query signals, hunt X/Z, and record bugs from a search engine.

Everything below was executed end-to-end; sample outputs are real.

---

## 0. Components & paths

| Piece | Path |
| --- | --- |
| xezim simulator | `~/repo/sv2023/xezim/target/release/xezim` |
| C906 design (rtlmeter) | `~/repo/rtlmeter/designs/XuanTie-C906` |
| C906 compile workdir (filelist + sources) | `~/repo/rtlmeter/work/XuanTie-C906/default/compile-0` |
| xevdb (this repo — this example is `examples/c906_opensearch/`) | `../../` |
| OpenSearch 2.17 (single node) | `~/os-run/os` |

> Commands below are run from this example directory
> (`xevdb/examples/c906_opensearch/`), so the xevdb checkout is `../../` and its
> CLI is `../../.venv/bin/xevdb` (or just `xevdb` after `source ../../.venv/bin/activate`).

---

## 1. Start a single-node OpenSearch

No cluster handy? A throwaway single node is enough.

```sh
cd ~/os-run
curl -L -o opensearch.tar.gz \
  https://artifacts.opensearch.org/releases/bundle/opensearch/2.17.1/opensearch-2.17.1-linux-x64.tar.gz
tar -xzf opensearch.tar.gz && mv opensearch-2.17.1 os
cat >> os/config/opensearch.yml <<'YML'
discovery.type: single-node
plugins.security.disabled: true        # plain HTTP on :9200, no auth (dev only)
YML
OPENSEARCH_JAVA_OPTS="-Xms1g -Xmx1g" os/bin/opensearch &
# wait until green:
curl -s localhost:9200/_cluster/health | grep -o '"status":"[a-z]*"'
```

> The bundled JDK is used automatically — no system Java needed. Security is
> disabled so xevdb talks plain HTTP with no credentials. **Dev only.**

---

## 2. Run c906 `hello` on xezim and dump a VCD

rtlmeter already prepared the C906 source list for Verilator; we reuse it for
xezim. xezim emits a VCD only when the testbench calls `$dumpfile/$dumpvars`,
and the stock `tb.v` has neither — so we make a dump-enabled copy of `tb`.

```sh
cd ~/repo/rtlmeter/work/XuanTie-C906/default/compile-0

# dump-enabled tb: inject $dumpfile/$dumpvars right after `module tb();`
python3 - <<'PY'
src = open("verilogSourceFiles/tb.v").read()
dump = ('module tb();\n'
        'initial begin\n'
        '  $dumpfile("c906_hello.vcd");\n'
        '  $dumpvars(0, x_soc);\n'          # see the SCOPE GOTCHA below
        'end\n')
open("tb_dump.v","w").write(src.replace("module tb();\n", dump, 1))
PY

# xezim filelist: the rtlmeter list, with tb.v swapped for the dump version
sed 's#^verilogSourceFiles/tb.v$#tb_dump.v#' filelist > filelist.xezim

# the hello program images the testbench $readmemh's (must be in cwd)
ln -sf ~/repo/rtlmeter/designs/XuanTie-C906/tests/hello/inst.pat inst.pat
ln -sf ~/repo/rtlmeter/designs/XuanTie-C906/tests/hello/data.pat data.pat

# run (top = tb; same incdir/define rtlmeter used for Verilator)
~/repo/sv2023/xezim/target/release/xezim -s tb \
  +incdir+verilogIncludeFiles +define+__RTLMETER_MAIN_CLOCK=tb.clk \
  -f filelist.xezim --max-time 50000000
```

Result — the test passes and a VCD is written:

```
********* Init Program *********
Hello Friend!
*    simulation finished successfully        *
TEST PASSED
[VCD] dumping 1427936 signals (filter_scopes=1)
Simulation finished at time 33255  ($finish called)
```

### ⚠ The `$dumpvars` scope gotcha (important)

xezim resolves a `$dumpvars(0, <scope>)` argument through its **signal**-name
resolver. A deep *instance* path that is not itself a signal
(`tb.x_soc.…​.x_aq_rtu_top`) is collapsed to its **leaf segment**
(`x_aq_rtu_top`), which never prefix-matches the `x_soc.`-rooted signal names —
so you get **0 signals dumped** (a 163-byte empty VCD). Two consequences:

- The top instance name is **not** part of internal signal names — use
  `x_soc`, not `tb.x_soc`.
- Only a **root-segment** scope works for subtree selection. `$dumpvars(0, x_soc)`
  dumps the whole chip (1.43M signals here). To get a *sub-scope*, dump the
  full chip and **post-filter the VCD** (next step).

---

## 3. Filter the VCD to the units you care about

The full c906 VCD is ~150 MB / 1.43M signals. For this tutorial we keep just
the **instruction-fetch (IFU)** and **retire (RTU)** units — the natural
"fetch → retire" view for a hello-world run:

```sh
python3 ~/os-run/vcd_scope_filter.py \
  ~/repo/rtlmeter/work/XuanTie-C906/default/compile-0/c906_hello.vcd \
  ~/os-run/tutorial/c906_hello_ifu_rtu.vcd \
  x_aq_ifu_top x_aq_rtu_top
# kept 53442 signals under ['x_aq_ifu_top', 'x_aq_rtu_top']  (≈12.6 MB)
```

`vcd_scope_filter.py` keeps every `$var` whose enclosing scope path contains one
of the named instances, preserving the `$scope` nesting and the value-change
records for the kept ids.

---

## 4. Load VCD + debug info into OpenSearch with xevdb

xevdb stores a dataset behind a backend. We point it at OpenSearch via a small
**pointer file** (JSON naming the cluster + a dump id). The slow single node
needs a generous client timeout, set via `extra`:

```sh
cd ~/os-run/tutorial
cat > c906_hello.xevdb <<'JSON'
{ "backend": "opensearch", "hosts": ["localhost:9200"],
  "dump_id": "c906_hello", "prefix": "xevdb",
  "extra": {"timeout": 180, "max_retries": 3, "retry_on_timeout": true} }
JSON

XV=../..        # xevdb repo root, relative to examples/c906_opensearch/

# (a) waveform: the filtered VCD  →  signals + changes + seed prompts
$XV/.venv/bin/xevdb build c906_hello_ifu_rtu.vcd --db c906_hello.xevdb

# (b) debug info — RTL: parse the IFU/RTU Verilog (modules, ports, signals, source)
$XV/.venv/bin/xevdb ingest-rtl c906_hello.xevdb rtl/

# (c) debug info — simulator log: severities, sim-time, file:line refs
$XV/.venv/bin/xevdb ingest-sim c906_hello.xevdb c906_hello.simlog
```

```
built … : 53442 signals, 1425639 changes, t in [0, 33255]
ingested 27 files … : 27 modules, 1134 ports, 2252 internal signals, 90 instantiations
ingested 23 lines … : 0 events       # the hello tb only $display's; no severity lines
```

> **Two gotchas you'll hit:**
> 1. Index creates take ~3 s each on a fresh single node, so a 13-index build
>    blows past the default 10 s client timeout → set `extra.timeout` in the
>    pointer (done above).
> 2. A module's full AST JSON can exceed Lucene's 32 766-byte keyword
>    doc-values limit. xevdb maps those blob fields with `doc_values:false`;
>    if you built an index with an older xevdb, drop and re-ingest it.

Indices land under `xevdb-c906_hello-*`:

```
xevdb-c906_hello-changes           1425639   107.3mb
xevdb-c906_hello-signals             53442     2.7mb
xevdb-c906_hello-modules                27   922.3kb
xevdb-c906_hello-module_ports         1134   130.7kb
xevdb-c906_hello-module_signals       2252     305kb
xevdb-c906_hello-bugs                    1     9.2kb
```

---

## 5. Debug from OpenSearch

All queries below run against the cluster (no local DB file). The pointer file
auto-routes xevdb to the OpenSearch backend.

```sh
DB=~/os-run/tutorial/c906_hello.xevdb
XV=../../.venv/bin/xevdb        # the xevdb CLI, relative to examples/c906_opensearch/
```

**Dataset overview**

```sh
$XV stats $DB
```

```
n_signals     53442
n_changes     1425639
t_min 0   t_max 33255
row_counts:  signals 53442  changes 1425639  modules 27  module_ports 1134  module_signals 2252
```

**Find signals** (e.g. the IFU program counter, retire valids)

```sh
$XV find $DB "pc_" --limit 8
$XV find $DB "retire" --limit 8
```

```
# pc_  (IFU program-counter / mispredict signals)
…x_aq_ifu_top.ifu_iu_ex1_pc_pred       (wire, 40b)
…x_aq_ifu_top.iu_ifu_pc_mispred        (wire,  1b)
…x_aq_ifu_top.iu_ifu_tar_pc_vld        (wire,  1b)
# retire  (IFU ibuf retire-enable lines)
…x_aq_ifu_top.x_aq_ifu_ibuf.entry0_retire0_en   (wire, 1b)
…x_aq_ifu_top.x_aq_ifu_ibuf.entry1_retire0_en   (wire, 1b)
```

**Value of a signal over time** (waveform semantic — last change at-or-before T)

```sh
S=…x_aq_ifu_top.iu_ifu_pc_mispred
$XV at     $DB $S --time 20000
$XV window $DB $S --from 0 --to 6000
```

```
…iu_ifu_pc_mispred   @20000   last_t=100   value=0
# window:
…iu_ifu_pc_mispred   0      x      # X out of reset
…iu_ifu_pc_mispred   100    0      # reset deasserts at #100 → resolves to 0
```

That `x → 0` at t=100 is the reset edge (`tb` deasserts `rst_b` at `#100`) — the
correct waveform semantic, served from OpenSearch.

**Stored prompts** (run their OpenSearch `dsl_json`)

```sh
$XV prompt run $DB change_count       --arg limit=10   # busiest signals
$XV prompt run $DB xz_signals         --arg limit=10   # carried x/z (uninitialized)
$XV prompt run $DB signals_in_scope   --arg prefix=<hier> --arg limit=10
```

```
# change_count → the clocks dominate (toggle every cycle, ~6650 edges)
…x_aq_ifu_top.forever_cpuclk                    6650
…x_aq_ifu_top.x_aq_ifu_btb.btb_clk             6650
# xz_signals → the uninitialized icache data-array SRAMs carry X
…x_aq_ifu_icache_data_array.…ram0.mem          2048
…x_aq_ifu_icache_data_array.…ram1.mem          2048
# signals_in_scope (RTU) → retire-unit signals
…x_aq_rtu_top.cp0_rtu_ex1_cmplt                (1b wire)
…x_aq_rtu_top.cp0_rtu_ex1_chgflw_pc            (40b wire)
```

The `xz_signals` hit is a real finding: the I-cache data RAMs have no reset and
`hello` touches only a few lines, so most ways stay **X** the whole run.

**Record a bug** (full-text searchable, linked to signals/modules/refs)

```sh
$XV bug add $DB "ifu-reset-xprop" --severity warning \
  --symptom "IFU signals carry X out of reset before first fetch" \
  --signal <sig> --module aq_ifu_top --ref aq_ifu_top.v:1
$XV bug search $DB "reset X"
```

```
$ xevdb bug add  … "ifu-icache-uninit-x" --severity warning \
      --symptom "IFU icache data-array RAMs hold X for the whole hello run" \
      --module aq_ifu_icache_data_array --signal …ram0.mem --keyword icache --keyword xprop
stored bug 'ifu-icache-uninit-x'
$ xevdb bug search … "uninitialized icache"
ifu-icache-uninit-x   [open/warning]
```

> **What's relational-only:** `xevdb show`, `modules`, and `xz` use hand-written
> SQL and are SQLite-only; on OpenSearch they fail fast with a clear message.
> Cross-index-join prompts (`*_with_rtl`) are likewise SQLite-only. The waveform
> queries, single-index prompts, and the bug KB all work on OpenSearch.

---

## 6. Inspect in OpenSearch directly

The data is plain OpenSearch — query it with anything:

```sh
# busiest IFU/RTU signals via a raw aggregation
curl -s 'localhost:9200/xevdb-c906_hello-changes/_search' -H 'Content-Type: application/json' -d '{
  "size":0, "aggs":{"busy":{"terms":{"field":"fullname","size":5}}}}'

# distinct signals that ever went X/Z (cardinality aggregation):
curl -s 'localhost:9200/xevdb-c906_hello-changes/_search' -H 'Content-Type: application/json' -d '{
  "size":0, "query":{"term":{"xz":true}},
  "aggs":{"xz":{"cardinality":{"field":"fullname"}}}}'
# → "xz": { "value": 3060 }        3060 distinct IFU/RTU signals carried X/Z
```

---

## Summary of what was produced

| Artifact | |
| --- | --- |
| Sim | `xezim` ran C906 `hello` → **TEST PASSED** at sim-time 33255 |
| Full VCD | 1.43M signals, ~150 MB (`compile-0/c906_hello.vcd`) |
| Scoped VCD | 53 442 signals (IFU+RTU), 12.6 MB |
| OpenSearch dump `c906_hello` | 53 442 signals · 1 425 639 changes · 27 modules · 1134 ports · 2252 module signals |
| Debug finding | 3060 signals X/Z; root cause = uninitialized I-cache data RAMs (recorded as a bug) |

All of it is queryable from OpenSearch with `xevdb` or plain `curl`.
