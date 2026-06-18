"""MCP server: JSON-RPC dispatch + tools over a real (sqlite) dataset.

Drives the pure `handle()` method directly (no subprocess), against a small
.xevdb built from the counter fixture, plus an end-to-end stdio round-trip.
"""
from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from xevdb import db as _db
from xevdb.mcp_server import XevdbMcp

REPO = Path(__file__).resolve().parents[1]
VCD = REPO / "examples" / "simple" / "counter.vcd"
RTL = REPO / "examples" / "simple"


@pytest.fixture
def srv(tmp_path):
    dbp = tmp_path / "counter.xevdb"
    _db.build(VCD, dbp, reset=True)
    try:
        _db.ingest_rtl(RTL, dbp)
    except Exception:
        pass                      # sv-parse may be unavailable; RTL tools just no-op
    return XevdbMcp(str(dbp), "sqlite")


def _call(srv, name, arguments=None, mid=1):
    resp = srv.handle({"jsonrpc": "2.0", "id": mid, "method": "tools/call",
                       "params": {"name": name, "arguments": arguments or {}}})
    assert resp["id"] == mid and "result" in resp
    res = resp["result"]
    payload = json.loads(res["content"][0]["text"])
    return res, payload


# ---------------- protocol ----------------

def test_initialize(srv):
    resp = srv.handle({"jsonrpc": "2.0", "id": 0, "method": "initialize",
                       "params": {"protocolVersion": "2024-11-05"}})
    r = resp["result"]
    assert r["serverInfo"]["name"] == "xevdb"
    assert "tools" in r["capabilities"]
    assert r["protocolVersion"] == "2024-11-05"


def test_initialized_notification_has_no_response(srv):
    # a notification (no id) must not produce a response
    assert srv.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None


def test_ping(srv):
    resp = srv.handle({"jsonrpc": "2.0", "id": 9, "method": "ping"})
    assert resp["result"] == {}


def test_unknown_method(srv):
    resp = srv.handle({"jsonrpc": "2.0", "id": 2, "method": "bogus"})
    assert resp["error"]["code"] == -32601


def test_tools_list(srv):
    resp = srv.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/list"})
    names = {t["name"] for t in resp["result"]["tools"]}
    assert {"stats", "find_signals", "signal_value_at", "signal_window",
            "list_prompts", "run_prompt", "search_bugs", "show_source"} <= names
    # every tool advertises a JSON-Schema object
    for t in resp["result"]["tools"]:
        assert t["inputSchema"]["type"] == "object"


# ---------------- tools ----------------

def test_stats(srv):
    _res, payload = _call(srv, "stats")
    assert payload["row_counts"]["signals"] == 4
    assert payload["row_counts"]["changes"] == 16


def test_find_signals(srv):
    _res, payload = _call(srv, "find_signals", {"pattern": "count"})
    assert any("count" in s["fullname"] for s in payload["signals"])


def test_signal_value_at(srv):
    _res, payload = _call(srv, "signal_value_at", {"signal": "count", "time": 25})
    assert payload["value"] is not None and payload["last_t"] <= 25


def test_signal_window(srv):
    _res, payload = _call(srv, "signal_window",
                          {"signal": "count", "from": 0, "to": 40})
    ts = [c["t"] for c in payload["changes"]]
    assert ts == sorted(ts) and all(0 <= t <= 40 for t in ts)


def test_list_and_run_prompt(srv):
    _res, payload = _call(srv, "list_prompts")
    names = {p["name"] for p in payload["prompts"]}
    assert "change_count" in names
    _res, run = _call(srv, "run_prompt", {"name": "change_count", "args": {"limit": 5}})
    assert run["prompt"] == "change_count" and run["row_count"] >= 1


def test_tool_error_is_reported_not_raised(srv):
    resp = srv.handle({"jsonrpc": "2.0", "id": 5, "method": "tools/call",
                       "params": {"name": "signal_value_at",
                                  "arguments": {"signal": "nope_xyz", "time": 0}}})
    res = resp["result"]
    assert res["isError"] is True                      # reported, not a JSON-RPC error
    assert "not found" in res["content"][0]["text"]


def test_unknown_tool(srv):
    resp = srv.handle({"jsonrpc": "2.0", "id": 7, "method": "tools/call",
                       "params": {"name": "frobnicate", "arguments": {}}})
    assert resp["error"]["code"] == -32602


# ---------------- stdio round-trip ----------------

def test_serve_stdio_roundtrip(srv):
    inp = io.StringIO(
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}) + "\n"
        + json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n"
        + json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                      "params": {"name": "stats", "arguments": {}}}) + "\n")
    out = io.StringIO()
    srv.serve(stdin=inp, stdout=out)
    lines = [json.loads(x) for x in out.getvalue().splitlines() if x.strip()]
    # initialize response + stats response (the notification yields nothing)
    assert [m["id"] for m in lines] == [1, 2]
    stats = json.loads(lines[1]["result"]["content"][0]["text"])
    assert stats["row_counts"]["signals"] == 4
