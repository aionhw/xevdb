from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from models import DebugEvent, DebugSession

META_KEYS = {"__TRIGGER", "__SAMPLE_INDEX", "__WINDOW_INDEX", "__WINDOW_SAMPLE_INDEX", "__GAP"}


class XevdbStore:
    """Small JSON state store for dashboard/session metadata.

    Waveform data lives in the real `.xevdb` dataset built from XTrace. This
    file only keeps app-level sessions, decoded protocol events, and compact
    sample rows used by the demo dashboard.
    """

    def __init__(self, path: str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.data = self._load()

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"sessions": {}, "samples": [], "events": []}
        return json.loads(self.path.read_text(encoding="utf-8"))

    def _save(self) -> None:
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(self.data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        tmp.replace(self.path)

    def close(self) -> None:
        self._save()

    def upsert_session(self, session: DebugSession) -> None:
        existing = self.data["sessions"].get(session.session_id, {})
        created_at = existing.get("created_at") or session.created_at or datetime.now(timezone.utc).isoformat()
        self.data["sessions"][session.session_id] = {
            "session_id": session.session_id,
            "project": session.project,
            "board": session.board,
            "source": session.source,
            "created_at": created_at,
        }
        self._save()

    def clear_capture(self, session_id: str) -> None:
        self.data["samples"] = [
            r for r in self.data["samples"] if r.get("session_id") != session_id
        ]
        self.data["events"] = [
            r for r in self.data["events"] if r.get("session_id") != session_id
        ]
        self._save()

    def ingest_samples(self, session_id: str, chipscopy_data: Dict[str, List[int]]) -> int:
        sample_index = chipscopy_data.get("__SAMPLE_INDEX") or list(
            range(len(next(iter(chipscopy_data.values()))))
        )
        trigger = chipscopy_data.get("__TRIGGER") or [0] * len(sample_index)
        window_index = chipscopy_data.get("__WINDOW_INDEX") or [0] * len(sample_index)
        gap = chipscopy_data.get("__GAP") or [0] * len(sample_index)

        rows: list[dict[str, Any]] = []
        for signal, values in chipscopy_data.items():
            if signal in META_KEYS:
                continue
            for i, value in enumerate(values):
                rows.append({
                    "session_id": session_id,
                    "sample_index": int(sample_index[i]),
                    "signal": signal,
                    "value": int(value),
                    "trigger": int(trigger[i]),
                    "window_index": int(window_index[i]),
                    "gap": int(gap[i]),
                })
        self.data["samples"].extend(rows)
        self._save()
        return len(rows)

    def ingest_events(self, events: List[DebugEvent]) -> int:
        rows = [e.to_dict() for e in events]
        self.data["events"].extend(rows)
        self._save()
        return len(rows)

    def list_sessions(self) -> List[Dict[str, Any]]:
        return sorted(
            self.data["sessions"].values(),
            key=lambda r: r.get("created_at", ""),
            reverse=True,
        )

    def list_signals(self, session_id: str) -> List[str]:
        return sorted({
            r["signal"] for r in self.data["samples"]
            if r.get("session_id") == session_id
        })

    def get_events(self, session_id: str) -> List[Dict[str, Any]]:
        return sorted(
            [dict(r) for r in self.data["events"] if r.get("session_id") == session_id],
            key=lambda r: int(r.get("start_sample", 0)),
        )

    def get_window(
        self,
        session_id: str,
        start: int,
        end: int,
        signals: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        wanted = set(signals or [])
        rows = []
        for r in self.data["samples"]:
            if r.get("session_id") != session_id:
                continue
            sample = int(r["sample_index"])
            if sample < start or sample > end:
                continue
            if wanted and r["signal"] not in wanted:
                continue
            rows.append({
                "sample_index": sample,
                "signal": r["signal"],
                "value": r["value"],
                "trigger": r.get("trigger", 0),
            })
        return sorted(rows, key=lambda r: (r["sample_index"], r["signal"]))

    def summarize(self, session_id: str) -> Dict[str, Any]:
        session = self.data["sessions"].get(session_id)
        samples = [r for r in self.data["samples"] if r.get("session_id") == session_id]
        events = self.get_events(session_id)
        return {
            "session": dict(session) if session else None,
            "sample_count": len({r["sample_index"] for r in samples}),
            "signal_count": len({r["signal"] for r in samples}),
            "event_count": len(events),
            "critical_events": [e for e in events if e["severity"] == "critical"],
            "first_event": events[0] if events else None,
        }
