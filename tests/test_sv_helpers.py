"""Unit tests for the pure helpers in `xevdb.sv`.

`sv.py` sits at ~26% coverage because every test that would exercise it is
skipped when the external `sv-parse` binary isn't built — and CI never builds
it, so the skip is permanent. But a large slice of the module is pure logic
over already-parsed AST dicts and source strings: offset/line math, expression
and dimension rendering, port/param extraction, and comment scanning. None of
that needs the binary. These tests pin that behavior so refactors to the
rendering pipeline are caught in CI.
"""
from __future__ import annotations

from xevdb import sv


# --------------------------------------------------------------------------
# line/offset math
# --------------------------------------------------------------------------

def test_line_starts():
    assert sv._line_starts("a\nbb\nc") == [0, 2, 5]
    assert sv._line_starts("") == [0]
    assert sv._line_starts("no newline") == [0]


def test_offset_to_line_maps_offsets_to_1_based_lines():
    src = "aaa\nbbb\nccc"
    starts = sv._line_starts(src)
    assert sv._offset_to_line(starts, 0) == 1
    assert sv._offset_to_line(starts, 2) == 1
    assert sv._offset_to_line(starts, 4) == 2      # first char of line 2
    assert sv._offset_to_line(starts, 8) == 3


def test_slice_line_returns_line_text_without_newline():
    src = "module m;\n  wire clk;\nendmodule\n"
    starts = sv._line_starts(src)
    assert sv._slice_line(src, starts, 1) == "module m;"
    assert sv._slice_line(src, starts, 2) == "  wire clk;"
    # Out-of-range lines return empty.
    assert sv._slice_line(src, starts, 0) == ""
    assert sv._slice_line(src, starts, 99) == ""


# --------------------------------------------------------------------------
# expression rendering
# --------------------------------------------------------------------------

def _ident(name):
    return {"kind": {"Ident": {"path": [{"name": {"name": name}}]}}}


def _int(value):
    return {"kind": {"Number": {"Integer": {"value": value}}}}


def test_render_expr_ident_and_number():
    assert sv._render_expr(_ident("WIDTH")) == "WIDTH"
    assert sv._render_expr(_int(8)) == "8"


def test_render_expr_binary():
    e = {"kind": {"Binary": {"op": "Sub", "left": _ident("WIDTH"), "right": _int(1)}}}
    assert sv._render_expr(e) == "WIDTH-1"


def test_render_expr_unary():
    e = {"kind": {"Unary": {"op": "-", "expr": _int(1)}}}
    assert sv._render_expr(e) == "-1"


def test_render_expr_handles_garbage():
    assert sv._render_expr(None) == "?"
    assert sv._render_expr({"kind": "not-a-dict"}) == "?"
    assert sv._render_expr({"kind": {"Mystery": {}}}) == "?"


# --------------------------------------------------------------------------
# dimension rendering
# --------------------------------------------------------------------------

def test_render_dims_range_and_sized():
    dims = [{"Range": {"left": _ident("WIDTH"), "right": _int(0)}}]
    assert sv._render_dims(dims) == "[WIDTH:0]"
    assert sv._render_dims([{"Sized": _int(4)}]) == "[4]"
    assert sv._render_dims([{"Unknown": {}}]) == "[?]"


def test_render_dims_empty():
    assert sv._render_dims(None) == ""
    assert sv._render_dims([]) == ""


# --------------------------------------------------------------------------
# data-type inspection
# --------------------------------------------------------------------------

def test_data_type_kind_and_dims():
    dims = [{"Range": {"left": _int(7), "right": _int(0)}}]
    dt = {"IntegerVector": {"kind": "Logic", "dimensions": dims}}
    assert sv._data_type_kind(dt) == "logic"
    assert sv._data_type_dims(dt) == dims


def test_data_type_kind_implicit_and_missing():
    assert sv._data_type_kind({"Implicit": {}}) == ""
    assert sv._data_type_kind(None) == ""
    assert sv._data_type_dims(None) == []


# --------------------------------------------------------------------------
# port extraction
# --------------------------------------------------------------------------

def test_extract_ports_ansi():
    port_list = {"Ansi": [
        {"name": {"name": "clk"}, "direction": "Input", "data_type": {"Implicit": {}}},
        {"name": {"name": "data"}, "direction": "Output",
         "data_type": {"IntegerVector": {
             "kind": "Logic",
             "dimensions": [{"Range": {"left": _int(7), "right": _int(0)}}]}}},
    ]}
    ports = sv._extract_ports(port_list)
    assert [p.name for p in ports] == ["clk", "data"]
    assert [p.direction for p in ports] == ["input", "output"]
    assert ports[1].kind == "logic"
    assert ports[1].width == "[7:0]"


def test_extract_ports_non_ansi_and_garbage():
    assert [p.name for p in sv._extract_ports({"NonAnsi": [{"name": "a"}, {"name": "b"}]})] \
        == ["a", "b"]
    assert sv._extract_ports({}) == []
    assert sv._extract_ports("nope") == []


def test_extract_params():
    decl = {"params": [
        {"kind": {"Data": {"assignments": [
            {"name": {"name": "WIDTH"}}, {"name": {"name": "DEPTH"}},
        ]}}},
    ]}
    assert sv._extract_params(decl) == ["WIDTH", "DEPTH"]
    assert sv._extract_params({}) == []


# --------------------------------------------------------------------------
# module description iteration
# --------------------------------------------------------------------------

def test_iter_module_decls_yields_kind_and_body():
    ast = {"descriptions": [
        {"Module": {"name": "top"}},
        {"Interface": {"name": "bus_if"}},
        "junk",
        {"Unrelated": {}},
    ]}
    got = list(sv._iter_module_decls(ast))
    assert got[0][0] == "Module"
    assert got[0][1] == {"name": "top"}
    assert got[1][0] == "Interface"
    assert len(got) == 2       # 'junk' and 'Unrelated' skipped


# --------------------------------------------------------------------------
# leading-comment scanning
# --------------------------------------------------------------------------

def test_leading_comment_line_comments():
    src = "// a counter\n// with reset\nmodule counter;\n"
    offset = src.index("module")
    assert sv._leading_comment(src, offset) == "// a counter\n// with reset"


def test_leading_comment_block_comment():
    src = "/* AXI master */\nmodule m;\n"
    offset = src.index("module")
    assert sv._leading_comment(src, offset) == "/* AXI master */"


def test_leading_comment_absent():
    src = "module m;\n"
    assert sv._leading_comment(src, src.index("module")) == ""


# --------------------------------------------------------------------------
# module-offset search
# --------------------------------------------------------------------------

def test_find_module_offset():
    src = "// header\nmodule counter (input clk);\nendmodule\n"
    off = sv._find_module_offset(src, "module", "counter")
    assert src[off:off + len("module counter")] == "module counter"
    assert sv._find_module_offset(src, "module", "nonexistent") == -1


# --------------------------------------------------------------------------
# have_sv_parse gate
# --------------------------------------------------------------------------

def test_have_sv_parse_false_for_missing_binary():
    assert sv.have_sv_parse("/nonexistent/path/to/sv-parse-xyz") is False


# --------------------------------------------------------------------------
# body walk over a synthetic AST
# --------------------------------------------------------------------------

def test_walk_items_collects_signals_and_instances():
    src = "module m;\n  wire [3:0] bus;\n  sub u_sub ();\nendmodule\n"
    starts = sv._line_starts(src)
    items = [
        {"NetDeclaration": {
            "data_type": {"IntegerVector": {
                "kind": "Wire",
                "dimensions": [{"Range": {"left": _int(3), "right": _int(0)}}]}},
            "decls": [{"name": {"name": "bus"}, "span": {"start": src.index("bus")}}],
            "span": {"start": src.index("wire")},
        }},
        {"ModuleInstantiation": {
            "module": {"name": "sub"},
            "instances": [{"name": {"name": "u_sub"},
                           "span": {"start": src.index("u_sub")}}],
        }},
        {"AlwaysConstruct": {}},
        {"ContinuousAssign": {}},
    ]
    signals, instances, counts = sv._walk_items(items, starts, src)
    assert [(s.name, s.width) for s in signals] == [("bus", "[3:0]")]
    assert [(i.instance_name, i.module_name) for i in instances] == [("u_sub", "sub")]
    assert counts["always"] == 1
    assert counts["assign"] == 1
