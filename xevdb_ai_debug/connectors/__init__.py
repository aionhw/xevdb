"""Model connectors for the reasoning plane.

`select_connector()` picks the backend from the environment so the API,
ask_agent script, and orchestrator all honor the same selection:

    XEVDB_AI_MODEL = claude | codex | none   (takes precedence)

Without XEVDB_AI_MODEL it falls back to the per-tool legacy flags
(XEVDB_AI_USE_CLAUDE / XEVDB_AI_USE_CODEX); if neither is set, the
deterministic local fallback is used.
"""
from __future__ import annotations

import os

from .base_connector import BaseDebugConnector
from .claude_connector import ClaudeDebugConnector
from .codex_connector import CodexDebugConnector

__all__ = [
    "BaseDebugConnector",
    "ClaudeDebugConnector",
    "CodexDebugConnector",
    "select_connector",
]

_TRUTHY = {"1", "true", "yes"}


def select_connector(model: str | None = None) -> BaseDebugConnector:
    """Return the connector named by `model` (or $XEVDB_AI_MODEL, then legacy flags)."""
    choice = (model or os.environ.get("XEVDB_AI_MODEL") or "").strip().lower()
    if choice == "claude":
        return ClaudeDebugConnector(use_claude=True)
    if choice == "codex":
        return CodexDebugConnector(use_codex=True)
    if choice in {"none", "local", "fallback", "off"}:
        return BaseDebugConnector()

    # auto: honor the per-tool flags, Claude first
    if os.environ.get("XEVDB_AI_USE_CLAUDE", "").lower() in _TRUTHY:
        return ClaudeDebugConnector()
    if os.environ.get("XEVDB_AI_USE_CODEX", "").lower() in _TRUTHY:
        return CodexDebugConnector()
    return BaseDebugConnector()
