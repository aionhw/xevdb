from __future__ import annotations

from typing import Any, Dict, List


class BaseDebugConnector:
    """Shared evidence-building + deterministic fallback for model connectors.

    Concrete connectors (Codex, Claude) only implement how to call their CLI
    (`_ask_model`) and when it is `enabled`. Everything else — the compact
    context sent to the model and the deterministic local answer used when no
    model is available — is identical across backends and lives here.

    A bare `BaseDebugConnector()` is itself a valid connector: `enabled` is
    False, so `ask()` always returns the deterministic fallback (useful as the
    "no model" / offline mode).
    """

    model_name = "model"

    @property
    def enabled(self) -> bool:
        return False

    def build_context(
        self,
        question: str,
        summary: Dict[str, Any],
        events: List[Dict[str, Any]],
        window: List[Dict[str, Any]] | None = None,
    ) -> Dict[str, Any]:
        return {
            "role": "fpga_debug_agent",
            "instruction": "Answer using only the provided xevdb evidence. Do not invent signal behavior.",
            "question": question,
            "summary": summary,
            "events": events[:20],
            "window": window[:200] if window else [],
        }

    def ask(self, context: Dict[str, Any]) -> str:
        if self.enabled:
            try:
                return self._ask_model(context)
            except (OSError, RuntimeError, TimeoutError) as e:
                return self._fallback_answer(context, note=f"{self.model_name} CLI unavailable: {e}")
        return self._fallback_answer(context)

    def _ask_model(self, context: Dict[str, Any]) -> str:  # pragma: no cover - abstract
        raise NotImplementedError

    @staticmethod
    def _prompt_text(context: Dict[str, Any]) -> str:
        import json

        return (
            "You are an FPGA protocol debug assistant. Answer using only the "
            "provided xevdb evidence. Do not invent signal behavior. Keep the "
            "answer concise and include next probes/triggers.\n\n"
            f"{json.dumps(context, indent=2)}"
        )

    def _fallback_answer(self, context: Dict[str, Any], note: str | None = None) -> str:
        # Deterministic local fallback that mimics the answer shape.
        events = context.get("events") or []
        summary = context.get("summary") or {}
        if not events:
            base = "No protocol events were detected in this session. Suggested next step: widen the capture window or add valid/ready probes."
            return f"{base}\n\n{note}" if note else base

        first = events[0]
        critical = summary.get("critical_events") or []
        lines = [
            "Debug hypothesis from xevdb evidence:",
            f"- First event: {first.get('event_type')} on {first.get('interface')} {first.get('channel')} channel.",
            f"- Location: samples {first.get('start_sample')} to {first.get('end_sample')} ({first.get('cycles')} cycles).",
            f"- Evidence: {first.get('reason')}.",
        ]
        if critical:
            lines.append(f"- Critical events: {len(critical)} event(s), including {critical[0].get('channel')} stall for {critical[0].get('cycles')} cycles.")
        evidence = first.get("evidence") or {}
        valid = evidence.get("valid_signal", "valid")
        ready = evidence.get("ready_signal", "ready")
        lines.extend([
            f"- Likely root direction: downstream path did not assert {ready} while {valid} was requesting transfer.",
            "- Suggested next probes: fifo_full, reset_done/reset_sync_done, credit_count, downstream_ready, outstanding_txn_count.",
            "- Suggested next trigger: valid high and ready low for more than 32 cycles.",
        ])
        if note:
            lines.append(f"- Connector note: {note}")
        return "\n".join(lines)
