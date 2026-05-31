from __future__ import annotations

from typing import Any, Dict, List

from connectors import BaseDebugConnector, select_connector
from storage import XevdbStore


class DebugOrchestrator:
    def __init__(self, store: XevdbStore, model_connector: BaseDebugConnector | None = None):
        self.store = store
        self.model = model_connector or select_connector()

    def answer(self, session_id: str, question: str) -> Dict[str, Any]:
        summary = self.store.summarize(session_id)
        events = self.store.get_events(session_id)
        window = []
        if events:
            first = events[0]
            start = max(0, int(first["start_sample"]) - 16)
            end = int(first["end_sample"]) + 16
            signals = list((first.get("evidence") or {}).values())
            signals = [s for s in signals if isinstance(s, str)]
            window = self.store.get_window(session_id, start, end, signals=signals or None)
        context = self.model.build_context(question=question, summary=summary, events=events, window=window)
        answer = self.model.ask(context)
        return {"answer": answer, "context": context}
