from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any, Dict, List, Optional


@dataclass
class DebugSession:
    session_id: str
    project: str
    board: str
    source: str = "chipscopy.synthetic"
    created_at: Optional[str] = None


@dataclass
class SignalSample:
    session_id: str
    sample_index: int
    signal: str
    value: int
    trigger: int = 0
    window_index: int = 0
    gap: int = 0


@dataclass
class DebugEvent:
    session_id: str
    event_type: str
    interface: str
    channel: str
    start_sample: int
    end_sample: int
    cycles: int
    severity: str
    reason: str
    evidence: Dict[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)
