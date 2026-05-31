from __future__ import annotations

from typing import Dict, List, Tuple
from models import DebugEvent

AXI_CHANNELS: Dict[str, Tuple[str, str]] = {
    "AW": ("awvalid", "awready"),
    "W": ("wvalid", "wready"),
    "B": ("bvalid", "bready"),
    "AR": ("arvalid", "arready"),
    "R": ("rvalid", "rready"),
    "AXIS": ("tvalid", "tready"),
}


def _find_signal(data: Dict[str, List[int]], suffix: str) -> str | None:
    # Match the channel token at a name boundary, so `arvalid` is not mistaken
    # for the R channel's `rvalid` (it ends with it) nor `awvalid` for `wvalid`.
    # AXI probes are named `<prefix>_<channel><valid|ready>`, so require the
    # token to be the whole name or follow an underscore.
    suffix = suffix.lower()
    matches = [
        k for k in data
        if not k.startswith("__")
        and (k.lower() == suffix or k.lower().endswith("_" + suffix))
    ]
    return matches[0] if matches else None


def _stall_ranges(valid: List[int], ready: List[int], min_cycles: int = 2) -> List[Tuple[int, int]]:
    ranges: List[Tuple[int, int]] = []
    start = None
    for i, (v, r) in enumerate(zip(valid, ready)):
        stalled = bool(v) and not bool(r)
        if stalled and start is None:
            start = i
        elif not stalled and start is not None:
            if i - start >= min_cycles:
                ranges.append((start, i - 1))
            start = None
    if start is not None and len(valid) - start >= min_cycles:
        ranges.append((start, len(valid) - 1))
    return ranges


def detect_axi_events(session_id: str, chipscopy_data: Dict[str, List[int]], interface: str = "s_axi", min_cycles: int = 2) -> List[DebugEvent]:
    sample_index = chipscopy_data.get("__SAMPLE_INDEX") or list(range(len(next(iter(chipscopy_data.values())))))
    events: List[DebugEvent] = []

    for channel, (valid_suffix, ready_suffix) in AXI_CHANNELS.items():
        valid_sig = _find_signal(chipscopy_data, valid_suffix)
        ready_sig = _find_signal(chipscopy_data, ready_suffix)
        if not valid_sig or not ready_sig:
            continue
        for start_i, end_i in _stall_ranges(chipscopy_data[valid_sig], chipscopy_data[ready_sig], min_cycles=min_cycles):
            cycles = end_i - start_i + 1
            severity = "critical" if cycles >= 32 else "warning" if cycles >= 8 else "info"
            events.append(
                DebugEvent(
                    session_id=session_id,
                    event_type="axi_stall" if channel != "AXIS" else "axis_backpressure",
                    interface=interface,
                    channel=channel,
                    start_sample=int(sample_index[start_i]),
                    end_sample=int(sample_index[end_i]),
                    cycles=cycles,
                    severity=severity,
                    reason=f"{valid_sig} held high while {ready_sig} was low for {cycles} cycles",
                    evidence={"valid_signal": valid_sig, "ready_signal": ready_sig},
                )
            )
    return events
