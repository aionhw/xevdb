"""SystemVerilog parsing via the vendored `sv-parse` Rust CLI.

Shells out to xezim-parser/target/release/sv-parse with --dump-json, then
walks the AST to extract: module declarations, port lists, internal
wire/reg/logic declarations, and child instantiations. The original source
text is captured alongside so we can slice code for `xevdb show` later.
"""
from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator


DEFAULT_SV_PARSE = os.environ.get(
    "XEVDB_SV_PARSE",
    str(Path(__file__).resolve().parents[2] / "xezim-core" / "xezim-parser"
        / "target" / "release" / "sv-parse"),
)

_RTL_SUFFIXES = (".v", ".sv", ".svh", ".vh")


# ----------------------------------------------------------------------------
# Records
# ----------------------------------------------------------------------------

@dataclass
class Port:
    name: str
    direction: str = ""
    width: str = ""
    kind: str = ""


@dataclass
class SignalDecl:
    name: str
    kind: str       # wire / reg / logic / int / ...
    line: int
    width: str = ""
    decl_text: str = ""


@dataclass
class Instance:
    instance_name: str
    module_name: str
    line: int


@dataclass
class ParsedModule:
    name: str
    kind: str
    file: str
    line_start: int
    line_end: int
    parameters: list[str] = field(default_factory=list)
    ports: list[Port] = field(default_factory=list)
    signals: list[SignalDecl] = field(default_factory=list)
    instances: list[Instance] = field(default_factory=list)
    leading_comment: str = ""
    body_summary: str = ""
    ast: dict = field(default_factory=dict)


# ----------------------------------------------------------------------------
# Driver
# ----------------------------------------------------------------------------

def have_sv_parse(binary: str = DEFAULT_SV_PARSE) -> bool:
    return os.path.isfile(binary) and os.access(binary, os.X_OK)


def load_ast(
    path: str | Path,
    binary: str = DEFAULT_SV_PARSE,
    include_dirs: list[str] | None = None,
    defines: list[tuple[str, str]] | None = None,
) -> dict[str, Any]:
    """Run `sv-parse --dump-json` and return the parsed envelope."""
    cmd = [binary, "--dump-json"]
    for d in include_dirs or []:
        cmd.extend(["-I", d])
    for k, v in defines or []:
        cmd.extend(["-D", f"{k}={v}"])
    cmd.append(str(path))
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if not proc.stdout.strip():
        raise RuntimeError(f"sv-parse produced no output for {path}: {proc.stderr.strip()}")
    return json.loads(proc.stdout)


# ----------------------------------------------------------------------------
# AST helpers (mirror xezim_parser/ast_loader.py from trudbg)
# ----------------------------------------------------------------------------

def _line_starts(src: str) -> list[int]:
    starts = [0]
    for i, ch in enumerate(src):
        if ch == "\n":
            starts.append(i + 1)
    return starts


def _offset_to_line(starts: list[int], offset: int) -> int:
    lo, hi = 0, len(starts) - 1
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if starts[mid] <= offset:
            lo = mid
        else:
            hi = mid - 1
    return lo + 1


def _iter_module_decls(ast: dict) -> Iterator[tuple[str, dict]]:
    for desc in ast.get("descriptions") or []:
        if not isinstance(desc, dict):
            continue
        for kind in ("Module", "Interface", "Program", "Package"):
            if kind in desc:
                yield kind, desc[kind]
                break


def _data_type_kind(dt: dict | None) -> str:
    if not isinstance(dt, dict):
        return ""
    for k in ("IntegerVector", "IntegerAtom", "NonIntegerType"):
        if k in dt and isinstance(dt[k], dict):
            return dt[k].get("kind", "").lower()
    if "Implicit" in dt:
        return ""
    keys = list(dt.keys())
    return keys[0].lower() if keys else ""


def _data_type_dims(dt: dict | None) -> list[dict]:
    if not isinstance(dt, dict):
        return []
    for k in ("IntegerVector", "IntegerAtom", "NonIntegerType"):
        if k in dt and isinstance(dt[k], dict):
            return dt[k].get("dimensions") or []
    return []


def _render_expr(e: dict | None) -> str:
    if not isinstance(e, dict):
        return "?"
    k = e.get("kind")
    if not isinstance(k, dict):
        return "?"
    if "Ident" in k:
        path = k["Ident"].get("path", [])
        if path and isinstance(path[0], dict):
            return path[0].get("name", {}).get("name", "?")
    if "Number" in k:
        n = k["Number"]
        if isinstance(n, dict):
            if "Integer" in n:
                return str(n["Integer"].get("value", "?"))
            if "Real" in n:
                return str(n["Real"].get("value", "?"))
        return str(n)
    if "Binary" in k:
        b = k["Binary"]
        op_map = {"Add": "+", "Sub": "-", "Mul": "*", "Div": "/", "Mod": "%"}
        op = op_map.get(b.get("op"), str(b.get("op", "?")))
        return f"{_render_expr(b.get('left'))}{op}{_render_expr(b.get('right'))}"
    if "Unary" in k:
        u = k["Unary"]
        return f"{u.get('op', '?')}{_render_expr(u.get('expr'))}"
    return "?"


def _render_dims(dims: list[dict] | None) -> str:
    if not dims:
        return ""
    parts: list[str] = []
    for d in dims:
        if "Range" in d:
            r = d["Range"]
            parts.append(f"[{_render_expr(r['left'])}:{_render_expr(r['right'])}]")
        elif "Sized" in d:
            parts.append(f"[{_render_expr(d['Sized'])}]")
        else:
            parts.append("[?]")
    return "".join(parts)


def _extract_ports(port_list: Any) -> list[Port]:
    if not isinstance(port_list, dict):
        return []
    if "Ansi" not in port_list:
        if "NonAnsi" in port_list:
            return [Port(name=(p or {}).get("name", "?")) for p in port_list["NonAnsi"]
                    if isinstance(p, dict)]
        return []
    ports: list[Port] = []
    for ap in port_list["Ansi"]:
        if not isinstance(ap, dict):
            continue
        name_node = ap.get("name") or {}
        name = name_node.get("name", "")
        direction = (ap.get("direction") or "").lower()
        dt = ap.get("data_type")
        kind = _data_type_kind(dt)
        width = _render_dims(_data_type_dims(dt))
        ports.append(Port(name=name, direction=direction, width=width, kind=kind))
    return ports


def _extract_params(decl: dict) -> list[str]:
    out: list[str] = []
    for p in decl.get("params", []) or []:
        if not isinstance(p, dict):
            continue
        kind = p.get("kind")
        if isinstance(kind, dict):
            for variant in ("Data", "Type"):
                if variant in kind and isinstance(kind[variant], dict):
                    for a in kind[variant].get("assignments", []) or []:
                        if isinstance(a, dict) and isinstance(a.get("name"), dict):
                            nm = a["name"].get("name")
                            if nm:
                                out.append(nm)
                    break
    return out


# ----------------------------------------------------------------------------
# Walk the module body for signal decls + child instantiations.
# These types vary by parse variant so we fall back to recursive key scans.
# ----------------------------------------------------------------------------

def _walk_items(items: list, starts: list[int], src: str
                ) -> tuple[list[SignalDecl], list[Instance], dict]:
    signals: list[SignalDecl] = []
    instances: list[Instance] = []
    counts = {"always": 0, "assign": 0}

    def visit(node):
        if isinstance(node, dict):
            for k, v in node.items():
                kl = k.lower()
                if k.startswith("Always") or kl.startswith("always"):
                    counts["always"] += 1
                elif k in ("ContinuousAssign", "Assign"):
                    counts["assign"] += 1
                # Signal declarations (NetDeclaration / VariableDeclaration / DataDeclaration)
                if k in ("NetDeclaration", "VariableDeclaration", "DataDeclaration"):
                    if isinstance(v, dict):
                        decls = v.get("decls") or v.get("declarators") or []
                        dt = v.get("data_type") or v.get("net_type") or v.get("type")
                        kind = _data_type_kind(dt) or k.lower().replace("declaration", "")
                        width = _render_dims(_data_type_dims(dt))
                        span = v.get("span") or {}
                        line = _offset_to_line(starts, int(span.get("start", 0))) if span else 0
                        for d in decls:
                            if not isinstance(d, dict):
                                continue
                            nm_node = d.get("name") or {}
                            if isinstance(nm_node, dict):
                                nm = nm_node.get("name", "")
                            else:
                                nm = str(nm_node)
                            if not nm:
                                continue
                            d_span = d.get("span") or span
                            d_line = _offset_to_line(starts, int(d_span.get("start", 0))) \
                                     if d_span else line
                            decl_text = _slice_line(src, starts, d_line)
                            signals.append(SignalDecl(
                                name=nm, kind=kind, line=d_line, width=width,
                                decl_text=decl_text,
                            ))
                # Module instantiation
                if k in ("ModuleInstantiation", "Instantiation"):
                    if isinstance(v, dict):
                        mod_name_node = v.get("module") or v.get("module_name") or {}
                        if isinstance(mod_name_node, dict):
                            mod_name = mod_name_node.get("name", "")
                        else:
                            mod_name = str(mod_name_node)
                        for inst in v.get("instances") or v.get("hierarchical_instances") or []:
                            if isinstance(inst, dict):
                                in_node = inst.get("name") or {}
                                if isinstance(in_node, dict):
                                    in_name = in_node.get("name", "")
                                else:
                                    in_name = str(in_node)
                                span = inst.get("span") or v.get("span") or {}
                                line = _offset_to_line(starts, int(span.get("start", 0))) \
                                       if span else 0
                                if mod_name and in_name:
                                    instances.append(Instance(
                                        instance_name=in_name,
                                        module_name=mod_name,
                                        line=line,
                                    ))
                visit(v)
        elif isinstance(node, list):
            for x in node:
                visit(x)

    visit(items)
    return signals, instances, counts


def _slice_line(src: str, starts: list[int], line: int) -> str:
    """Return the text of a single line (1-based)."""
    if line < 1 or line > len(starts):
        return ""
    start = starts[line - 1]
    end = starts[line] if line < len(starts) else len(src)
    return src[start:end].rstrip("\n")


_COMMENT_RE = None
def _leading_comment(raw_src: str, module_offset: int) -> str:
    """Walk backwards from module_offset to collect contiguous comments."""
    i = module_offset
    while i > 0 and raw_src[i - 1] in " \t\r\n":
        i -= 1
    end = i
    found = False
    while i > 0:
        if i >= 2 and raw_src[i - 2:i] == "*/":
            start = raw_src.rfind("/*", 0, i - 2)
            if start == -1:
                break
            i = start
            found = True
        else:
            line_start = raw_src.rfind("\n", 0, i) + 1
            line = raw_src[line_start:i]
            if line.lstrip().startswith("//"):
                i = line_start
                found = True
            else:
                break
        while i > 0 and raw_src[i - 1] in " \t\r\n":
            i -= 1
    return raw_src[i:end].strip() if found else ""


def _find_module_offset(raw_src: str, kw: str, name: str) -> int:
    import re
    pat = re.compile(rf"\b{re.escape(kw)}\s+{re.escape(name)}\b")
    m = pat.search(raw_src)
    return m.start() if m else -1


# ----------------------------------------------------------------------------
# Public entry
# ----------------------------------------------------------------------------

def parse_sv_file(
    path: str | Path,
    binary: str = DEFAULT_SV_PARSE,
) -> tuple[list[ParsedModule], str]:
    """Parse one .v/.sv file. Returns (modules, raw_source_text)."""
    p = Path(path)
    envelope = load_ast(p, binary=binary)
    preprocessed_src: str = envelope.get("source_text", "")
    ast = envelope.get("ast") or {}
    starts_pp = _line_starts(preprocessed_src)
    raw_src = p.read_text(encoding="utf-8", errors="replace")

    modules: list[ParsedModule] = []
    for kind, decl in _iter_module_decls(ast):
        if not isinstance(decl, dict):
            continue
        name = (decl.get("name") or {}).get("name", "<anon>")
        span = decl.get("span") or {}
        start_off = int(span.get("start", 0))
        end_off = int(span.get("end", start_off))
        line_start = _offset_to_line(starts_pp, start_off)
        line_end = _offset_to_line(starts_pp, max(end_off - 1, start_off))

        ports = _extract_ports(decl.get("ports"))
        params = _extract_params(decl)
        signals, instances, counts = _walk_items(
            decl.get("items") or [], starts_pp, preprocessed_src,
        )

        raw_off = _find_module_offset(raw_src, kind.lower(), name)
        leading = _leading_comment(raw_src, raw_off) if raw_off >= 0 else ""

        summary = (
            f"kind={kind.lower()} ports={len(ports)} params={len(params)} "
            f"always={counts['always']} assign={counts['assign']} "
            f"signals={len(signals)} instances={len(instances)}"
        )
        modules.append(ParsedModule(
            name=name,
            kind=kind.lower(),
            file=str(p),
            line_start=line_start,
            line_end=line_end,
            parameters=params,
            ports=ports,
            signals=signals,
            instances=instances,
            leading_comment=leading,
            body_summary=summary,
            ast=decl,
        ))
    return modules, raw_src


def walk_rtl(root: str | Path) -> Iterator[tuple[Path, list[ParsedModule], str]]:
    """Yield (path, modules, raw_src) for every .v/.sv file under `root`."""
    rp = Path(root)
    if rp.is_file():
        if rp.suffix.lower() in _RTL_SUFFIXES:
            mods, raw = parse_sv_file(rp)
            yield rp, mods, raw
        return
    for p in sorted(rp.rglob("*")):
        if p.is_file() and p.suffix.lower() in _RTL_SUFFIXES:
            try:
                mods, raw = parse_sv_file(p)
                yield p, mods, raw
            except Exception as e:
                import sys
                print(f"warn: parse failed for {p}: {e}", file=sys.stderr)
