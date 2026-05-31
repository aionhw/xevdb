#!/usr/bin/env python3
"""Filter a VCD to the signals under one or more instance scopes.

Usage: vcd_scope_filter.py <in.vcd> <out.vcd> <inst1> [inst2 ...]

Keeps a $var iff its enclosing scope path contains one of the named instances
as a component (so the whole subtree under that instance is kept). Preserves
the $scope/$upscope nesting for kept signals and the value-change records for
kept VCD ids only. Pure-stdlib, single pass over the definitions + one over
the value section.
"""
import sys


def main() -> int:
    inp, outp, *insts = sys.argv[1:]
    insts = set(insts)
    keep_ids: set[str] = set()
    scope_path: list[str] = []
    out = []
    in_defs = True

    with open(inp, "r", errors="replace") as f:
        lines = f.readlines()

    # pass 1: header + definitions, decide kept ids, re-emit pruned scope tree
    i = 0
    n = len(lines)
    emitted_scope_depth = 0  # how many $scope we've written but not closed
    pending_scopes: list[str] = []  # scopes entered but not yet emitted

    def under_target() -> bool:
        return any(c in insts for c in scope_path)

    while i < n:
        line = lines[i]
        s = line.strip()
        if in_defs:
            if s.startswith("$scope"):
                parts = s.split()
                name = parts[2] if len(parts) >= 3 else "?"
                scope_path.append(name)
                pending_scopes.append(line)
            elif s.startswith("$upscope"):
                if scope_path:
                    scope_path.pop()
                if pending_scopes:
                    pending_scopes.pop()           # scope was never emitted
                else:
                    out.append(line)               # close an emitted scope
            elif s.startswith("$var"):
                if under_target():
                    # flush any pending (unemitted) ancestor scopes first
                    for ps in pending_scopes:
                        out.append(ps)
                    pending_scopes = []
                    out.append(line)
                    keep_ids.add(s.split()[3])     # $var <type> <w> <id> <name>
            elif s.startswith("$enddefinitions"):
                out.append(line)
                in_defs = False
            else:
                # date/version/timescale/comment header lines
                out.append(line)
        else:
            # value-change section
            if s.startswith("#") or s.startswith("$"):
                out.append(line)
            elif not s:
                pass
            else:
                c = s[0]
                if c in "01xXzZ":
                    if s[1:] in keep_ids:
                        out.append(line)
                elif c in "bBrR":
                    # b<bits> <id>  /  r<float> <id>
                    parts = s.split()
                    if len(parts) == 2 and parts[1] in keep_ids:
                        out.append(line)
        i += 1

    with open(outp, "w") as f:
        f.writelines(out)
    print(f"kept {len(keep_ids)} signals under {sorted(insts)} -> {outp}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
