# xevdb AI Debug MVP

> **Start here →** [`../INTRODUCTION.md`](../INTRODUCTION.md) puts this
> project in context — the guided tour from a single waveform up to this
> AI-assisted FPGA debug layer (Level 6).

Runnable MVP skeleton for an AMD/Xilinx FPGA debug flow:

```text
Synthetic or real ChipScoPy ILA data
  -> Capture Agent
  -> XTrace Normalizer
  -> xevdb CLI build-xtrace
  -> .xevdb dataset
  -> JSON session/event state
  -> Agent API
  -> model connector (Claude or Codex CLI)
  -> dashboard
```

The MVP runs without FPGA hardware by using synthetic ChipScoPy-shaped data. The canonical waveform capture is handed to `xevdb` as XTrace. The dashboard uses a small JSON state file for decoded sessions/events. OpenSearch is no longer indexed by this app directly; use `xevdb --backend opensearch` when you want the xevdb OpenSearch backend.

## Why this architecture

- ChipScoPy is the official Python seam for AMD/Xilinx hardware debug data.
- xevdb is the deterministic debug memory/API.
- xevdb is the searchable observability layer, using its normal local file or OpenSearch backend.
- Claude Code or Codex CLI is the optional reasoning interface; it receives compact xevdb evidence instead of raw traces.

## Quick start

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 1. Generate synthetic ChipScoPy-style capture
python scripts/generate_synthetic_capture.py --out examples/failing_axi_capture.json

# 2. Build a real xevdb dataset from XTrace and write JSON debug state
python scripts/ingest_capture.py \
  --capture examples/failing_axi_capture.json \
  --state examples/debug_state.json \
  --xtrace-out examples/failing_axi_capture.xtrace \
  --xevdb-db examples/failing_axi_capture.xevdb \
  --session DBG-AXI-001 \
  --project "AXI DMA Bring-up" \
  --board VCK190

# 3. Ask the debug agent
python scripts/ask_agent.py \
  --state examples/debug_state.json \
  --session DBG-AXI-001 \
  --question "Why did AXI write hang?"

# 4. Run API + dashboard
uvicorn api:app --reload --port 8080
# open dashboard/index.html in browser, or serve it:
python -m http.server 8081 -d dashboard
```

## Synthetic scenarios

`generate_synthetic_capture.py` ships a small library of ChipScoPy-shaped
captures exercising each failure mode the protocol agent detects:

| Scenario | What it shows |
| --- | --- |
| `axi_write_hang` | AW stall (default) — write address never accepted, + a short AXIS dip |
| `axi_read_hang` | AR stall — read address never accepted |
| `axi_bresp_backpressure` | B-channel stall — master not accepting write responses |
| `axis_overrun` | AXIS downstream backpressure with `fifo_full` evidence |
| `write_read_deadlock` | AW **and** AR stalled together — arbiter/mutual deadlock |
| `clean_pass` | healthy capture, no events (negative control) |
| `multi_window` | 2-window capture (`__WINDOW_INDEX`/`__GAP`) with a stall in window 1 |

```bash
python scripts/generate_synthetic_capture.py --list
python scripts/generate_synthetic_capture.py --scenario axi_read_hang --out cap.json
python scripts/generate_synthetic_capture.py --all examples/scenarios   # one file each
```

Then feed any of them to `scripts/ingest_capture.py` exactly as in the Quick
start. (The captures also exposed — and this commit fixes — a signal-matching
bug where `arvalid`/`awvalid` were mis-paired to the R/W channels.)

## xevdb OpenSearch Backend

Use xevdb's backend directly. This project does not import `opensearch-py` or maintain its own indices.

```bash
export XEVDB_OPENSEARCH_HOSTS=localhost:9200
python scripts/ingest_capture.py \
  --capture examples/failing_axi_capture.json \
  --state examples/debug_state.json \
  --xevdb-db examples/failing_axi_capture-os.xevdb \
  --xevdb-backend opensearch \
  --session DBG-AXI-001
```

For a local OpenSearch demo you can run your own OpenSearch container. The app still works without OpenSearch.

## Real ChipScoPy hook

`connectors/chipscopy_connector.py` does real board capture following the
[official chipscopy examples](https://github.com/Xilinx/chipscopy/tree/master/chipscopy/examples/ila_and_vio):
`create_session` → `devices.filter_by(family=).get()` → `program(pdi)` →
`discover_and_setup_cores(ltx_file=)` → `ila_cores.get(name=)` →
`reset_probes`/`set_probe_trigger_value` → `run_basic_trigger(...)` →
`wait_till_done` → `upload` → `waveform.get_data([probes], include_trigger=True,
include_sample_info=True)`. `normalize_chipscopy_data()` adapts ChipScoPy's
`trigger`/`sample_index`/… columns to this app's `__`-prefixed shape. ChipScoPy
is a lazy, optional board-side import.

## Model connectors (Claude / Codex)

The reasoning plane can use **Claude Code** (`claude -p`) or **Codex**
(`codex exec`); either receives only compact xevdb evidence, never raw traces,
and both fall back to deterministic local text when the CLI is unavailable.

Pick one with `--model` (or `$XEVDB_AI_MODEL`):

```bash
# Claude Code CLI
python scripts/ask_agent.py \
  --state examples/debug_state.json --session DBG-AXI-001 \
  --question "Why did AXI write hang?" --model claude

# Codex CLI
python scripts/ask_agent.py ... --model codex

# deterministic, no model
python scripts/ask_agent.py ... --model none
```

Equivalent environment selection (honored by the API and orchestrator too):

| Variable | Effect |
| --- | --- |
| `XEVDB_AI_MODEL=claude\|codex\|none` | choose the backend (takes precedence) |
| `XEVDB_AI_USE_CLAUDE=1` | legacy flag — enable the Claude connector |
| `XEVDB_AI_USE_CODEX=1` | legacy flag — enable the Codex connector |
| `CLAUDE_BIN` / `CODEX_BIN` | path to the CLI (default: PATH lookup) |
| `CLAUDE_MODEL` | `--model` passed to `claude` (optional) |
| `CLAUDE_ARGS` | extra `claude` args, e.g. `--permission-mode plan` |
