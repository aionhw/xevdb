"""Canonical query prompts seeded into every .xevdb at build time.

Each prompt is a parameterized SQL template using SQLite's `:name` placeholder
syntax. Both VCD (waveform) and SV (RTL) tables are queryable from the same
prompt, so cross-cutting analyses live here too.

Users can override or extend the library via `xevdb prompt add`.
"""
from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass


@dataclass(frozen=True)
class SeedPrompt:
    name: str
    description: str
    sql: str
    params: list[dict]
    # Optional backend-agnostic query template (e.g. an OpenSearch DSL body as
    # a JSON string). Empty means SQL-only; non-relational backends that lack a
    # `dsl` for a prompt should skip or fall back rather than run `sql`.
    dsl: str = ""


PROMPTS: list[SeedPrompt] = [
    # ---------- waveform (inherited from vcdb) -------------------------------
    SeedPrompt(
        name="signal_transitions",
        description="Busiest signals in a time window — top N by change count.",
        sql=(
            "SELECT s.fullname, s.width, s.kind, COUNT(c.t) AS transitions "
            "FROM signals s JOIN changes c ON c.sig_id = s.id "
            "WHERE c.t BETWEEN :t0 AND :t1 "
            "GROUP BY s.id ORDER BY transitions DESC LIMIT :limit"
        ),
        params=[
            {"name": "t0", "default": 0, "type": "int"},
            {"name": "t1", "default": 9223372036854775807, "type": "int"},
            {"name": "limit", "default": 20, "type": "int"},
        ],
        # changes carry a denormalized `fullname` + `t`; range-filter then a
        # terms agg gives per-signal counts in the window.
        dsl='{"index": "changes", "rows": "aggs:sigs", "body": {"size": 0, '
            '"query": {"range": {"t": {"gte": ":t0", "lte": ":t1"}}}, '
            '"aggs": {"sigs": {"terms": {"field": "fullname", "size": ":limit"}}}}}',
    ),
    SeedPrompt(
        name="change_count",
        description="Total change count per signal across the whole VCD.",
        sql=(
            "SELECT s.fullname, s.width, COUNT(c.t) AS transitions "
            "FROM signals s LEFT JOIN changes c ON c.sig_id = s.id "
            "GROUP BY s.id ORDER BY transitions DESC LIMIT :limit"
        ),
        params=[{"name": "limit", "default": 50, "type": "int"}],
        # changes carry a denormalized `fullname`; a terms agg == per-signal count.
        dsl='{"index": "changes", "rows": "aggs:sigs", "body": {"size": 0, "aggs": '
            '{"sigs": {"terms": {"field": "fullname", "size": ":limit"}}}}}',
    ),
    SeedPrompt(
        name="stuck_at",
        description="Signals that never changed (or changed at most once).",
        sql=(
            "SELECT s.fullname, s.width, s.kind, COUNT(c.t) AS transitions, "
            "       (SELECT value FROM changes WHERE sig_id = s.id ORDER BY t LIMIT 1) AS held_value "
            "FROM signals s LEFT JOIN changes c ON c.sig_id = s.id "
            "GROUP BY s.id HAVING transitions <= 1 "
            "ORDER BY s.fullname LIMIT :limit"
        ),
        params=[{"name": "limit", "default": 200, "type": "int"}],
    ),
    SeedPrompt(
        name="xz_signals",
        description="Signals that ever carried an 'x' or 'z' bit (uninitialized hint).",
        sql=(
            "SELECT DISTINCT s.fullname, s.width, s.kind FROM signals s "
            "JOIN changes c ON c.sig_id = s.id "
            "WHERE c.value LIKE '%x%' OR c.value LIKE '%z%' "
            "   OR c.value LIKE '%X%' OR c.value LIKE '%Z%' "
            "ORDER BY s.fullname LIMIT :limit"
        ),
        params=[{"name": "limit", "default": 200, "type": "int"}],
        # changes carry a precomputed `xz` boolean — filter + distinct fullnames.
        dsl='{"index": "changes", "rows": "aggs:sigs", "body": {"size": 0, '
            '"query": {"term": {"xz": true}}, "aggs": {"sigs": {"terms": '
            '{"field": "fullname", "size": ":limit"}}}}}',
    ),
    SeedPrompt(
        name="signals_in_scope",
        description="List every signal whose VCD hierarchy starts with `:prefix`.",
        sql=(
            "SELECT id, hier, name, fullname, width, kind FROM signals "
            "WHERE hier LIKE :prefix || '%' ORDER BY fullname LIMIT :limit"
        ),
        params=[
            {"name": "prefix", "default": "", "type": "str"},
            {"name": "limit", "default": 500, "type": "int"},
        ],
        dsl='{"index": "signals", "body": {"size": ":limit", "query": {"prefix": '
            '{"hier": ":prefix"}}, "sort": [{"fullname": "asc"}]}}',
    ),
    SeedPrompt(
        name="value_at_many",
        description="Last value of every signal matching :pattern at time :time.",
        sql=(
            "SELECT s.fullname, "
            "       (SELECT c.value FROM changes c WHERE c.sig_id = s.id AND c.t <= :time "
            "        ORDER BY c.t DESC LIMIT 1) AS value, "
            "       (SELECT c.t FROM changes c WHERE c.sig_id = s.id AND c.t <= :time "
            "        ORDER BY c.t DESC LIMIT 1) AS last_t "
            "FROM signals s WHERE s.fullname LIKE :pattern OR s.name LIKE :pattern "
            "ORDER BY s.fullname LIMIT :limit"
        ),
        params=[
            {"name": "time", "default": 0, "type": "int"},
            {"name": "pattern", "default": "%", "type": "str"},
            {"name": "limit", "default": 100, "type": "int"},
        ],
    ),
    SeedPrompt(
        name="signal_history",
        description="Full change history of one signal over a time window.",
        sql=(
            "SELECT c.t, c.value FROM changes c JOIN signals s ON s.id = c.sig_id "
            "WHERE s.fullname = :signal AND c.t BETWEEN :t0 AND :t1 "
            "ORDER BY c.t LIMIT :limit"
        ),
        params=[
            {"name": "signal", "default": "", "type": "str"},
            {"name": "t0", "default": 0, "type": "int"},
            {"name": "t1", "default": 9223372036854775807, "type": "int"},
            {"name": "limit", "default": 1000, "type": "int"},
        ],
        # changes carry a denormalized `fullname`; filter to one signal + window.
        dsl='{"index": "changes", "body": {"size": ":limit", "query": {"bool": '
            '{"filter": [{"term": {"fullname": ":signal"}}, {"range": {"t": '
            '{"gte": ":t0", "lte": ":t1"}}}]}}, "sort": [{"t": "asc"}]}}',
    ),
    SeedPrompt(
        name="clock_period",
        description="Estimate the period of a 1-bit clock from its rising edges.",
        sql=(
            "WITH rises AS (SELECT c.t AS edge FROM changes c JOIN signals s ON s.id = c.sig_id "
            "  WHERE s.fullname = :signal AND c.value = '1' ORDER BY c.t LIMIT :samples), "
            "deltas AS (SELECT r2.edge - r1.edge AS dt FROM rises r1, rises r2 "
            "  WHERE r2.edge > r1.edge AND NOT EXISTS "
            "  (SELECT 1 FROM rises r3 WHERE r3.edge > r1.edge AND r3.edge < r2.edge)) "
            "SELECT MIN(dt) AS min_period, MAX(dt) AS max_period, "
            "       AVG(dt) AS avg_period, COUNT(*) AS edges_observed FROM deltas"
        ),
        params=[
            {"name": "signal", "default": "", "type": "str"},
            {"name": "samples", "default": 32, "type": "int"},
        ],
    ),

    # ---------- RTL (new in xevdb) ------------------------------------------
    SeedPrompt(
        name="list_modules",
        description="List every parsed RTL module with size and body summary.",
        sql=(
            "SELECT name, kind, file, line_start, line_end, body_summary "
            "FROM modules ORDER BY file, line_start LIMIT :limit"
        ),
        params=[{"name": "limit", "default": 100, "type": "int"}],
        dsl='{"index": "modules", "body": {"size": ":limit", "query": {"match_all": '
            '{}}, "sort": [{"file": "asc"}, {"line_start": "asc"}]}}',
    ),
    SeedPrompt(
        name="ports_of_module",
        description="Every port of a module, in declaration order.",
        sql=(
            "SELECT p.position, p.direction, p.kind, p.width, p.name "
            "FROM module_ports p JOIN modules m ON m.id = p.module_id "
            "WHERE m.name = :module ORDER BY p.position"
        ),
        params=[{"name": "module", "default": "", "type": "str"}],
        # module_ports carry a denormalized `module_name` — no join needed.
        dsl='{"index": "module_ports", "body": {"size": 1000, "query": {"term": '
            '{"module_name": ":module"}}, "sort": [{"position": "asc"}]}}',
    ),
    SeedPrompt(
        name="signals_of_module",
        description="Every internal signal (wire/reg/logic/...) declared inside a module.",
        sql=(
            "SELECT ms.line, ms.kind, ms.width, ms.name, ms.decl_text "
            "FROM module_signals ms JOIN modules m ON m.id = ms.module_id "
            "WHERE m.name = :module ORDER BY ms.line LIMIT :limit"
        ),
        params=[
            {"name": "module", "default": "", "type": "str"},
            {"name": "limit", "default": 200, "type": "int"},
        ],
        # module_signals carry a denormalized `module_name`.
        dsl='{"index": "module_signals", "body": {"size": ":limit", "query": '
            '{"term": {"module_name": ":module"}}, "sort": [{"line": "asc"}]}}',
    ),
    SeedPrompt(
        name="signal_declaration",
        description=(
            "Find where a signal (by bare name) is declared in the RTL — "
            "across modules and ports. Useful when a VCD shows a signal stuck "
            "at X and you want to see how (or whether) it's reset."
        ),
        sql=(
            "SELECT 'signal' AS where_, m.name AS module, m.file, ms.line, "
            "       ms.kind, ms.width, ms.decl_text "
            "FROM module_signals ms JOIN modules m ON m.id = ms.module_id "
            "WHERE ms.name = :name "
            "UNION ALL "
            "SELECT 'port' AS where_, m.name AS module, m.file, m.line_start AS line, "
            "       p.direction AS kind, p.width, p.name AS decl_text "
            "FROM module_ports p JOIN modules m ON m.id = p.module_id "
            "WHERE p.name = :name "
            "ORDER BY where_, file, line LIMIT :limit"
        ),
        params=[
            {"name": "name", "default": "", "type": "str"},
            {"name": "limit", "default": 50, "type": "int"},
        ],
    ),
    SeedPrompt(
        name="modules_in_file",
        description="Every module defined in a single source file.",
        sql=(
            "SELECT name, kind, line_start, line_end, body_summary "
            "FROM modules WHERE file LIKE :file ORDER BY line_start"
        ),
        params=[{"name": "file", "default": "%", "type": "str"}],
    ),
    SeedPrompt(
        name="instance_tree",
        description="Parent → child module instances (one level of the design hierarchy).",
        sql=(
            "SELECT m.name AS parent_module, mi.instance_name, "
            "       mi.child_module_name, m.file, mi.line "
            "FROM module_instances mi JOIN modules m ON m.id = mi.parent_module_id "
            "WHERE m.name LIKE :parent OR :parent = '' "
            "ORDER BY m.name, mi.line LIMIT :limit"
        ),
        params=[
            {"name": "parent", "default": "", "type": "str",
             "description": "Parent module name (empty for all)."},
            {"name": "limit", "default": 100, "type": "int"},
        ],
    ),
    # ---------- simulator output (new in xevdb 0.2) -------------------------
    SeedPrompt(
        name="sim_summary",
        description=(
            "Per-run summary: line count, total events, and counts by severity. "
            "Useful as a first-pass check that ingest worked and that the run "
            "actually emitted what you expected."
        ),
        sql=(
            "SELECT id AS run_id, name, source, line_count, n_events, "
            "       n_fatal, n_error, n_warning, severity_json "
            "FROM sim_runs ORDER BY id"
        ),
        params=[],
        # sim_runs already stores the per-run counts — just return the docs.
        dsl='{"index": "sim_runs", "body": {"size": 1000, "query": {"match_all": '
            '{}}, "sort": [{"id": "asc"}]}}',
    ),
    SeedPrompt(
        name="sim_errors",
        description=(
            "Every error-class event (UVM_FATAL, UVM_ERROR, ERROR, FATAL, "
            "ASSERTION). Joined back to sim_runs for the run name."
        ),
        sql=(
            "SELECT r.name AS run, e.line_no, e.severity, e.t, "
            "       e.ref_file, e.ref_line, e.message "
            "FROM sim_events e JOIN sim_runs r ON r.id = e.run_id "
            "WHERE e.severity IN ('UVM_FATAL','UVM_ERROR','FATAL','ERROR','ASSERTION') "
            "ORDER BY e.t IS NULL, e.t, e.line_no LIMIT :limit"
        ),
        params=[{"name": "limit", "default": 100, "type": "int"}],
        dsl='{"index": "sim_events", "body": {"size": ":limit", "query": {"terms": '
            '{"severity": ["UVM_FATAL", "UVM_ERROR", "FATAL", "ERROR", "ASSERTION"]}}, '
            '"sort": [{"t": {"order": "asc", "missing": "_last"}}, {"line_no": "asc"}]}}',
    ),
    SeedPrompt(
        name="sim_around_time",
        description=(
            "Every event whose simulation time falls in [:t0, :t1]. Use to "
            "find what the simulator said around the moment a signal changed."
        ),
        sql=(
            "SELECT r.name AS run, e.line_no, e.severity, e.t, "
            "       e.ref_file, e.ref_line, e.message "
            "FROM sim_events e JOIN sim_runs r ON r.id = e.run_id "
            "WHERE e.t BETWEEN :t0 AND :t1 "
            "ORDER BY e.t, e.line_no LIMIT :limit"
        ),
        params=[
            {"name": "t0", "default": 0, "type": "int",
             "description": "Window start (simulation time)."},
            {"name": "t1", "default": 9223372036854775807, "type": "int",
             "description": "Window end."},
            {"name": "limit", "default": 100, "type": "int"},
        ],
        dsl='{"index": "sim_events", "body": {"size": ":limit", "query": {"range": '
            '{"t": {"gte": ":t0", "lte": ":t1"}}}, "sort": [{"t": "asc"}, '
            '{"line_no": "asc"}]}}',
    ),
    SeedPrompt(
        name="sim_by_ref_file",
        description=(
            "Every event whose message embedded a `file:line` reference "
            "matching :file. Find all the testbench/RTL callouts about one "
            "specific source file."
        ),
        sql=(
            "SELECT r.name AS run, e.line_no, e.severity, e.t, "
            "       e.ref_file, e.ref_line, e.message "
            "FROM sim_events e JOIN sim_runs r ON r.id = e.run_id "
            "WHERE e.ref_file LIKE :file "
            "ORDER BY e.ref_file, e.ref_line, e.t LIMIT :limit"
        ),
        params=[
            {"name": "file", "default": "%", "type": "str",
             "description": "SQL LIKE pattern (%foo.sv%)."},
            {"name": "limit", "default": 100, "type": "int"},
        ],
    ),
    SeedPrompt(
        name="sim_with_rtl",
        description=(
            "Sim events whose `ref_file:ref_line` lands inside a parsed RTL "
            "module — bridges the simulator log to the source code. Joins "
            "sim_events with modules so each row shows the implicated module "
            "and its file range."
        ),
        sql=(
            "SELECT r.name AS run, e.line_no, e.severity, e.t, "
            "       e.ref_file, e.ref_line, m.name AS module, "
            "       m.line_start AS mod_start, m.line_end AS mod_end, "
            "       e.message "
            "FROM sim_events e "
            "JOIN sim_runs r ON r.id = e.run_id "
            "JOIN modules m ON m.file LIKE '%' || e.ref_file "
            "WHERE e.ref_file <> '' "
            "  AND e.ref_line BETWEEN m.line_start AND m.line_end "
            "ORDER BY e.severity, e.t, e.line_no LIMIT :limit"
        ),
        params=[{"name": "limit", "default": 50, "type": "int"}],
    ),
    SeedPrompt(
        name="xz_signals_with_rtl",
        description=(
            "Signals that ever carried x/z, with their RTL declaration line "
            "if a same-named signal exists in any module. The bridge between "
            "the waveform side and the RTL side."
        ),
        sql=(
            "SELECT DISTINCT s.fullname, s.width, s.kind AS vcd_kind, "
            "       m.name AS module, m.file, ms.line AS decl_line, ms.kind AS decl_kind, "
            "       ms.decl_text "
            "FROM signals s JOIN changes c ON c.sig_id = s.id "
            "LEFT JOIN module_signals ms ON ms.name = s.name "
            "     OR ms.name = substr(s.name, 1, "
            "        CASE WHEN instr(s.name, '[') > 0 THEN instr(s.name, '[') - 1 "
            "             ELSE length(s.name) END) "
            "LEFT JOIN modules m ON m.id = ms.module_id "
            "WHERE c.value LIKE '%x%' OR c.value LIKE '%z%' "
            "   OR c.value LIKE '%X%' OR c.value LIKE '%Z%' "
            "ORDER BY s.fullname LIMIT :limit"
        ),
        params=[{"name": "limit", "default": 50, "type": "int"}],
    ),
    # ---------- bug knowledge base -----------------------------------------
    SeedPrompt(
        name="bug_search",
        description="Search the bug KB (portable LIKE over title/symptom/root-cause/"
                    "fix/keywords). The `bug search` CLI uses FTS5 ranking when available.",
        sql=(
            "SELECT name, status, severity, title FROM bugs "
            "WHERE title LIKE '%'||:query||'%' OR symptom LIKE '%'||:query||'%' "
            "   OR root_cause LIKE '%'||:query||'%' OR fix LIKE '%'||:query||'%' "
            "   OR keywords_json LIKE '%'||:query||'%' "
            "ORDER BY updated_at DESC LIMIT :limit"
        ),
        params=[
            {"name": "query", "default": "", "type": "str"},
            {"name": "limit", "default": 50, "type": "int"},
        ],
        dsl='{"index": "bugs", "body": {"size": ":limit", "query": {"multi_match": '
            '{"query": ":query", "fields": ["title", "symptom", "root_cause", "fix", '
            '"keywords", "tags"]}}}}',
    ),
    SeedPrompt(
        name="bugs_by_status",
        description="Bugs with a given status (open/investigating/fixed/wontfix), newest first.",
        sql=(
            "SELECT name, severity, title, updated_at FROM bugs "
            "WHERE status = :status ORDER BY updated_at DESC LIMIT :limit"
        ),
        params=[
            {"name": "status", "default": "open", "type": "str"},
            {"name": "limit", "default": 100, "type": "int"},
        ],
        dsl='{"index": "bugs", "body": {"size": ":limit", "query": {"term": '
            '{"status": ":status"}}, "sort": [{"updated_at": "desc"}]}}',
    ),
    SeedPrompt(
        name="bugs_for_signal",
        description="Bugs linked to a given signal (by fullname or bare name).",
        sql=(
            "SELECT DISTINCT b.name, b.status, b.severity, b.title "
            "FROM bugs b JOIN bug_links bl ON bl.bug_name = b.name "
            "WHERE bl.kind = 'signal' AND (bl.value = :signal OR bl.value LIKE '%.'||:signal) "
            "ORDER BY b.updated_at DESC LIMIT :limit"
        ),
        params=[
            {"name": "signal", "default": "", "type": "str"},
            {"name": "limit", "default": 50, "type": "int"},
        ],
        # OpenSearch: signals are a denormalized array on the bug doc — no join.
        dsl='{"index": "bugs", "body": {"size": ":limit", "query": {"term": '
            '{"signals": ":signal"}}}}',
    ),
    SeedPrompt(
        name="bugs_for_module",
        description="Bugs linked to a given module.",
        sql=(
            "SELECT DISTINCT b.name, b.status, b.severity, b.title "
            "FROM bugs b JOIN bug_links bl ON bl.bug_name = b.name "
            "WHERE bl.kind = 'module' AND bl.value = :module "
            "ORDER BY b.updated_at DESC LIMIT :limit"
        ),
        params=[
            {"name": "module", "default": "", "type": "str"},
            {"name": "limit", "default": 50, "type": "int"},
        ],
        dsl='{"index": "bugs", "body": {"size": ":limit", "query": {"term": '
            '{"modules": ":module"}}}}',
    ),
    SeedPrompt(
        name="bugs_with_rtl",
        description="Cross — bugs whose `ref` (file:line) lands inside a parsed module. "
                    "SQL-only (true cross-index join).",
        sql=(
            "SELECT b.name, b.status, bl.value AS ref, m.name AS module, "
            "       m.file, m.line_start, m.line_end "
            "FROM bugs b "
            "JOIN bug_links bl ON bl.bug_name = b.name AND bl.kind = 'ref' "
            "JOIN modules m "
            "  ON (m.file = substr(bl.value, 1, instr(bl.value, ':') - 1) "
            "      OR m.file LIKE '%'||substr(bl.value, 1, instr(bl.value, ':') - 1)) "
            " AND CAST(substr(bl.value, instr(bl.value, ':') + 1) AS INTEGER) "
            "       BETWEEN m.line_start AND m.line_end "
            "ORDER BY b.name LIMIT :limit"
        ),
        params=[{"name": "limit", "default": 100, "type": "int"}],
    ),
    SeedPrompt(
        name="xz_signals_with_open_bugs",
        description="Cross — VCD X/Z signals that already have a non-fixed bug linked. "
                    "SQL-only (true cross-index join).",
        sql=(
            "SELECT DISTINCT s.fullname, b.name AS bug, b.status, b.severity "
            "FROM signals s JOIN changes c ON c.sig_id = s.id "
            "JOIN bug_links bl ON bl.kind = 'signal' "
            "     AND (bl.value = s.fullname OR bl.value LIKE '%.'||s.name) "
            "JOIN bugs b ON b.name = bl.bug_name AND b.status != 'fixed' "
            "WHERE c.value LIKE '%x%' OR c.value LIKE '%z%' "
            "   OR c.value LIKE '%X%' OR c.value LIKE '%Z%' "
            "ORDER BY s.fullname LIMIT :limit"
        ),
        params=[{"name": "limit", "default": 100, "type": "int"}],
    ),

    # ---------- RISC-V ISA reference (opensearch-only knowledge base) -------
    # These run on the standalone riscv reference dataset built by
    # `xevdb ingest-riscv`. The `dsl` is what executes on OpenSearch; the `sql`
    # mirrors it for a future SQLite port (the riscv_* tables are OS-only today).
    SeedPrompt(
        name="riscv_instr_search",
        description="Search RISC-V instructions by mnemonic, syntax, or description.",
        sql=(
            "SELECT name, extension, format, syntax, description "
            "FROM riscv_instructions "
            "WHERE name LIKE '%'||:query||'%' OR description LIKE '%'||:query||'%' "
            "   OR syntax LIKE '%'||:query||'%' ORDER BY name LIMIT :limit"
        ),
        params=[
            {"name": "query", "default": "", "type": "str"},
            {"name": "limit", "default": 50, "type": "int"},
        ],
        dsl='{"index": "riscv_instructions", "body": {"size": ":limit", '
            '"query": {"multi_match": {"query": ":query", "fields": '
            '["name", "mnemonic", "description", "syntax", "operands"]}}}}',
    ),
    SeedPrompt(
        name="riscv_instr_by_name",
        description="Exact RISC-V instruction lookup (encoding + format) by mnemonic.",
        sql=(
            "SELECT name, extension, format, mask, match, syntax, description "
            "FROM riscv_instructions WHERE name = :name LIMIT :limit"
        ),
        params=[
            {"name": "name", "default": "", "type": "str"},
            {"name": "limit", "default": 5, "type": "int"},
        ],
        dsl='{"index": "riscv_instructions", "body": {"size": ":limit", '
            '"query": {"term": {"name": ":name"}}}}',
    ),
    SeedPrompt(
        name="riscv_by_extension",
        description="List the instructions defined by an ISA extension "
                    "(RV32I/RV64I/M/A/F/D/C/Zicsr/...).",
        sql=(
            "SELECT name, format, syntax, description FROM riscv_instructions "
            "WHERE extension = :extension ORDER BY name LIMIT :limit"
        ),
        params=[
            {"name": "extension", "default": "RV32I", "type": "str"},
            {"name": "limit", "default": 100, "type": "int"},
        ],
        dsl='{"index": "riscv_instructions", "body": {"size": ":limit", '
            '"query": {"term": {"extension": ":extension"}}, '
            '"sort": [{"name": "asc"}]}}',
    ),
    SeedPrompt(
        name="riscv_csr_lookup",
        description="Find a control/status register by name or description.",
        sql=(
            "SELECT addr, name, privilege, access, description FROM riscv_csrs "
            "WHERE name LIKE '%'||:query||'%' OR description LIKE '%'||:query||'%' "
            "ORDER BY addr LIMIT :limit"
        ),
        params=[
            {"name": "query", "default": "", "type": "str"},
            {"name": "limit", "default": 50, "type": "int"},
        ],
        dsl='{"index": "riscv_csrs", "body": {"size": ":limit", '
            '"query": {"multi_match": {"query": ":query", '
            '"fields": ["name", "description"]}}}}',
    ),
    SeedPrompt(
        name="riscv_csr_by_addr",
        description="Decode a CSR number (e.g. 0x305 seen in csrr/csrw) to its name.",
        sql=(
            "SELECT addr, name, privilege, access, description FROM riscv_csrs "
            "WHERE addr = :addr LIMIT :limit"
        ),
        params=[
            {"name": "addr", "default": "0x300", "type": "str"},
            {"name": "limit", "default": 5, "type": "int"},
        ],
        dsl='{"index": "riscv_csrs", "body": {"size": ":limit", '
            '"query": {"term": {"addr": ":addr"}}}}',
    ),
    SeedPrompt(
        name="riscv_reg_lookup",
        description="Resolve a register by x-name or ABI name (e.g. a0 -> x10, "
                    "caller-saved).",
        sql=(
            'SELECT name, abi, number, "group", role, saver FROM riscv_registers '
            "WHERE name = :query OR abi = :query OR role LIKE '%'||:query||'%' "
            "ORDER BY number LIMIT :limit"
        ),
        params=[
            {"name": "query", "default": "a0", "type": "str"},
            {"name": "limit", "default": 10, "type": "int"},
        ],
        dsl='{"index": "riscv_registers", "body": {"size": ":limit", '
            '"query": {"bool": {"should": [{"term": {"name": ":query"}}, '
            '{"term": {"abi": ":query"}}, {"match": {"role": ":query"}}], '
            '"minimum_should_match": 1}}, "sort": [{"number": "asc"}]}}',
    ),
    SeedPrompt(
        name="riscv_pseudo_search",
        description="Search pseudo-instructions and their real-instruction expansion.",
        sql=(
            "SELECT name, expansion, base, description FROM riscv_pseudo "
            "WHERE name LIKE '%'||:query||'%' OR expansion LIKE '%'||:query||'%' "
            "   OR description LIKE '%'||:query||'%' ORDER BY name LIMIT :limit"
        ),
        params=[
            {"name": "query", "default": "", "type": "str"},
            {"name": "limit", "default": 50, "type": "int"},
        ],
        dsl='{"index": "riscv_pseudo", "body": {"size": ":limit", '
            '"query": {"multi_match": {"query": ":query", '
            '"fields": ["name", "expansion", "base", "description"]}}}}',
    ),
    SeedPrompt(
        name="riscv_ext_overview",
        description="Instruction count per ISA extension (overview).",
        sql=(
            "SELECT extension, COUNT(*) AS instructions FROM riscv_instructions "
            "GROUP BY extension ORDER BY instructions DESC LIMIT :limit"
        ),
        params=[{"name": "limit", "default": 50, "type": "int"}],
        dsl='{"index": "riscv_instructions", "rows": "aggs:exts", '
            '"body": {"size": 0, "aggs": {"exts": {"terms": '
            '{"field": "extension", "size": ":limit"}}}}}',
    ),

    # ---------- RISC-V Linux kernel architecture (opensearch-only) ----------
    # Built by `xevdb ingest-kernel`. dsl runs on OpenSearch; sql mirrors it.
    SeedPrompt(
        name="kernel_syscall_by_nr",
        description="Decode a syscall number (the value in a7 at an ecall) to its name.",
        sql=("SELECT nr, name, entry, abi, description FROM kernel_syscalls "
             "WHERE nr = :nr LIMIT :limit"),
        params=[
            {"name": "nr", "default": 64, "type": "int"},
            {"name": "limit", "default": 5, "type": "int"},
        ],
        dsl='{"index": "kernel_syscalls", "body": {"size": ":limit", '
            '"query": {"term": {"nr": ":nr"}}}}',
    ),
    SeedPrompt(
        name="kernel_syscall_search",
        description="Search Linux syscalls by name, entry symbol, or description.",
        sql=("SELECT nr, name, entry, description FROM kernel_syscalls "
             "WHERE name LIKE '%'||:query||'%' OR description LIKE '%'||:query||'%' "
             "ORDER BY nr LIMIT :limit"),
        params=[
            {"name": "query", "default": "", "type": "str"},
            {"name": "limit", "default": 50, "type": "int"},
        ],
        dsl='{"index": "kernel_syscalls", "body": {"size": ":limit", '
            '"query": {"multi_match": {"query": ":query", '
            '"fields": ["name", "entry", "description"]}}}}',
    ),
    SeedPrompt(
        name="kernel_trap_by_code",
        description="Decode an scause/mcause code to its trap cause "
                    "(kind = exception or interrupt).",
        sql=("SELECT code, kind, name, label, description FROM kernel_traps "
             "WHERE code = :code AND kind = :kind LIMIT :limit"),
        params=[
            {"name": "code", "default": 8, "type": "int"},
            {"name": "kind", "default": "exception", "type": "str"},
            {"name": "limit", "default": 5, "type": "int"},
        ],
        dsl='{"index": "kernel_traps", "body": {"size": ":limit", '
            '"query": {"bool": {"filter": [{"term": {"code": ":code"}}, '
            '{"term": {"kind": ":kind"}}]}}}}',
    ),
    SeedPrompt(
        name="kernel_trap_search",
        description="Search trap causes (exceptions + interrupts) by name or text.",
        sql=("SELECT code, kind, name, label, description FROM kernel_traps "
             "WHERE name LIKE '%'||:query||'%' OR label LIKE '%'||:query||'%' "
             "   OR description LIKE '%'||:query||'%' ORDER BY kind, code LIMIT :limit"),
        params=[
            {"name": "query", "default": "", "type": "str"},
            {"name": "limit", "default": 50, "type": "int"},
        ],
        dsl='{"index": "kernel_traps", "body": {"size": ":limit", '
            '"query": {"multi_match": {"query": ":query", '
            '"fields": ["name", "label", "description"]}}}}',
    ),
    SeedPrompt(
        name="kernel_sbi_search",
        description="Search SBI extensions and functions (the S<->M ABI).",
        sql=("SELECT kind, extension, name, eid, fid, description FROM kernel_sbi "
             "WHERE name LIKE '%'||:query||'%' OR extension LIKE '%'||:query||'%' "
             "   OR description LIKE '%'||:query||'%' LIMIT :limit"),
        params=[
            {"name": "query", "default": "", "type": "str"},
            {"name": "limit", "default": 50, "type": "int"},
        ],
        dsl='{"index": "kernel_sbi", "body": {"size": ":limit", '
            '"query": {"multi_match": {"query": ":query", '
            '"fields": ["name", "extension", "description"]}}}}',
    ),
    SeedPrompt(
        name="kernel_sbi_functions",
        description="List the function IDs of one SBI extension (e.g. HSM, TIME, RFENCE).",
        sql=("SELECT name, fid, description FROM kernel_sbi "
             "WHERE extension = :extension AND kind = 'function' "
             "ORDER BY fid LIMIT :limit"),
        params=[
            {"name": "extension", "default": "HSM", "type": "str"},
            {"name": "limit", "default": 50, "type": "int"},
        ],
        dsl='{"index": "kernel_sbi", "body": {"size": ":limit", '
            '"query": {"bool": {"filter": [{"term": {"extension": ":extension"}}, '
            '{"term": {"kind": "function"}}]}}, "sort": [{"fid": "asc"}]}}',
    ),
    SeedPrompt(
        name="kernel_memmap_lookup",
        description="Search the kernel virtual-memory layout / boot ABI by name or text.",
        sql=("SELECT mode, region, start, end, size, description FROM kernel_memmap "
             "WHERE region LIKE '%'||:query||'%' OR description LIKE '%'||:query||'%' "
             "LIMIT :limit"),
        params=[
            {"name": "query", "default": "", "type": "str"},
            {"name": "limit", "default": 50, "type": "int"},
        ],
        dsl='{"index": "kernel_memmap", "body": {"size": ":limit", '
            '"query": {"multi_match": {"query": ":query", '
            '"fields": ["region", "description", "mode"]}}}}',
    ),
    SeedPrompt(
        name="kernel_memmap_by_mode",
        description="List the VM-layout regions for a paging mode (Sv39/Sv48/Sv57).",
        sql=("SELECT region, start, end, size, description FROM kernel_memmap "
             "WHERE mode = :mode AND category = 'vm-layout' "
             "ORDER BY start LIMIT :limit"),
        params=[
            {"name": "mode", "default": "Sv39", "type": "str"},
            {"name": "limit", "default": 50, "type": "int"},
        ],
        dsl='{"index": "kernel_memmap", "body": {"size": ":limit", '
            '"query": {"bool": {"filter": [{"term": {"mode": ":mode"}}, '
            '{"term": {"category": "vm-layout"}}]}}, "sort": [{"start": "asc"}]}}',
    ),
]


def install(con: sqlite3.Connection) -> int:
    """Insert any missing seed prompts. Existing rows with the same name are
    left alone (the user may have edited them). Returns rows inserted."""
    now = time.time()
    n = 0
    for p in PROMPTS:
        cur = con.execute("SELECT 1 FROM prompts WHERE name = ?", (p.name,))
        if cur.fetchone() is not None:
            continue
        con.execute(
            "INSERT INTO prompts (name, description, sql, dsl_json, params_json, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (p.name, p.description, p.sql, p.dsl, json.dumps(p.params), now, now),
        )
        n += 1
    return n
