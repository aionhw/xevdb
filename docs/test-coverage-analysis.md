# Test Coverage Analysis

_Generated 2026-07-01. Measured with `coverage run -m pytest` against `src/` and
`xevdb_ai_debug/`._

## How the numbers were produced

Two runs, because coverage depends heavily on which optional dependencies and
external binaries are present:

| Scenario | Passed | Skipped | Total coverage |
|---|---|---|---|
| Minimal env (no `opensearch-py`) | 107 | 20 | **63%** |
| CI-parity (`.[opensearch,dev]` installed) | 156 | 14 | **70%** |

CI installs `opensearch-py` and runs the OpenSearch backend against an in-memory
fake, so **70% is the real number to reason about**. Even in CI, 14 tests stay
skipped because they need external binaries that CI never builds (`sv-parse`,
`vcd2fst`/`fst2vcd`).

## Per-module coverage (CI-parity)

| Module | Stmts | Miss | Cover | Notes |
|---|---:|---:|---:|---|
| `xevdb_ai_debug/**` | ~1369 | ~1369 | **0%** | Entire package — never imported by any test |
| `cli.py` | 716 | 434 | **39%** | Largest module; command bodies untested |
| `show.py` | 82 | 63 | **23%** | Source-slice rendering; blocked on `sv-parse` |
| `sv.py` | 296 | 220 | **26%** | SystemVerilog AST parsing; blocked on `sv-parse` |
| `cache.py` | 61 | 29 | **52%** | Result cache put/get/stats/clear |
| `db.py` | 191 | 51 | 73% | Query + error paths |
| `opensearch_docs.py` | 76 | 16 | 79% | |
| `sqlite_backend.py` | 65 | 13 | 80% | |
| `mcp_server.py` | 188 | 38 | 80% | |
| `sim.py` / `prompts.py` | — | — | 84–85% | |
| bugs / decode / diff / kernel / riscv / xtrace / xztrace / parser | — | — | 90–100% | Well covered |

The pure library core (parser, decode, diff, bugs, kernel, riscv, xtrace,
xztrace) is in good shape. The gaps are concentrated in the **user-facing
entry points** and one **entirely untested package**.

## Recommended areas to improve, in priority order

### 1. `cli.py` — the primary user surface, 434 uncovered lines (highest ROI)
The CLI is the largest module and how nearly every user drives the tool, yet
only argument parsing and the backend-gating guard are exercised. All 17 command
_bodies_ — `build`, `build-xtrace`, `build-fst`, `at`, `window`, `find`,
`stats`, `diff`, `riscv-decode`, `decode`, `ingest-*`, `modules`, `show` — are
missed (the missing-line ranges map one-to-one onto the command functions).

Most commands are thin wrappers over already-tested `db.py`/`show.py` functions,
so they're cheap to cover with `click.testing.CliRunner`. Build a small
fixture `.xevdb` once and assert on:
- **Human vs `--json` output** for `at`, `window`, `find`, `stats`, `diff` —
  the JSON-formatting branches are pure glue and are exactly what silently
  breaks on refactors.
- **Error/exit-code paths**: missing signal, empty result, bad `--from/--to`
  range, nonexistent db path. These currently have zero coverage and are the
  most user-visible failure modes.
- **`build` variants**: `--reset`, `--no-seed`, default-db-path derivation
  (`_default_db_path`).

### 2. `xevdb_ai_debug/` — 0% coverage on ~1369 lines
This package (FastAPI `api.py`, orchestrator, connectors, storage, models,
`xtrace_writer`) has **no tests at all** and isn't even imported by the suite.
It's also not part of the `src/` package-find, so it ships/runs as a separate
tree with no safety net. Recommendations:
- Add unit tests for the pure pieces first: `models.py`, `storage.py`
  (round-trip a `DebugSession` through `XevdbStore`), `xtrace_writer.py`, and
  `agents/protocol_agent.py::detect_axi_events` (feed synthetic events, assert
  detections).
- Test the connector abstraction (`base_connector`, `xevdb_connector`) with a
  stub/subprocess mock rather than real AI backends.
- Use FastAPI's `TestClient` for `api.py` endpoints with the orchestrator
  mocked. Decide explicitly whether this package is in scope for CI — right now
  it's invisible to it.

### 3. Make `sv.py` / `show.py` testable without the `sv-parse` binary
`sv.py` (26%) and `show.py` (23%) are low **only because the tests that would
cover them are skipped** when the `sv-parse` binary isn't built — and CI never
builds it, so these skips are permanent, not just local. This is the biggest
"pretend coverage" risk: a whole feature area that never runs in CI.
- Many `sv.py` helpers are **pure and need no binary**: `_line_starts`,
  `_offset_to_line`, `_render_expr`, `_render_dims`, `_extract_ports`,
  `_extract_params`, `_leading_comment`, `_find_module_offset`. Test these
  directly against hand-built AST dicts / source strings.
- For the subprocess path (`load_ast`, `parse_sv_file`), inject a fake
  `sv-parse` (a fixture script emitting canned JSON) or monkeypatch
  `subprocess.run`, so the parsing/rendering pipeline runs in CI without the
  real Rust binary.
- `show.py::render`, `_slice_lines`, and `by_file_line` can be tested against a
  small in-memory SQLite fixture with a couple of source rows — no ingestion
  needed.

### 4. `cache.py` — 52%, pure SQLite logic that's cheap to cover
`put`, `get` (hit/miss/expiry), `stats`, `list_entries`, and `clear` are all
missed. This is straightforward SQLite behavior with no external deps; add a
fixture connection and assert cache round-trips, key derivation
(`make_key`), and the `enabled()` env toggle.

### 5. Fill remaining `db.py` (73%) branch gaps
Target the missed query/error branches: `resolve_signal` returning `None`,
`value_at` with no sample, `window` bound combinations, and the FTS
availability fallback (`_ensure_bug_fts` / `bug_fts_available`).

## Process-level recommendations

- **Add coverage measurement to CI.** There's currently no coverage step and no
  threshold. Wire `pytest --cov` (or `coverage run`) into
  `.github/workflows` and publish the report; consider a ratchet (fail if
  total drops) once the CLI gap is closed.
- **Surface permanent skips.** 14 tests skip even in CI because `sv-parse` and
  the FST tools aren't built. Either build them in a CI job or provide the
  fakes described above — otherwise `sv.py`/`show.py`/`fst.py` coverage will
  never reflect reality.
- **Decide `xevdb_ai_debug`'s status.** If it's shipped, it needs tests and a
  CI entry; if it's experimental, document that so its 0% isn't mistaken for an
  oversight.
