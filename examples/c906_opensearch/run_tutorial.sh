#!/usr/bin/env bash
# =============================================================================
# c906 "hello" on xezim  ->  scoped VCD  ->  OpenSearch debug DB (xevdb)
#
# Reproduces TUTORIAL.md end to end. Idempotent: re-running skips work that is
# already done (downloaded OpenSearch, an existing VCD, etc.) unless --force.
#
# Usage:
#   ./run_tutorial.sh [all]      # default: every step
#   ./run_tutorial.sh opensearch # just bring up the cluster
#   ./run_tutorial.sh sim        # just run xezim -> full VCD
#   ./run_tutorial.sh filter     # just scope-filter the VCD
#   ./run_tutorial.sh ingest     # just load VCD+RTL+sim into OpenSearch
#   ./run_tutorial.sh query      # just run the demo queries
#   ./run_tutorial.sh --force ...# redo even if outputs already exist
# =============================================================================
set -euo pipefail

# ---- configuration (override via env) ---------------------------------------
XEZIM="${XEZIM:-$HOME/repo/sv2023/xezim/target/release/xezim}"
COMPILE_DIR="${COMPILE_DIR:-$HOME/repo/rtlmeter/work/XuanTie-C906/default/compile-0}"
HELLO_DIR="${HELLO_DIR:-$HOME/repo/rtlmeter/designs/XuanTie-C906/tests/hello}"
C906_SRC="${C906_SRC:-$HOME/repo/rtlmeter/designs/XuanTie-C906/src}"
# xevdb is the repo this example lives in — two levels up from this script
# (xevdb/examples/c906_opensearch/run_tutorial.sh). Resolve it relatively so the
# script works wherever the repo is checked out.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
XEVDB_ROOT="${XEVDB_ROOT:-$(cd "$SCRIPT_DIR/../.." && pwd)}"
XEVDB="${XEVDB:-$XEVDB_ROOT/.venv/bin/xevdb}"

OS_HOME="${OS_HOME:-$HOME/os-run/os}"
OS_HOST="${OS_HOST:-localhost:9200}"
OS_VERSION="${OS_VERSION:-2.17.1}"

WORK="${WORK:-$HOME/os-run/tutorial}"          # outputs land here
DUMP_ID="${DUMP_ID:-c906_hello}"
SCOPES=(x_aq_ifu_top x_aq_rtu_top)             # VCD sub-scopes to keep

FORCE=0
CPU_HIER="x_soc.x_cpu_sub_system_axi.x_c906_wrapper.x_cpu_top.x_aq_top_0.x_aq_core"

# ---- helpers ----------------------------------------------------------------
log()  { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
info() { printf '    %s\n' "$*"; }
die()  { printf '\033[31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }
os_up(){ curl -fsS -m 5 "http://$OS_HOST/_cluster/health" >/dev/null 2>&1; }

require() { [ -e "$1" ] || die "missing $2: $1"; }

# =============================================================================
step_opensearch() {
  log "1. OpenSearch single node ($OS_HOST)"
  if os_up; then info "already up: $(curl -fsS http://$OS_HOST/_cluster/health | grep -o '"status":"[a-z]*"')"; return; fi

  if [ ! -x "$OS_HOME/bin/opensearch" ]; then
    info "downloading OpenSearch $OS_VERSION ..."
    mkdir -p "$(dirname "$OS_HOME")"; cd "$(dirname "$OS_HOME")"
    curl -fL -o opensearch.tgz \
      "https://artifacts.opensearch.org/releases/bundle/opensearch/$OS_VERSION/opensearch-$OS_VERSION-linux-x64.tar.gz"
    tar -xzf opensearch.tgz && mv "opensearch-$OS_VERSION" "$OS_HOME"
  fi
  # dev mode: single node, security off -> plain HTTP, no auth (DEV ONLY)
  if ! grep -q "discovery.type: single-node" "$OS_HOME/config/opensearch.yml"; then
    cat >> "$OS_HOME/config/opensearch.yml" <<'YML'

# --- xevdb tutorial: single-node dev, security disabled ---
discovery.type: single-node
plugins.security.disabled: true
YML
  fi
  info "launching (logs: $WORK/opensearch.log) ..."
  mkdir -p "$WORK"
  OPENSEARCH_JAVA_OPTS="-Xms1g -Xmx1g" nohup "$OS_HOME/bin/opensearch" >"$WORK/opensearch.log" 2>&1 &
  for _ in $(seq 1 60); do os_up && break; sleep 2; done
  os_up || die "OpenSearch did not come up; see $WORK/opensearch.log"
  info "up: $(curl -fsS http://$OS_HOST/_cluster/health | grep -o '"status":"[a-z]*"')"
}

# =============================================================================
step_sim() {
  log "2. Run c906 hello on xezim -> full VCD"
  require "$XEZIM" "xezim binary"; require "$COMPILE_DIR/filelist" "rtlmeter filelist"
  local vcd="$COMPILE_DIR/c906_hello.vcd"
  if [ -s "$vcd" ] && [ "$FORCE" = 0 ]; then info "VCD exists ($(du -h "$vcd"|cut -f1)); use --force to rerun"; return; fi

  cd "$COMPILE_DIR"
  # dump-enabled tb: inject $dumpfile/$dumpvars after `module tb();`.
  # NOTE: xezim collapses a deep instance scope to its leaf (which never matches
  # the x_soc.-rooted signal names), so we dump the whole chip (x_soc) and
  # scope-filter the VCD in step 3.  (See TUTORIAL.md "scope gotcha".)
  python3 - <<'PY'
src = open("verilogSourceFiles/tb.v").read()
dump = ('module tb();\n'
        'initial begin\n'
        '  $dumpfile("c906_hello.vcd");\n'
        '  $dumpvars(0, x_soc);\n'
        'end\n')
open("tb_dump.v", "w").write(src.replace("module tb();\n", dump, 1))
PY
  sed 's#^verilogSourceFiles/tb.v$#tb_dump.v#' filelist > filelist.xezim
  ln -sf "$HELLO_DIR/inst.pat" inst.pat
  ln -sf "$HELLO_DIR/data.pat" data.pat

  rm -f "$vcd"
  # 2>&1: xezim's $display lines are stdout but its warnings / "[VCD] dumping" /
  # "[PROF]" / "Simulation finished" are stderr — capture both in the sim log.
  "$XEZIM" -s tb +incdir+verilogIncludeFiles +define+__RTLMETER_MAIN_CLOCK=tb.clk \
    -f filelist.xezim --max-time 50000000 2>&1 | tee "$WORK/c906_hello.simlog" \
    | grep -E "TEST PASSED|TEST FAILED|\[VCD\] dumping|finished at time" || true
  grep -q "TEST PASSED" "$WORK/c906_hello.simlog" || die "sim did not pass"
  [ -s "$vcd" ] || die "no VCD produced"
  info "VCD: $(du -h "$vcd" | cut -f1)"
}

# =============================================================================
ensure_filter_script() {
  cat > "$WORK/vcd_scope_filter.py" <<'PY'
#!/usr/bin/env python3
"""Filter a VCD to the signals under one or more instance scopes."""
import sys
def main():
    inp, outp, *insts = sys.argv[1:]; insts = set(insts)
    keep, scope, out, defs, pend = set(), [], [], True, []
    for line in open(inp, errors="replace"):
        s = line.strip()
        if defs:
            if s.startswith("$scope"):
                p = s.split(); scope.append(p[2] if len(p) >= 3 else "?"); pend.append(line)
            elif s.startswith("$upscope"):
                if scope: scope.pop()
                if pend: pend.pop()
                else: out.append(line)
            elif s.startswith("$var"):
                if any(c in insts for c in scope):
                    out.extend(pend); pend = []; out.append(line); keep.add(s.split()[3])
            elif s.startswith("$enddefinitions"): out.append(line); defs = False
            else: out.append(line)
        else:
            if s.startswith("#") or s.startswith("$") or not s: out.append(line)
            else:
                c = s[0]
                if c in "01xXzZ" and s[1:] in keep: out.append(line)
                elif c in "bBrR":
                    p = s.split()
                    if len(p) == 2 and p[1] in keep: out.append(line)
    open(outp, "w").writelines(out)
    print(f"kept {len(keep)} signals under {sorted(insts)} -> {outp}")
main()
PY
}

step_filter() {
  log "3. Scope-filter VCD -> ${SCOPES[*]}"
  local vcd="$COMPILE_DIR/c906_hello.vcd" out="$WORK/${DUMP_ID}_ifu_rtu.vcd"
  require "$vcd" "full VCD (run 'sim' first)"
  ensure_filter_script
  python3 "$WORK/vcd_scope_filter.py" "$vcd" "$out" "${SCOPES[@]}"
  info "filtered VCD: $(du -h "$out" | cut -f1)"
}

# =============================================================================
step_ingest() {
  log "4. Load VCD + RTL + sim into OpenSearch (dump_id=$DUMP_ID)"
  require "$XEVDB" "xevdb"; os_up || die "OpenSearch not up (run 'opensearch' step)"
  local vcd="$WORK/${DUMP_ID}_ifu_rtu.vcd" ptr="$WORK/$DUMP_ID.xevdb"
  require "$vcd" "filtered VCD (run 'filter' first)"

  # pointer file: where the dataset lives + a generous client timeout (a fresh
  # single node spends ~3s per index create).
  cat > "$ptr" <<JSON
{ "backend": "opensearch", "hosts": ["$OS_HOST"],
  "dump_id": "$DUMP_ID", "prefix": "xevdb",
  "extra": {"timeout": 180, "max_retries": 3, "retry_on_timeout": true} }
JSON

  # focused RTL: the IFU + RTU sources whose signals are in the VCD
  mkdir -p "$WORK/rtl"; cp "$C906_SRC"/aq_ifu_*.v "$C906_SRC"/aq_rtu_*.v "$WORK/rtl/" 2>/dev/null || true

  curl -fsS -X DELETE "http://$OS_HOST/xevdb-$DUMP_ID-*" >/dev/null 2>&1 || true
  info "(a) waveform ..."; "$XEVDB" build "$vcd" --db "$ptr"
  info "(b) RTL ...";      "$XEVDB" ingest-rtl "$ptr" "$WORK/rtl/"
  info "(c) sim log ...";  "$XEVDB" ingest-sim "$ptr" "$WORK/c906_hello.simlog"
  curl -fsS "http://$OS_HOST/_cat/indices/xevdb-$DUMP_ID-*?h=index,docs.count,store.size&s=index" \
    | grep -E "changes|signals|modules" || true
}

# =============================================================================
step_query() {
  log "5. Debug from OpenSearch"
  local DB="$WORK/$DUMP_ID.xevdb"; require "$DB" "pointer (run 'ingest' first)"
  local RTU="tb.$CPU_HIER.x_aq_rtu_top"

  info "stats:";            "$XEVDB" stats "$DB" | grep -E "n_signals|n_changes|t_max" || true
  info "find pc_:";         "$XEVDB" find "$DB" "pc_" --limit 4 || true
  info "busiest signals:";  "$XEVDB" prompt run "$DB" change_count --arg limit=4 || true
  info "X/Z signals:";      "$XEVDB" prompt run "$DB" xz_signals --arg limit=4 || true
  info "retire scope:";     "$XEVDB" prompt run "$DB" signals_in_scope --arg prefix="$RTU" --arg limit=4 || true
  info "record + search a bug:"
  "$XEVDB" bug add "$DB" "ifu-icache-uninit-x" --severity warning \
    --symptom "IFU icache data-array RAMs hold X the whole hello run" \
    --module aq_ifu_icache_data_array --keyword icache --keyword xprop --overwrite >/dev/null || true
  "$XEVDB" bug search "$DB" "uninitialized icache" || true
  info "raw OpenSearch cardinality agg (distinct X/Z signals):"
  curl -fsS "http://$OS_HOST/xevdb-$DUMP_ID-changes/_search" -H 'Content-Type: application/json' \
    -d '{"size":0,"query":{"term":{"xz":true}},"aggs":{"xz":{"cardinality":{"field":"fullname"}}}}' \
    | python3 -c "import sys,json;print('   distinct X/Z signals:',json.load(sys.stdin)['aggregations']['xz']['value'])" || true
}

# =============================================================================
main() {
  local steps=()
  for a in "$@"; do case "$a" in
    --force) FORCE=1 ;;
    all|opensearch|sim|filter|ingest|query) steps+=("$a") ;;
    -h|--help) sed -n '2,22p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) die "unknown arg: $a" ;;
  esac; done
  [ "${#steps[@]}" -eq 0 ] && steps=(all)

  mkdir -p "$WORK"
  for s in "${steps[@]}"; do case "$s" in
    all)        step_opensearch; step_sim; step_filter; step_ingest; step_query ;;
    opensearch) step_opensearch ;;
    sim)        step_sim ;;
    filter)     step_filter ;;
    ingest)     step_ingest ;;
    query)      step_query ;;
  esac; done
  log "done. Tutorial dataset: dump_id=$DUMP_ID on $OS_HOST"
}
main "$@"
