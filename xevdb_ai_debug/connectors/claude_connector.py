from __future__ import annotations

import os
import shlex
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict

from .base_connector import BaseDebugConnector


class ClaudeDebugConnector(BaseDebugConnector):
    """Claude Code CLI connector with a deterministic local fallback.

    Mirrors the Codex connector: sends only compact xevdb context (never raw
    traces) and runs the model headlessly via `claude -p`. Enable with
    XEVDB_AI_USE_CLAUDE=1 or pass use_claude=True.

    Knobs:
      CLAUDE_BIN          path to the claude CLI (default: PATH lookup)
      CLAUDE_MODEL        --model passed to claude (optional)
      XEVDB_AI_CLAUDE_CWD working dir for the headless run (default: cwd)
      CLAUDE_ARGS         extra CLI args, shell-split (e.g. "--permission-mode plan")
    """

    model_name = "Claude"

    def __init__(
        self,
        *,
        use_claude: bool | None = None,
        claude_bin: str | None = None,
        model: str | None = None,
        cwd: str | Path | None = None,
    ):
        self.use_claude = (
            os.environ.get("XEVDB_AI_USE_CLAUDE", "").lower() in {"1", "true", "yes"}
            if use_claude is None else use_claude
        )
        self.claude_bin = claude_bin or os.environ.get("CLAUDE_BIN") or shutil.which("claude")
        self.model = model or os.environ.get("CLAUDE_MODEL")
        self.cwd = Path(cwd or os.environ.get("XEVDB_AI_CLAUDE_CWD", Path.cwd()))
        self._extra_args = shlex.split(os.environ.get("CLAUDE_ARGS", ""))

    @property
    def enabled(self) -> bool:
        return bool(self.use_claude and self.claude_bin)

    def _ask_model(self, context: Dict[str, Any]) -> str:
        prompt = self._prompt_text(context)
        # `claude -p` is headless/non-interactive: it prints the final assistant
        # message to stdout and never prompts for approval, so a read-only
        # reasoning call cannot hang or mutate the workspace.
        cmd = [self.claude_bin, "-p", prompt, "--output-format", "text"]
        if self.model:
            cmd += ["--model", self.model]
        cmd += self._extra_args
        proc = subprocess.run(
            cmd,
            text=True,
            cwd=str(self.cwd),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=120,
        )
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip()
            raise RuntimeError(detail or f"claude exited with status {proc.returncode}")
        answer = proc.stdout.strip()
        if not answer:
            raise RuntimeError("claude returned an empty response")
        return answer
