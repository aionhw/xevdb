# Introduction to xevdb — from a single waveform to AI FPGA debug

xevdb turns the artifacts of a hardware run — a **waveform**, its **RTL**, and the
**simulator/log output** — into one *queryable* thing. Instead of scrolling a
waveform viewer or `grep`-ing a VCD, you ask questions:

> *What is `reg_pc` at t=200? Where is it declared? Which signals went X out of
> reset? What bugs have we seen on this block before?*

This guide climbs from the simplest possible use to a full **AI-assisted FPGA
debug** pipeline. Each level adds one idea and builds on the last.

```
Level 1  one file              VCD + RTL  →  .xevdb  →  query signals + show source
Level 2  real RTL              X/Z hunting on a CPU core (picorv32)
Level 3  memory                a searchable bug knowledge base
Level 4  scale                 the OpenSearch backend (team-shared, large dumps)
Level 5  trace-native          XTrace: sample/transaction captures, not just VCD
Level 6  AI debug for FPGA     ChipScoPy → agents → Claude/Codex  (xevdb_ai_debug)
```

The throughline: **every level stores deterministic, queryable evidence first**;
intelligence (search, cross-refs, an LLM) is layered *on top of* that evidence,
never in place of it.

---

## Level 1 — One file: query a waveform + its source

A `.xevdb` is a normal SQLite file holding the VCD signals/changes, the parsed
RTL (modules, ports, signals, **full source text**), the sim log, a library of
stored query "prompts", and a result cache.

```sh
cd xevdb && bash install.sh && source .venv/bin/activate

# build a database from a VCD, then add the RTL
xevdb build       examples/simple/counter.vcd
xevdb ingest-rtl  examples/simple/counter.vcd.xevdb  examples/simple/

# waveform question: value of a signal at a time (correct last-change semantic)
xevdb at     examples/simple/counter.vcd.xevdb  top.u_cnt.count --time 25
# → top.u_cnt.count  @25  last_t=25  value=00000010

# source question: show the RTL that declares it (sliced from the DB, no files needed)
xevdb show   examples/simple/counter.vcd.xevdb  counter
xevdb find   examples/simple/counter.vcd.xevdb  "*count*"
```

**The idea:** the waveform query (*what was the value at T*) and the RTL lookup
(*show me the code*) live in one artifact you can hand to a teammate or pipe
through `jq`.

---

## Level 2 — Real RTL: hunt the X/Z

On a real CPU core, the first debug question is usually *"why is my sim full of
red?"* — unknown (`x`) / high-impedance (`z`) states. xevdb traces them with the
correct waveform semantic and bridges each one to the RTL that drives it.

```sh
bash demos/picorv32/run.sh        # builds a picorv32 .xevdb and runs the queries

xevdb xz summary   picorv32.xevdb              # how widespread + the root-cause set
xevdb xz first     picorv32.xevdb --limit 10   # earliest signals to go X (root first)
xevdb xz propagate picorv32.xevdb testbench.top.mem_axi_araddr --window 30000
```

**The idea:** `xz first` ranks signals by *when they first went X* — the top of
the list is the root cause; everything else inherited it. `xz propagate` follows
the blast radius and, where RTL is ingested, points at the declaring `file:line`.

---

## Level 3 — Memory: a searchable bug knowledge base

Debugging produces knowledge — *what was wrong, why, and how it was fixed*.
xevdb stores it as named, keyworded, full-text-searchable **bugs** linked to the
signals, modules, and `file:line`s they touch.

```sh
xevdb bug add  picorv32.xevdb "axi-fifo-xprop" --severity error \
  --symptom   "fifo_full goes X after reset" \
  --root-cause "uninitialized temp array read past populated range" \
  --fix       "pre-init temp arrays before \$readmemh" \
  --signal testbench.top.mem_axi_araddr --module axi_fifo --ref picorv32.v:176

xevdb bug search picorv32.xevdb "uninitialized reset"
```

**The idea:** the same database that holds *this run's* evidence becomes the
team's institutional memory — and bugs cross-join back into the waveform/RTL
(`bugs_for_signal`, `bugs_with_rtl`, `xz_signals_with_open_bugs`). Still no AI.

---

## Level 4 — Scale: the OpenSearch backend

A `.xevdb` file is perfect until a dump is too big for one file or needs to be
shared. xevdb stores the *same* dataset behind a pluggable backend; the only
change is a flag and a tiny JSON **pointer file** instead of the SQLite file.

```sh
export XEVDB_OPENSEARCH_HOSTS=localhost:9200
xevdb --backend opensearch build wave.vcd --db wave.xevdb   # creates the pointer
xevdb prompt run wave.xevdb change_count                    # auto-routes to the cluster
```

A worked, real-silicon example is in **[`examples/c906_opensearch/`](examples/c906_opensearch/)**:
run the XuanTie **C906** core's `hello` test on a simulator, capture a scoped
VCD, and load 50k+ signals / 1.4M changes into OpenSearch — then query X/Z,
signals, and run aggregation prompts straight from the cluster.

```sh
cd examples/c906_opensearch && ./run_tutorial.sh        # opensearch | sim | filter | ingest | query
```

**The idea:** one logical surface (`build`, `at`, `find`, `prompt`, `bug`), two
storage engines. Stored prompts carry both SQL (for SQLite) and an OpenSearch
DSL template (for the cluster). Pick the backend by scale; the questions don't
change.

> What's relational-only on OpenSearch: `show`, `modules`, and `xz` read via
> hand-written SQL and stay SQLite-only (they fail fast with a clear message).

---

## Level 5 — Trace-native: XTrace

VCD is signal-toggle oriented. Hardware debug is often **sample**- or
**transaction**-oriented (an ILA window, an AXI burst). xevdb accepts a
lightweight **XTrace** text format directly:

```
xtrace.version 1.0
signal s_axi_awvalid width=1
signal s_axi_awaddr  width=32
@96 s_axi_awvalid=1 s_axi_awaddr=0x40001000
@97 s_axi_awvalid=1 s_axi_awaddr=0x40001000
```

```sh
xevdb build-xtrace capture.xtrace --db capture.xevdb     # same .xevdb, same queries
```

**The idea:** the storage/query layer doesn't care where the samples came from —
a simulator VCD or a board capture both become the same signal/change rows.
This is the seam the FPGA layer plugs into.

---

## Level 6 — AI debug for FPGA (`xevdb_ai_debug`)

The top layer adds AMD/Xilinx hardware and an LLM, in a deliberate **two-plane**
design (see **[`xevdb_ai_debug/`](xevdb_ai_debug/)**):

```
ChipScoPy ILA capture (real board, or synthetic JSON)
  → Protocol Agent      deterministic AXI/AXIS valid-ready stall detection
  → XTrace              → xevdb build-xtrace → .xevdb dataset (Levels 4–5)
  → Orchestrator        builds compact evidence: summary + events + a signal window
  → Model connector     Claude (`claude -p`) or Codex (`codex exec`) — or none
  → FastAPI + dashboard
```

- **Deterministic data plane** (the trustworthy part): a ChipScoPy capture is
  reduced to *facts* — "AW stalled for 48 cycles, samples 96→143, because
  `awvalid` held high while `awready` stayed low." Those facts are computed by
  rules, stored in xevdb, and are auditable.
- **Reasoning plane** (the optional part): the orchestrator sends only the
  **compact evidence** (never raw traces) to Claude or Codex for a hypothesis
  and next probes. With no model available it falls back to deterministic text.

```sh
cd xevdb_ai_debug && pip install -r requirements.txt

# generate a failing capture (a library of scenarios ships with it)
python scripts/generate_synthetic_capture.py --list
python scripts/generate_synthetic_capture.py --scenario axi_read_hang --out cap.json

# capture → protocol events → real .xevdb → ask the agent
python scripts/ingest_capture.py --capture cap.json --state state.json \
  --xevdb-db cap.xevdb --session DBG-RD-001
python scripts/ask_agent.py --state state.json --session DBG-RD-001 \
  --question "Why did the read stall?" --model claude
```

**The idea:** the LLM never sees the waveform — it sees the *evidence xevdb
already extracted*. That keeps the answer grounded, cheap, and explainable, and
means you can swap Claude for Codex (or turn the model off entirely) without
changing the data plane.

---

## The stack at a glance

| Layer | Adds | AI? |
| --- | --- | --- |
| **vcdb** | VCD-only DB + prompts + cache | no |
| **xevdb** | + RTL (code display), sim log, X/Z, bug KB, OpenSearch, XTrace | no |
| **xevdb_ai_debug** | + ChipScoPy capture + protocol agents + Claude/Codex reasoning | yes |

Read top-down, it's a debug *memory* that grows from a single file to a
team-scale, AI-assisted FPGA bring-up tool — without ever giving up the
deterministic, queryable core underneath.

### Where to go next

- `README.md` — full command surface, schema, and the seeded prompt library.
- `USER_GUIDE.md` — task-oriented recipes.
- `examples/simple/` — the 4-signal fixture used in Level 1.
- `examples/c906_opensearch/` — the real-silicon OpenSearch walkthrough (Level 4).
- `xevdb_ai_debug/` — the FPGA AI-debug MVP (Level 6).
- `docs/riscv-reference-tutorial.md` / `docs/kernel-reference-tutorial.md` — a
  standalone, searchable **RISC-V ISA + Linux kernel architecture** database on
  OpenSearch (decode an instruction, register, CSR, syscall, or trap when reading
  a RISC-V trace — by hand or from an AI agent).
