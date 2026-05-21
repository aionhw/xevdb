#!/usr/bin/env bash
# xevdb picorv32 demo
#
# Builds a self-contained .xevdb from picorv32 iv.vcd + RTL, then exercises
# the bundled prompt library and the 'show' command. Demonstrates the
# waveform → RTL bridge: find an interesting signal in the waveform, then
# show the SystemVerilog source that declares it, straight from the DB.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DEMO="$REPO/demos/picorv32"
PICORV32_REPO="${PICORV32_REPO:-$(cd "$REPO/.." && pwd)/picorv32}"

if [[ ! -f "$PICORV32_REPO/picorv32.v" ]]; then
    echo "error: picorv32 sources not found at $PICORV32_REPO" >&2
    exit 1
fi
VCD="${VCD_PATH:-$PICORV32_REPO/iv.vcd}"
[[ -f "$VCD" ]] || { echo "error: VCD not found at $VCD" >&2; exit 1; }

DB="$DEMO/picorv32.xevdb"

if [[ -f "$REPO/.venv/bin/activate" ]]; then
    # shellcheck source=/dev/null
    source "$REPO/.venv/bin/activate"
elif ! command -v xevdb >/dev/null 2>&1; then
    echo "error: xevdb not on PATH; run: bash $REPO/install.sh" >&2
    exit 1
fi

hr() { printf '\n\033[1m== %s ==\033[0m\n' "$*"; }

# ----- 1. build waveform side ----------------------------------------------
hr "1/8  build $(basename "$DB") from $(basename "$VCD")"
time xevdb build "$VCD" --db "$DB" --reset

# ----- 2. ingest RTL into the same file ------------------------------------
hr "2/8  ingest RTL (picorv32.v + testbench.v) into the SAME .xevdb"
# Stage the two relevant RTL files alongside the .xevdb (NOT into mktemp) so
# the `file` paths recorded in the database remain valid afterwards. The
# picorv32 repo contains many synth artifacts (.v files); staging keeps the
# ingest focused on the two we actually care about.
STAGE="$DEMO/rtl_src"
mkdir -p "$STAGE"
cp -f "$PICORV32_REPO/picorv32.v" "$PICORV32_REPO/testbench.v" "$STAGE/"
time xevdb ingest-rtl "$DB" "$STAGE" --reset

# ----- 3. unified stats ----------------------------------------------------
hr "3/8  stats — waveform + RTL counts in one file"
xevdb stats "$DB"

# ----- 4. wave → RTL bridge: declaration of reg_pc -------------------------
hr "4/8  signal_declaration — where is reg_pc declared?"
echo "\$ xevdb prompt run $(basename "$DB") signal_declaration --arg name=reg_pc"
xevdb prompt run "$DB" signal_declaration --arg name=reg_pc

# ----- 5. show the picorv32 module header ---------------------------------
hr "5/8  show — render the picorv32 module header from the database"
echo "\$ xevdb show $(basename "$DB") picorv32  --context 8"
xevdb show "$DB" picorv32 --context 8

# ----- 6. cross-cutting prompt: xz signals with RTL line -------------------
hr "6/8  xz_signals_with_rtl — VCD X/Z hits joined with their RTL declaration"
echo "\$ xevdb prompt run $(basename "$DB") xz_signals_with_rtl --arg limit=5"
xevdb prompt run "$DB" xz_signals_with_rtl --arg limit=5

# ----- 7. ingest a sim log -------------------------------------------------
hr "7/8  ingest-sim — add a synthetic UVM-style log to the same .xevdb"
SIM_LOG="$REPO/examples/sim.log"
echo "\$ xevdb ingest-sim $(basename "$DB") examples/sim.log --reset"
xevdb ingest-sim "$DB" "$SIM_LOG" --reset

echo
echo "\$ xevdb prompt run $(basename "$DB") sim_summary"
xevdb prompt run "$DB" sim_summary

# ----- 8. sim_with_rtl — the three-way bridge ------------------------------
hr "8/8  sim_with_rtl — sim events whose ref_file:ref_line lands in an RTL module"
echo "\$ xevdb prompt run $(basename "$DB") sim_with_rtl --arg limit=10"
xevdb prompt run "$DB" sim_with_rtl --arg limit=10

# ----- summary -------------------------------------------------------------
hr "done"
SIZE="$(du -h "$DB" | cut -f1)"
cat <<EOF
$DB ($SIZE) holds:
  - 11 K signals + 36 K value changes (picorv32 waveform)
  - 11 modules + every internal signal / port / instance
  - full source text of picorv32.v and testbench.v
  - 14 events from sim.log (1 UVM_FATAL, 3 UVM_ERROR, …) tied back to RTL
  - 20 seed prompts + the result cache from this demo

inspect it with sqlite3:
  sqlite3 $(basename "$DB") 'SELECT name, line_start, line_end FROM modules;'
  sqlite3 $(basename "$DB") 'SELECT severity, COUNT(*) FROM sim_events GROUP BY severity;'
  sqlite3 $(basename "$DB") 'SELECT name, COUNT(*) FROM module_signals GROUP BY module_id;'
EOF
