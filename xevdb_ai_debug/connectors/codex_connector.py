from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict

from .base_connector import BaseDebugConnector


class CodexDebugConnector(BaseDebugConnector):
    """Codex CLI connector with a deterministic local fallback.

    Sends only compact xevdb context, not raw traces. Set XEVDB_AI_USE_CODEX=1
    or pass use_codex=True to call `codex exec`.
    """

    model_name = "Codex"

    def __init__(
        self,
        *,
        use_codex: bool | None = None,
        codex_bin: str | None = None,
        cwd: str | Path | None = None,
    ):
        self.use_codex = (
            os.environ.get("XEVDB_AI_USE_CODEX", "").lower() in {"1", "true", "yes"}
            if use_codex is None else use_codex
        )
        self.codex_bin = codex_bin or os.environ.get("CODEX_BIN") or shutil.which("codex")
        self.cwd = Path(cwd or os.environ.get("XEVDB_AI_CODEX_CWD", Path.cwd()))

    @property
    def enabled(self) -> bool:
        return bool(self.use_codex and self.codex_bin)

    def _ask_model(self, context: Dict[str, Any]) -> str:
        prompt = self._prompt_text(context)
        with tempfile.NamedTemporaryFile("r+", encoding="utf-8", suffix=".txt") as out:
            cmd = [
                self.codex_bin,
                "--sandbox",
                "read-only",
                "--ask-for-approval",
                "never",
                "exec",
                "--skip-git-repo-check",
                "--cd",
                str(self.cwd),
                "--output-last-message",
                out.name,
                prompt,
            ]
            proc = subprocess.run(
                cmd,
                text=True,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=120,
            )
            if proc.returncode != 0:
                detail = (proc.stderr or proc.stdout or "").strip()
                raise RuntimeError(detail or f"codex exited with status {proc.returncode}")
            out.seek(0)
            return out.read().strip()
