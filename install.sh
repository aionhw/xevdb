#!/usr/bin/env bash
# xevdb — standalone installer
#
# Sets up a Python venv, pip-installs xevdb editable, clones + builds the
# SystemVerilog parser (sv-parse) from the aionhw/xezim-core repo, and
# smoke-tests the full pipeline against examples/{counter.vcd, counter.sv}.
# Zero AI dependencies.
#
# Idempotent. The xezim-core checkout is refreshed (git pull) on re-run.
#
# Usage:
#   bash install.sh                  # default
#   bash install.sh --no-smoke
#   bash install.sh --no-rust        # skip clone+build (RTL features unavailable)
#   bash install.sh --recreate-venv
#   bash install.sh --with-duckdb
#   bash install.sh --help
#
# Environment:
#   XEZIM_CORE_REPO  git URL of xezim-core   (default: the aionhw HTTPS repo)
#   XEZIM_CORE_REF   branch/tag/sha to check out (default: main)

set -euo pipefail

SMOKE=1
BUILD_RUST=1
RECREATE=0
EXTRAS=""

for arg in "$@"; do
    case "$arg" in
        --no-smoke)       SMOKE=0 ;;
        --no-rust)        BUILD_RUST=0 ;;
        --recreate-venv)  RECREATE=1 ;;
        --with-duckdb)    EXTRAS="[duckdb]" ;;
        -h|--help)
            sed -n '2,21p' "$0" | sed 's/^# \{0,1\}//'
            exit 0 ;;
        *)
            echo "unknown flag: $arg" >&2
            exit 2 ;;
    esac
done

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO"

if [[ -t 1 ]]; then
    BOLD=$'\033[1m'; RED=$'\033[31m'; YEL=$'\033[33m'; GRN=$'\033[32m'; DIM=$'\033[2m'; RST=$'\033[0m'
else
    BOLD=""; RED=""; YEL=""; GRN=""; DIM=""; RST=""
fi
hr()   { printf '\n%s== %s ==%s\n' "$BOLD" "$*" "$RST"; }
ok()   { printf '  %s✓%s  %s\n'    "$GRN" "$RST" "$*"; }
warn() { printf '  %s!%s  %s\n'    "$YEL" "$RST" "$*"; }
err()  { printf '  %s✗%s  %s\n'    "$RED" "$RST" "$*"; }

# ----- 1. prereqs ----------------------------------------------------------
hr "1/5  prerequisites"

# xevdb's sv.py drives `sv-parse --dump-json`, provided by the
# `json-cli` cargo feature (on by default) of xezim-core's main branch.
XEZIM_CORE_REPO="${XEZIM_CORE_REPO:-https://github.com/aionhw/xezim-core.git}"
XEZIM_CORE_REF="${XEZIM_CORE_REF:-main}"

PY=""
for cand in python3.13 python3.12 python3.11 python3.10 python3 python; do
    if command -v "$cand" >/dev/null 2>&1; then
        ver=$("$cand" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || echo "")
        major=${ver%%.*}; minor=${ver##*.}
        if [[ -n "$ver" && "$major" -ge 3 && ( "$major" -gt 3 || "$minor" -ge 10 ) ]]; then
            PY="$cand"; break
        fi
    fi
done
[[ -z "$PY" ]] && { err "Python >= 3.10 not found"; exit 1; }
ok "Python: $($PY --version 2>&1)"

if command -v cargo >/dev/null 2>&1; then
    ok "Rust: $(rustc --version 2>&1)"
else
    warn "cargo not found — the SystemVerilog parser will not be built"
    warn "RTL features (ingest-rtl, show, modules) will be unavailable"
    BUILD_RUST=0
fi

if [[ "$BUILD_RUST" -eq 1 ]]; then
    if command -v git >/dev/null 2>&1; then
        ok "git: $(git --version 2>&1 | head -1)"
    else
        warn "git not found — cannot clone xezim-core; RTL features disabled"
        BUILD_RUST=0
    fi
fi

# ----- 2. Python venv + install -------------------------------------------
hr "2/5  Python venv + pip install"

VENV="$REPO/.venv"
if [[ -d "$VENV" && "$RECREATE" -eq 1 ]]; then rm -rf "$VENV"; fi
[[ -d "$VENV" ]] || "$PY" -m venv "$VENV"
# shellcheck source=/dev/null
source "$VENV/bin/activate"
python -m pip install --upgrade pip wheel >/dev/null

LOG="$REPO/.install-pip.log"
if pip install -e ".${EXTRAS}" >"$LOG" 2>&1; then
    ok "xevdb installed${EXTRAS:+ (with optional $EXTRAS)}"
else
    err "pip install failed — last 20 lines of $LOG:"
    tail -20 "$LOG" >&2
    exit 1
fi

command -v xevdb >/dev/null || { err "xevdb console script missing"; exit 1; }

# ----- 3. clone / update xezim-core ---------------------------------------
hr "3/5  xezim-core (SystemVerilog parser source)"

XC_DIR="$REPO/xezim-core"
if [[ "$BUILD_RUST" -eq 1 ]]; then
    if [[ -d "$XC_DIR/.git" ]]; then
        if (cd "$XC_DIR" && git fetch --quiet origin \
            && git checkout --quiet "$XEZIM_CORE_REF" \
            && git pull --quiet --ff-only origin "$XEZIM_CORE_REF" 2>/dev/null); then
            ok "xezim-core updated → $(cd "$XC_DIR" && git rev-parse --short HEAD)"
        else
            # Detached tag/sha, or pull not fast-forwardable — checkout is
            # still valid, just not advanceable. Not fatal.
            warn "xezim-core: could not fast-forward $XEZIM_CORE_REF (using current checkout)"
        fi
    else
        # Stale non-git directory (e.g. an old vendored copy) — replace it.
        [[ -e "$XC_DIR" ]] && rm -rf "$XC_DIR"
        if git clone --quiet --branch "$XEZIM_CORE_REF" \
               "$XEZIM_CORE_REPO" "$XC_DIR" 2>/dev/null \
           || git clone --quiet "$XEZIM_CORE_REPO" "$XC_DIR"; then
            # Second form (no --branch) covers refs that are a sha, not a
            # branch/tag; check it out explicitly afterwards.
            (cd "$XC_DIR" && git checkout --quiet "$XEZIM_CORE_REF" 2>/dev/null) || true
            ok "xezim-core cloned → $(cd "$XC_DIR" && git rev-parse --short HEAD)"
        else
            err "failed to clone xezim-core from $XEZIM_CORE_REPO"
            exit 1
        fi
    fi
else
    printf '  %s·%s  skipped (Rust toolchain unavailable)\n' "$DIM" "$RST"
fi

# ----- 4. cargo build sv-parse --------------------------------------------
hr "4/5  build SystemVerilog parser (sv-parse)"

SV_PARSE="$REPO/xezim-core/xezim-parser/target/release/sv-parse"
if [[ "$BUILD_RUST" -eq 1 ]]; then
    CARGO_LOG="$REPO/.install-cargo.log"
    if (cd "$REPO/xezim-core/xezim-parser" && cargo build --release) >"$CARGO_LOG" 2>&1; then
        if [[ -x "$SV_PARSE" ]]; then
            ok "sv-parse built ($(du -h "$SV_PARSE" | cut -f1))"
        else
            err "cargo reported success but binary not at $SV_PARSE"
            tail -20 "$CARGO_LOG" >&2
            exit 1
        fi
    else
        err "cargo build failed — last 20 lines of $CARGO_LOG:"
        tail -20 "$CARGO_LOG" >&2
        exit 1
    fi
elif [[ -x "$SV_PARSE" ]]; then
    ok "sv-parse already built (skipping rebuild)"
else
    warn "sv-parse not built (RTL features disabled)"
fi

# ----- 5. smoke test -------------------------------------------------------
hr "5/5  smoke test"

if [[ "$SMOKE" -eq 0 ]]; then
    printf '  %s·%s  skipped (--no-smoke)\n' "$DIM" "$RST"
else
    DB=$(mktemp --suffix=.xevdb)
    trap 'rm -f "$DB"' EXIT
    FAIL=0

    xevdb build "$REPO/examples/counter.vcd" --db "$DB" --reset >/dev/null \
        || { err "xevdb build failed"; FAIL=1; }

    VAL=$(xevdb at "$DB" top.u_cnt.count --time 25 --json 2>/dev/null \
          | python -c "import sys,json; print(json.load(sys.stdin)['value'])" || echo "")
    if [[ "$VAL" == "00000010" ]]; then
        ok "VCD: value at t=25 → 0x02 (expected)"
    else
        err "expected 00000010, got '$VAL'"; FAIL=1
    fi

    N=$(xevdb prompt list "$DB" 2>/dev/null | wc -l)
    if [[ "$N" -ge 14 ]]; then
        ok "$N seed prompts present (8 VCD + 6 RTL)"
    else
        err "expected >= 14 seed prompts, got $N"; FAIL=1
    fi

    if [[ -x "$SV_PARSE" ]]; then
        if xevdb ingest-rtl "$DB" "$REPO/examples/counter.sv" --reset >/dev/null 2>&1; then
            ok "RTL ingest: counter.sv"
        else
            err "RTL ingest failed"; FAIL=1
        fi

        N_MODS=$(xevdb modules "$DB" 2>/dev/null | wc -l)
        if [[ "$N_MODS" -ge 2 ]]; then
            ok "RTL: $N_MODS modules parsed"
        else
            err "expected >= 2 modules, got $N_MODS"; FAIL=1
        fi

        if xevdb show "$DB" counter >/dev/null 2>&1; then
            ok "show counter: rendered module slice"
        else
            err "show counter failed"; FAIL=1
        fi
    fi

    [[ "$FAIL" -gt 0 ]] && { err "smoke checks failed"; exit 1; }
fi

hr "ready"
cat <<EOF
${GRN}xevdb installed.${RST}

  source .venv/bin/activate

CLIs:
  ${BOLD}xevdb build${RST}        <vcd>                          # parse VCD → .xevdb
  ${BOLD}xevdb at${RST}           <db> <signal> --time <t>
  ${BOLD}xevdb window${RST}       <db> <signal> --from <t0> --to <t1>
  ${BOLD}xevdb find${RST}         <db> <pattern>
  ${BOLD}xevdb stats${RST}        <db>
  ${BOLD}xevdb ingest-rtl${RST}   <db> <path>                    # parse .sv/.v files
  ${BOLD}xevdb modules${RST}      <db> [--filter NAME]
  ${BOLD}xevdb show${RST}         <db> <module|signal|file:line> # display source
  ${BOLD}xevdb prompt${RST}       {list, show, run, add, remove}
  ${BOLD}xevdb cache${RST}        {stats, list, clear}

next:
  xevdb build       examples/counter.vcd
  xevdb ingest-rtl  examples/counter.vcd.xevdb examples/
  xevdb show        examples/counter.vcd.xevdb counter
EOF
