from __future__ import annotations

from typing import Dict, List, Iterable
from models import DebugEvent

META_KEYS = {"__TRIGGER", "__SAMPLE_INDEX", "__WINDOW_INDEX", "__WINDOW_SAMPLE_INDEX", "__GAP"}


def infer_width(values: Iterable[int]) -> int:
    max_v = max([0, *[int(v) for v in values]])
    return max(1, max_v.bit_length())


def write_xtrace(chipscopy_data: Dict[str, List[int]], events: List[DebugEvent], source: str = "chipscopy", session_id: str = "session") -> str:
    signals = [k for k in chipscopy_data.keys() if k not in META_KEYS]
    sample_index = chipscopy_data.get("__SAMPLE_INDEX") or list(range(len(next(iter(chipscopy_data.values())))))
    trigger = chipscopy_data.get("__TRIGGER") or [0] * len(sample_index)

    lines: List[str] = []
    lines.append("xtrace.version 1.0")
    lines.append(f"session {session_id}")
    lines.append(f"source {source}")
    lines.append("timescale sample")
    lines.append("")

    for sig in signals:
        width = infer_width(chipscopy_data[sig])
        lines.append(f"signal {sig} width={width}")
    lines.append("")

    for i, sample in enumerate(sample_index):
        parts = [f"@{sample}"]
        if trigger[i]:
            parts.append("trigger=1")
        for sig in signals:
            val = chipscopy_data[sig][i]
            width = infer_width(chipscopy_data[sig])
            if width > 4:
                parts.append(f"{sig}=0x{int(val):X}")
            else:
                parts.append(f"{sig}={int(val)}")
        lines.append(" ".join(parts))

    if events:
        lines.append("")
        for e in events:
            lines.append(
                f"event {e.event_type} interface={e.interface} channel={e.channel} "
                f"start={e.start_sample} end={e.end_sample} cycles={e.cycles} "
                f"severity={e.severity} reason=\"{e.reason}\""
            )
    lines.append("")
    return "\n".join(lines)
