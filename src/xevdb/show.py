"""Show RTL source code for signals or modules, slicing it from the .xevdb file.

`show_code` is the workhorse. Given a query (signal name, module name, or
`file:line`), it figures out which source file + line range to display and
returns the slice. All data comes from the SQLite tables — no need for the
original .sv files to be present.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any


@dataclass
class CodeSlice:
    """One contiguous source slice with metadata for display."""
    file: str
    line_start: int                # 1-based, inclusive
    line_end: int                  # 1-based, inclusive
    text: str
    target_line: int | None = None # the line the user actually asked about
    module_name: str | None = None
    description: str = ""          # one-line summary of why this slice was selected


def _file_content(con: sqlite3.Connection, path: str) -> str | None:
    row = con.execute(
        "SELECT content FROM source_files WHERE path = ?", (path,)
    ).fetchone()
    return row["content"] if row else None


def _slice_lines(text: str, start: int, end: int) -> str:
    """Slice `text` to lines [start..end] inclusive (1-based)."""
    lines = text.splitlines(keepends=True)
    s = max(0, start - 1)
    e = min(len(lines), end)
    return "".join(lines[s:e])


def by_file_line(
    con: sqlite3.Connection,
    file: str,
    line: int,
    context: int = 6,
) -> CodeSlice | None:
    """Return a [line-context, line+context] slice of `file`."""
    content = _file_content(con, file)
    if content is None:
        return None
    total = content.count("\n") + 1
    start = max(1, line - context)
    end = min(total, line + context)
    return CodeSlice(
        file=file, line_start=start, line_end=end,
        text=_slice_lines(content, start, end),
        target_line=line,
        description=f"{file}:{line} (±{context} lines)",
    )


def of_module(
    con: sqlite3.Connection,
    name: str,
    full: bool = False,
    context: int = 6,
) -> list[CodeSlice]:
    """Return code slices for every module named `name`.

    If `full=True` the whole module body is returned; otherwise just the
    header (declaration line ±context).
    """
    rows = con.execute(
        "SELECT id, name, file, line_start, line_end, leading_comment, body_summary "
        "FROM modules WHERE name = ? ORDER BY file, line_start",
        (name,),
    ).fetchall()
    out: list[CodeSlice] = []
    for row in rows:
        content = _file_content(con, row["file"])
        if content is None:
            continue
        if full:
            start, end = row["line_start"], row["line_end"]
        else:
            total = content.count("\n") + 1
            start = max(1, row["line_start"] - context)
            end = min(total, row["line_start"] + context)
        out.append(CodeSlice(
            file=row["file"], line_start=start, line_end=end,
            text=_slice_lines(content, start, end),
            target_line=row["line_start"],
            module_name=row["name"],
            description=(
                f"module {row['name']} @ {row['file']}:{row['line_start']}"
                f"-{row['line_end']}  ({row['body_summary']})"
            ),
        ))
    return out


def of_signal(
    con: sqlite3.Connection,
    query: str,
    context: int = 6,
) -> list[CodeSlice]:
    """Return code slices for every `module_signals` row whose name matches.

    Resolution: exact module_signals.name → bare-name match against a VCD
    `signals.fullname` (so callers can pass a hierarchical waveform path).
    """
    # 1) Direct match against an internal RTL declaration.
    rows = con.execute(
        "SELECT m.name AS mod_name, m.file AS mod_file, "
        "       ms.name AS sig_name, ms.line AS sig_line, ms.kind AS sig_kind, "
        "       ms.width AS sig_width, ms.decl_text AS decl_text "
        "FROM module_signals ms JOIN modules m ON m.id = ms.module_id "
        "WHERE ms.name = ?",
        (query,),
    ).fetchall()

    # 2) If nothing matched and the user passed a VCD hierarchy like
    #    `top.uut.core.reg_pc`, try the bare name.
    if not rows and "." in query:
        bare = query.rsplit(".", 1)[-1]
        rows = con.execute(
            "SELECT m.name AS mod_name, m.file AS mod_file, "
            "       ms.name AS sig_name, ms.line AS sig_line, ms.kind AS sig_kind, "
            "       ms.width AS sig_width, ms.decl_text AS decl_text "
            "FROM module_signals ms JOIN modules m ON m.id = ms.module_id "
            "WHERE ms.name = ?",
            (bare,),
        ).fetchall()

    # 3) If still nothing, also check ports.
    if not rows:
        bare = query.rsplit(".", 1)[-1]
        port_rows = con.execute(
            "SELECT m.name AS mod_name, m.file AS mod_file, m.line_start AS sig_line, "
            "       p.name AS sig_name, p.direction AS sig_kind, p.width AS sig_width, "
            "       '(port)' AS decl_text "
            "FROM module_ports p JOIN modules m ON m.id = p.module_id "
            "WHERE p.name = ?",
            (bare,),
        ).fetchall()
        rows = port_rows

    out: list[CodeSlice] = []
    for row in rows:
        content = _file_content(con, row["mod_file"])
        if content is None:
            continue
        total = content.count("\n") + 1
        line = row["sig_line"]
        start = max(1, line - context)
        end = min(total, line + context)
        out.append(CodeSlice(
            file=row["mod_file"], line_start=start, line_end=end,
            text=_slice_lines(content, start, end),
            target_line=line,
            module_name=row["mod_name"],
            description=(
                f"{row['sig_kind']} {row['sig_width']} {row['sig_name']} "
                f"in module {row['mod_name']} @ {row['mod_file']}:{line}"
            ),
        ))
    return out


def show_code(
    con: sqlite3.Connection,
    target: str,
    context: int = 6,
    full: bool = False,
) -> list[CodeSlice]:
    """Top-level entry. Dispatches on the shape of `target`:

      - `file.sv:NNN`               → file/line slice
      - `<module_name>`             → module declaration slice(s)
      - `<signal_or_port_name>`     → declaration slice(s) inside the owning module
      - `top.h.path.signal`         → bare-name match against module_signals/module_ports
    """
    # file:line form
    if ":" in target and target.rsplit(":", 1)[1].isdigit():
        file, line_s = target.rsplit(":", 1)
        sl = by_file_line(con, file, int(line_s), context=context)
        return [sl] if sl else []

    # Try module first (more specific), then signal, then bare-name signal.
    mods = of_module(con, target, full=full, context=context)
    if mods:
        return mods
    return of_signal(con, target, context=context)


# ----------------------------------------------------------------------------
# Pretty-printer
# ----------------------------------------------------------------------------

def render(slice_: CodeSlice, with_lines: bool = True) -> str:
    """Render a CodeSlice as a string for terminal display."""
    head = f"== {slice_.description} =="
    if not with_lines:
        return f"{head}\n{slice_.text}"
    out: list[str] = [head]
    lineno = slice_.line_start
    for ln in slice_.text.splitlines():
        marker = ">>" if slice_.target_line is not None and lineno == slice_.target_line else "  "
        out.append(f"{marker} {lineno:5d}  {ln}")
        lineno += 1
    return "\n".join(out)
