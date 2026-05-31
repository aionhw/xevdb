#!/usr/bin/env python3
"""Generate synthetic ChipScoPy-shaped ILA captures for the debug pipeline.

Each scenario returns the same dict shape ChipScoPy's
`ila.waveform.get_data(include_trigger=True, include_sample_info=True,
include_gap=True)` produces: per-signal integer lists plus the `__`-prefixed
meta channels (`__SAMPLE_INDEX`, `__TRIGGER`, `__WINDOW_INDEX`,
`__WINDOW_SAMPLE_INDEX`, `__GAP`).

The scenarios exercise the failure modes the protocol agent detects
(valid/ready stalls on AW/W/B/AR/R and AXIS backpressure), plus a clean control
and a multi-window capture.

    python scripts/generate_synthetic_capture.py --list
    python scripts/generate_synthetic_capture.py --scenario axi_read_hang --out cap.json
    python scripts/generate_synthetic_capture.py --all examples/scenarios
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Callable, Dict, List

Capture = Dict[str, List[int]]

# Common AXI bus constants used across scenarios (kept readable, not random).
ADDR = 0x4000_1000
WDATA = 0xDEAD_BEEF
RDATA = 0xCAFE_F00D


def _base(samples: int) -> Capture:
    idx = list(range(samples))
    return {
        "__SAMPLE_INDEX": idx,
        "__TRIGGER": [0] * samples,
        "__WINDOW_INDEX": [0] * samples,
        "__WINDOW_SAMPLE_INDEX": list(idx),
        "__GAP": [0] * samples,
    }


def _z(samples: int, fill: int = 0) -> List[int]:
    return [fill] * samples


def _hold(seq: List[int], start: int, end: int, value: int) -> None:
    for i in range(max(0, start), min(len(seq), end)):
        seq[i] = value


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------

def axi_write_hang(samples: int = 256) -> Capture:
    """AXI write address (AW) channel stalls: awvalid high, awready low ~48 cyc.

    A short AXIS backpressure precedes it; the W channel partially stalls too.
    """
    cap = _base(samples)
    stall_start, stall_cycles = 96, 48
    cap["__TRIGGER"][stall_start] = 1

    awvalid, awready, awaddr = _z(samples), _z(samples), _z(samples)
    wvalid, wready, wdata = _z(samples), _z(samples), _z(samples)
    bvalid, bready = _z(samples), _z(samples, 1)
    tvalid, tready = _z(samples), _z(samples, 1)

    _hold(awvalid, stall_start, stall_start + stall_cycles, 1)
    _hold(awaddr, stall_start, stall_start + stall_cycles, ADDR)
    rec = stall_start + stall_cycles            # handshake finally completes
    if rec < samples:
        awvalid[rec], awready[rec], awaddr[rec] = 1, 1, ADDR
    for i in range(stall_start + 8, min(samples, stall_start + stall_cycles + 4)):
        wvalid[i] = 1
        wready[i] = 0 if i < stall_start + stall_cycles - 4 else 1
        wdata[i] = WDATA
    if stall_start + stall_cycles + 8 < samples:
        bvalid[stall_start + stall_cycles + 8] = 1
    _hold(tvalid, 40, 57, 1)
    for i in range(43, 51):
        tready[i] = 0

    cap.update({
        "s_axi_awvalid": awvalid, "s_axi_awready": awready, "s_axi_awaddr": awaddr,
        "s_axi_wvalid": wvalid, "s_axi_wready": wready, "s_axi_wdata": wdata,
        "s_axi_bvalid": bvalid, "s_axi_bready": bready,
        "m_axis_tvalid": tvalid, "m_axis_tready": tready,
    })
    return cap


def axi_read_hang(samples: int = 256) -> Capture:
    """AXI read address (AR) channel stalls: arvalid high, arready low ~40 cyc."""
    cap = _base(samples)
    stall_start, stall_cycles = 80, 40
    cap["__TRIGGER"][stall_start] = 1

    arvalid, arready, araddr = _z(samples), _z(samples), _z(samples)
    rvalid, rready, rdata, rlast = _z(samples), _z(samples, 1), _z(samples), _z(samples)

    _hold(arvalid, stall_start, stall_start + stall_cycles, 1)
    _hold(araddr, stall_start, stall_start + stall_cycles, ADDR)
    rec = stall_start + stall_cycles
    if rec < samples:
        arvalid[rec], arready[rec], araddr[rec] = 1, 1, ADDR
        # read data returns a few cycles later
        for j in range(rec + 2, min(samples, rec + 6)):
            rvalid[j], rdata[j] = 1, RDATA
        last = min(samples - 1, rec + 5)
        rlast[last] = 1

    cap.update({
        "s_axi_arvalid": arvalid, "s_axi_arready": arready, "s_axi_araddr": araddr,
        "s_axi_rvalid": rvalid, "s_axi_rready": rready, "s_axi_rdata": rdata,
        "s_axi_rlast": rlast,
    })
    return cap


def axi_bresp_backpressure(samples: int = 256) -> Capture:
    """Write completes but the B (response) channel stalls: bvalid high, bready low.

    Master isn't accepting write responses, so outstanding writes pile up.
    """
    cap = _base(samples)
    cap["__TRIGGER"][64] = 1

    awvalid, awready, awaddr = _z(samples), _z(samples), _z(samples)
    wvalid, wready, wdata, wlast = _z(samples), _z(samples), _z(samples), _z(samples)
    bvalid, bready, bresp = _z(samples), _z(samples), _z(samples)

    # AW + W handshake cleanly around sample 64..72
    for i in range(64, 72):
        awvalid[i] = awready[i] = wvalid[i] = wready[i] = 1
        awaddr[i] = ADDR + (i - 64) * 4
        wdata[i] = WDATA
    wlast[71] = 1
    # B response asserted at 74 but master holds bready low for 36 cycles
    bstall_start, bstall_cycles = 74, 36
    _hold(bvalid, bstall_start, bstall_start + bstall_cycles, 1)   # bresp=OKAY(0)
    rec = bstall_start + bstall_cycles
    if rec < samples:
        bvalid[rec], bready[rec] = 1, 1

    cap.update({
        "s_axi_awvalid": awvalid, "s_axi_awready": awready, "s_axi_awaddr": awaddr,
        "s_axi_wvalid": wvalid, "s_axi_wready": wready, "s_axi_wdata": wdata,
        "s_axi_wlast": wlast,
        "s_axi_bvalid": bvalid, "s_axi_bready": bready, "s_axi_bresp": bresp,
    })
    return cap


def axis_overrun(samples: int = 256) -> Capture:
    """AXIS downstream backpressure: tvalid high, tready low for a long burst.

    A `fifo_full` flag is high through the stall — useful next-probe evidence.
    """
    cap = _base(samples)
    stall_start, stall_cycles = 70, 64
    cap["__TRIGGER"][stall_start] = 1

    tvalid, tready, tdata, tlast = _z(samples), _z(samples, 1), _z(samples), _z(samples)
    fifo_full, drop_count = _z(samples), _z(samples)

    _hold(tvalid, stall_start, stall_start + stall_cycles + 8, 1)
    for i in range(stall_start, stall_start + stall_cycles + 8):
        if i < samples:
            tdata[i] = (WDATA + i) & 0xFFFF_FFFF
    _hold(tready, stall_start, stall_start + stall_cycles, 0)       # downstream not ready
    _hold(fifo_full, stall_start, stall_start + stall_cycles, 1)
    # a few samples get dropped as the count climbs
    c = 0
    for i in range(stall_start, min(samples, stall_start + stall_cycles)):
        if i % 8 == 0:
            c += 1
        drop_count[i] = c
    end = min(samples - 1, stall_start + stall_cycles + 7)
    tlast[end] = 1

    cap.update({
        "m_axis_tvalid": tvalid, "m_axis_tready": tready, "m_axis_tdata": tdata,
        "m_axis_tlast": tlast, "fifo_full": fifo_full, "drop_count": drop_count,
    })
    return cap


def write_read_deadlock(samples: int = 256) -> Capture:
    """AW and AR stall simultaneously for a long time — mutual/arbiter deadlock."""
    cap = _base(samples)
    start, cycles = 60, 80
    cap["__TRIGGER"][start] = 1

    awvalid, awready, awaddr = _z(samples), _z(samples), _z(samples)
    arvalid, arready, araddr = _z(samples), _z(samples), _z(samples)
    grant = _z(samples)   # arbiter grant stuck low through the deadlock

    _hold(awvalid, start, start + cycles, 1); _hold(awaddr, start, start + cycles, ADDR)
    _hold(arvalid, start, start + cycles, 1); _hold(araddr, start, start + cycles, ADDR + 0x80)
    # never recovers within the window (open-ended stall = still stuck at capture end)

    cap.update({
        "s_axi_awvalid": awvalid, "s_axi_awready": awready, "s_axi_awaddr": awaddr,
        "s_axi_arvalid": arvalid, "s_axi_arready": arready, "s_axi_araddr": araddr,
        "arb_grant": grant,
    })
    return cap


def clean_pass(samples: int = 256) -> Capture:
    """Healthy capture: every valid/ready handshake completes promptly. No events."""
    cap = _base(samples)
    cap["__TRIGGER"][32] = 1

    awvalid, awready, awaddr = _z(samples), _z(samples), _z(samples)
    wvalid, wready, wdata = _z(samples), _z(samples), _z(samples)
    bvalid, bready = _z(samples), _z(samples, 1)
    tvalid, tready, tdata = _z(samples), _z(samples, 1), _z(samples)

    # single-cycle handshakes scattered through the trace (never valid&&!ready > 1)
    for k, i in enumerate(range(32, samples - 8, 16)):
        awvalid[i] = awready[i] = 1; awaddr[i] = ADDR + k * 4
        wvalid[i + 1] = wready[i + 1] = 1; wdata[i + 1] = WDATA
        bvalid[i + 3] = 1                      # bready already high
        tvalid[i + 2] = 1; tdata[i + 2] = RDATA  # tready already high

    cap.update({
        "s_axi_awvalid": awvalid, "s_axi_awready": awready, "s_axi_awaddr": awaddr,
        "s_axi_wvalid": wvalid, "s_axi_wready": wready, "s_axi_wdata": wdata,
        "s_axi_bvalid": bvalid, "s_axi_bready": bready,
        "m_axis_tvalid": tvalid, "m_axis_tready": tready, "m_axis_tdata": tdata,
    })
    return cap


def multi_window(samples_per_window: int = 96) -> Capture:
    """ChipScoPy multi-window capture (window_count=2) with a stall in window 1.

    Demonstrates the `__WINDOW_INDEX` / `__WINDOW_SAMPLE_INDEX` / `__GAP` meta
    channels: each window has its own trigger and per-window sample index, and a
    gap marker sits at the window boundary.
    """
    nwin = 2
    total = samples_per_window * nwin
    idx = list(range(total))
    cap: Capture = {
        "__SAMPLE_INDEX": idx,
        "__TRIGGER": [0] * total,
        "__WINDOW_INDEX": [w for w in range(nwin) for _ in range(samples_per_window)],
        "__WINDOW_SAMPLE_INDEX": list(range(samples_per_window)) * nwin,
        "__GAP": [0] * total,
    }
    # trigger at the start of each window; gap marker at the boundary
    cap["__TRIGGER"][0] = 1
    cap["__TRIGGER"][samples_per_window] = 1
    cap["__GAP"][samples_per_window] = 1

    awvalid, awready, awaddr = _z(total), _z(total), _z(total)
    wvalid, wready, wdata = _z(total), _z(total), _z(total)

    # window 0: clean single-cycle write
    awvalid[10] = awready[10] = 1; awaddr[10] = ADDR
    wvalid[11] = wready[11] = 1; wdata[11] = WDATA
    # window 1: AW stalls for 40 cycles
    base = samples_per_window
    s0, sc = base + 20, 40
    _hold(awvalid, s0, s0 + sc, 1); _hold(awaddr, s0, s0 + sc, ADDR + 0x10)
    rec = s0 + sc
    if rec < total:
        awvalid[rec] = awready[rec] = 1

    cap.update({
        "s_axi_awvalid": awvalid, "s_axi_awready": awready, "s_axi_awaddr": awaddr,
        "s_axi_wvalid": wvalid, "s_axi_wready": wready, "s_axi_wdata": wdata,
    })
    return cap


SCENARIOS: Dict[str, Callable[..., Capture]] = {
    "axi_write_hang": axi_write_hang,
    "axi_read_hang": axi_read_hang,
    "axi_bresp_backpressure": axi_bresp_backpressure,
    "axis_overrun": axis_overrun,
    "write_read_deadlock": write_read_deadlock,
    "clean_pass": clean_pass,
    "multi_window": multi_window,
}

_DESCRIPTIONS = {name: (fn.__doc__ or "").strip().splitlines()[0] for name, fn in SCENARIOS.items()}

# Back-compat: the original entry point.
make_capture = axi_write_hang


def _build(scenario: str, samples: int) -> Capture:
    fn = SCENARIOS[scenario]
    # multi_window takes per-window size; others take total samples.
    return fn(samples) if scenario != "multi_window" else fn(samples // 2 or 1)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", help="output JSON path (single scenario)")
    ap.add_argument("--scenario", default="axi_write_hang", choices=list(SCENARIOS),
                    help="which scenario to generate (default: axi_write_hang)")
    ap.add_argument("--samples", type=int, default=256)
    ap.add_argument("--all", metavar="DIR",
                    help="write every scenario to DIR/<scenario>.json")
    ap.add_argument("--list", action="store_true", help="list scenarios and exit")
    args = ap.parse_args()

    if args.list:
        width = max(len(n) for n in SCENARIOS)
        for name in SCENARIOS:
            print(f"{name:<{width}}  {_DESCRIPTIONS[name]}")
        return

    if args.all:
        out_dir = Path(args.all)
        out_dir.mkdir(parents=True, exist_ok=True)
        for name in SCENARIOS:
            path = out_dir / f"{name}.json"
            path.write_text(json.dumps(_build(name, args.samples), indent=2))
            print(f"wrote {path}")
        return

    if not args.out:
        ap.error("provide --out FILE, or --all DIR, or --list")
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(_build(args.scenario, args.samples), indent=2))
    print(f"wrote {args.out}  (scenario: {args.scenario})")


if __name__ == "__main__":
    main()
