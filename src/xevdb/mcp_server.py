"""A Model Context Protocol (MCP) server exposing one xevdb dataset to agents.

Zero third-party dependencies: MCP's stdio transport is newline-delimited
JSON-RPC 2.0, and a tools-only server needs just ``initialize`` / ``tools/list``
/ ``tools/call`` / ``ping``. That keeps the server in line with xevdb's
dependency-light core (only the optional OpenSearch backend pulls a package).

The server is bound to a single dataset at launch (`xevdb mcp <db>`), so every
tool operates on that database — point an agent at a waveform dump, or at the
RISC-V/kernel reference, and it can query the deterministic evidence directly
(values, windows, stored prompts, the bug KB, ISA/kernel decode) instead of
guessing. Run several servers (one per dataset) the way MCP clients expect.

The message handling (`handle`) is a pure function over a parsed JSON-RPC
message, so it is unit-tested without spawning a process or touching stdio.
"""
from __future__ import annotations

import dataclasses
import json
import sys
from typing import Any, Callable

from . import backends as _backends

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "xevdb"


def _version() -> str:
    try:
        from importlib.metadata import version
        return version("xevdb")
    except Exception:  # noqa: BLE001
        return "0.1.0"


# JSON-RPC error codes
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


def _to_jsonable(obj: Any) -> Any:
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        if hasattr(obj, "to_dict"):
            return obj.to_dict()
        return dataclasses.asdict(obj)
    if isinstance(obj, (list, tuple)):
        return [_to_jsonable(x) for x in obj]
    if isinstance(obj, dict):
        return {k: _to_jsonable(v) for k, v in obj.items()}
    return obj


class XevdbMcp:
    """MCP server bound to one xevdb dataset."""

    def __init__(self, db_path: str, backend_name: str | None = None):
        self.db_path = db_path
        self.backend_name = backend_name
        # Construct once; sessions are opened per tool call.
        self.backend = _backends.get_backend(backend_name, db_path)
        self.tools = _build_tools(self)

    # -- backend helpers ----------------------------------------------------

    def _session(self, *, read_only: bool = True):
        return self.backend.open(read_only=read_only)

    # -- tool implementations ----------------------------------------------

    def t_stats(self, _args: dict) -> Any:
        with self._session() as s:
            return self.backend.stats(s)

    def t_find_signals(self, args: dict) -> Any:
        pattern = args["pattern"]
        limit = int(args.get("limit", 50))
        with self._session() as s:
            sigs = self.backend.find_signals(s, pattern, limit)
        return {"pattern": pattern, "signals": [_to_jsonable(x) for x in sigs]}

    def t_signal_value_at(self, args: dict) -> Any:
        signal, t = args["signal"], int(args["time"])
        with self._session() as s:
            sig = self.backend.resolve_signal(s, signal)
            if sig is None:
                raise ValueError(f"signal not found or ambiguous: {signal!r}")
            res = self.backend.value_at(s, sig.sig_id, t)
        return {"signal": sig.fullname, "width": sig.width, "time": t,
                "last_t": res[0] if res else None,
                "value": res[1] if res else None}

    def t_signal_window(self, args: dict) -> Any:
        signal = args["signal"]
        t0 = args.get("from")
        t1 = args.get("to")
        limit = int(args.get("limit", 200))
        with self._session() as s:
            sig = self.backend.resolve_signal(s, signal)
            if sig is None:
                raise ValueError(f"signal not found or ambiguous: {signal!r}")
            rows = self.backend.window(
                s, sig.sig_id,
                int(t0) if t0 is not None else None,
                int(t1) if t1 is not None else None, limit)
        return {"signal": sig.fullname,
                "changes": [{"t": t, "value": v} for t, v in rows]}

    def t_list_prompts(self, _args: dict) -> Any:
        with self._session() as s:
            prompts = self.backend.list_prompts(s)
        return {"prompts": [
            {"name": p.name, "description": p.description,
             "params": [pp.get("name") for pp in p.params],
             "opensearch": bool(getattr(p, "dsl_json", ""))}
            for p in prompts]}

    def t_run_prompt(self, args: dict) -> Any:
        name = args["name"]
        pargs = args.get("args") or {}
        limit = args.get("limit")
        with self._session(read_only=False) as s:
            rows, cache_hit = self.backend.run_prompt(s, name, pargs)
        if limit is not None:
            rows = rows[: int(limit)]
        return {"prompt": name, "args": pargs, "cache_hit": cache_hit,
                "row_count": len(rows), "rows": _to_jsonable(rows)}

    def t_search_bugs(self, args: dict) -> Any:
        query = args["query"]
        status = args.get("status")
        limit = int(args.get("limit", 50))
        with self._session() as s:
            bugs = self.backend.search_bugs(s, query, status=status, limit=limit)
        return {"query": query, "bugs": [_to_jsonable(b) for b in bugs]}

    def t_show_source(self, args: dict) -> Any:
        if not self.backend.supports_raw_sql:
            raise ValueError(
                "show_source needs a relational (sqlite) backend; the "
                f"{self.backend.name!r} backend does not expose RTL source yet.")
        from . import show as _show
        target = args["target"]
        context = int(args.get("context", 6))
        full = bool(args.get("full", False))
        with self._session() as s:
            slices = _show.show_code(s, target, context=context, full=full)
            if not slices:
                raise ValueError(f"no RTL match for {target!r}")
            out = [{"file": sl.file, "start_line": sl.line_start,
                    "end_line": sl.line_end, "description": sl.description,
                    "code": _show.render(sl, with_lines=True)} for sl in slices]
        return {"target": target, "slices": out}

    def t_decode_instruction(self, args: dict) -> Any:
        from . import decode as _decode
        word = _decode.parse_word(str(args["word"]))
        return _decode.decode(word).to_dict()

    def t_decode_signal(self, args: dict) -> Any:
        from . import decode as _decode
        signal, t = args["signal"], int(args["time"])
        with self._session() as s:
            sig = self.backend.resolve_signal(s, signal)
            if sig is None:
                raise ValueError(f"signal not found or ambiguous: {signal!r}")
            res = self.backend.value_at(s, sig.sig_id, t)
        if res is None:
            raise ValueError(f"{sig.fullname} has no value at-or-before t={t}")
        last_t, value = res
        d = _decode.decode(_decode.parse_word(value))   # raises on x/z
        return {"signal": sig.fullname, "time": t, "last_t": last_t,
                "value": value, **d.to_dict()}

    # -- JSON-RPC dispatch (pure; unit-tested without stdio) ----------------

    def handle(self, msg: Any) -> dict | None:
        """Handle one parsed JSON-RPC message. Returns a response, or None for
        notifications (no id)."""
        if not isinstance(msg, dict):
            return _err(None, INVALID_REQUEST, "request must be an object")
        mid = msg.get("id")
        method = msg.get("method")
        params = msg.get("params") or {}
        is_notification = "id" not in msg

        try:
            if method == "initialize":
                result = {
                    "protocolVersion": params.get("protocolVersion", PROTOCOL_VERSION),
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": SERVER_NAME, "version": _version()},
                    "instructions": (
                        f"xevdb dataset at {self.db_path}. Use tools/list to see "
                        "available queries; run_prompt + list_prompts cover the "
                        "stored prompt library (waveform/RTL/sim/bug/RISC-V/kernel)."),
                }
            elif method in ("notifications/initialized", "initialized"):
                return None
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": [
                    {"name": t["name"], "description": t["description"],
                     "inputSchema": t["inputSchema"]} for t in self.tools]}
            elif method == "tools/call":
                return self._tools_call(mid, params, is_notification)
            else:
                if is_notification:
                    return None
                return _err(mid, METHOD_NOT_FOUND, f"unknown method {method!r}")
        except Exception as e:  # noqa: BLE001
            if is_notification:
                return None
            return _err(mid, INTERNAL_ERROR, str(e))

        if is_notification:
            return None
        return {"jsonrpc": "2.0", "id": mid, "result": result}

    def _tools_call(self, mid: Any, params: dict, is_notification: bool) -> dict | None:
        name = params.get("name")
        arguments = params.get("arguments") or {}
        tool = next((t for t in self.tools if t["name"] == name), None)
        if tool is None:
            return _err(mid, INVALID_PARAMS, f"unknown tool {name!r}")
        try:
            result = tool["handler"](arguments)
            content = [{"type": "text", "text": json.dumps(
                _to_jsonable(result), indent=2, default=str)}]
            payload = {"content": content, "isError": False}
        except Exception as e:  # noqa: BLE001 - report tool errors to the agent
            payload = {"content": [{"type": "text", "text": f"error: {e}"}],
                       "isError": True}
        if is_notification:
            return None
        return {"jsonrpc": "2.0", "id": mid, "result": payload}

    # -- stdio loop ---------------------------------------------------------

    def serve(self, stdin=None, stdout=None) -> None:
        stdin = stdin or sys.stdin
        stdout = stdout or sys.stdout
        for line in stdin:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                _write(stdout, _err(None, PARSE_ERROR, "invalid JSON"))
                continue
            messages = msg if isinstance(msg, list) else [msg]
            for m in messages:
                resp = self.handle(m)
                if resp is not None:
                    _write(stdout, resp)


def _err(mid: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


def _write(stdout, obj: dict) -> None:
    stdout.write(json.dumps(obj) + "\n")
    stdout.flush()


def _build_tools(srv: "XevdbMcp") -> list[dict]:
    def tool(name: str, description: str, handler: Callable, props: dict,
             required: list[str] | None = None) -> dict:
        return {"name": name, "description": description, "handler": handler,
                "inputSchema": {"type": "object", "properties": props,
                                "required": required or []}}

    S = {"type": "string"}
    I = {"type": "integer"}
    return [
        tool("stats", "Dataset overview: meta + per-table/-index row counts.",
             srv.t_stats, {}),
        tool("find_signals",
             "Glob/substring search for signals by name (e.g. '*pc*').",
             srv.t_find_signals,
             {"pattern": S, "limit": I}, ["pattern"]),
        tool("signal_value_at",
             "Last value of a signal at-or-before a timestamp (correct VCD "
             "last-change semantics).",
             srv.t_signal_value_at, {"signal": S, "time": I}, ["signal", "time"]),
        tool("signal_window",
             "All value changes of a signal within an optional [from,to] window.",
             srv.t_signal_window,
             {"signal": S, "from": I, "to": I, "limit": I}, ["signal"]),
        tool("list_prompts",
             "List the stored prompt library (waveform/RTL/sim/bug/RISC-V/kernel) "
             "with their parameters.",
             srv.t_list_prompts, {}),
        tool("run_prompt",
             "Run a stored prompt by name with arguments. Covers signal queries, "
             "RTL, sim logs, the bug KB, and RISC-V/kernel decode (e.g. "
             "riscv_csr_by_addr {addr:'0x305'}, kernel_syscall_by_nr {nr:64}).",
             srv.t_run_prompt,
             {"name": S, "args": {"type": "object"}, "limit": I}, ["name"]),
        tool("search_bugs",
             "Full-text search the bug knowledge base.",
             srv.t_search_bugs, {"query": S, "status": S, "limit": I}, ["query"]),
        tool("decode_instruction",
             "Decode a RISC-V instruction word (hex/bin/decimal) to assembly "
             "using the bundled ISA — e.g. '0x00c58533' -> 'add a0, a1, a2'.",
             srv.t_decode_instruction, {"word": S}, ["word"]),
        tool("decode_signal",
             "Read the instruction word carried by a signal at time T off the "
             "waveform and disassemble it.",
             srv.t_decode_signal, {"signal": S, "time": I}, ["signal", "time"]),
        tool("show_source",
             "Show the RTL source for a module / signal / file:line "
             "(sqlite backend only).",
             srv.t_show_source,
             {"target": S, "context": I, "full": {"type": "boolean"}}, ["target"]),
    ]


def serve(db_path: str, backend_name: str | None = None) -> None:
    """Entry point used by the `xevdb mcp` CLI command."""
    XevdbMcp(db_path, backend_name).serve()
