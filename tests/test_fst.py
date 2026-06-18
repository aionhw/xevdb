"""FST input — converter error handling + (if tools present) a round-trip."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from xevdb import fst as _fst
from xevdb.parser import parse_file


def test_missing_tool_raises(monkeypatch):
    monkeypatch.setattr(_fst, "fst2vcd_bin", lambda: None)
    with pytest.raises(FileNotFoundError, match="fst2vcd not found"):
        _fst.convert_to_vcd("x.fst", "x.vcd")


def test_env_override(monkeypatch):
    monkeypatch.setenv("XEVDB_FST2VCD", "/opt/custom/fst2vcd")
    assert _fst.fst2vcd_bin() == "/opt/custom/fst2vcd"


def _vcd(path: Path):
    path.write_text(
        "$timescale 1ns $end\n$scope module top $end\n"
        "$var wire 8 ! sig [7:0] $end\n$upscope $end\n$enddefinitions $end\n"
        "#0\nb00000000 !\n#10\nb00000001 !\n")


@pytest.mark.skipif(not (_fst.have_fst2vcd() and shutil.which("vcd2fst")),
                    reason="fst2vcd / vcd2fst not installed")
def test_roundtrip(tmp_path):
    vcd = tmp_path / "in.vcd"
    _vcd(vcd)
    fst = tmp_path / "in.fst"
    subprocess.run(["vcd2fst", str(vcd), str(fst)], check=True,
                   capture_output=True)
    out = _fst.convert_to_vcd(fst, tmp_path / "out.vcd")
    parsed = parse_file(out)
    assert len(parsed.signals) == 1
    assert any(s.name.startswith("sig") for s in parsed.signals.values())
