"""FST waveform input — convert an `.fst` dump to VCD, then reuse the parser.

FST (the compressed waveform format from GTKWave / Verilator) is a binary
container; rather than reimplement its LZ4/zlib block format, xevdb shells out to
``fst2vcd`` (ships with Icarus Verilog and GTKWave) to produce a VCD, which the
existing parser already handles. The converter is configurable via
``$XEVDB_FST2VCD`` and surfaces a clear message when the tool is absent.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

DEFAULT_FST2VCD = "fst2vcd"


def fst2vcd_bin() -> str | None:
    return os.environ.get("XEVDB_FST2VCD") or shutil.which(DEFAULT_FST2VCD)


def have_fst2vcd() -> bool:
    return fst2vcd_bin() is not None


def convert_to_vcd(fst_path: str | Path, out_path: str | Path) -> Path:
    """Convert `fst_path` to a VCD at `out_path` via fst2vcd. Returns out_path."""
    bin_ = fst2vcd_bin()
    if not bin_:
        raise FileNotFoundError(
            "fst2vcd not found — install Icarus Verilog or GTKWave (both ship it), "
            "or set $XEVDB_FST2VCD to its path.")
    out = Path(out_path)
    proc = subprocess.run(
        [bin_, str(fst_path), "-o", str(out)],
        capture_output=True, text=True,
    )
    if proc.returncode != 0 or not out.is_file() or out.stat().st_size == 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise RuntimeError(f"fst2vcd failed on {fst_path}: {detail or 'no output'}")
    return out
