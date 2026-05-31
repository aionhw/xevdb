from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path


class XevdbCli:
    """Thin adapter around the real xevdb CLI, including its OpenSearch backend."""

    def __init__(self, binary: str | None = None, cwd: str | Path | None = None):
        self.binary = binary or self._discover_binary()
        self.cwd = Path(cwd) if cwd else None

    @staticmethod
    def _discover_binary() -> str:
        explicit = os.environ.get("XEVDB_BIN")
        if explicit:
            return explicit
        found = shutil.which("xevdb")
        if found:
            return found
        sibling = Path(__file__).resolve().parents[1].parent / ".venv" / "bin" / "xevdb"
        return str(sibling)

    def run(self, args: list[str], *, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        cmd = [self.binary, *args]
        merged_env = os.environ.copy()
        if env:
            merged_env.update(env)
        return subprocess.run(
            cmd,
            cwd=str(self.cwd) if self.cwd else None,
            env=merged_env,
            text=True,
            capture_output=True,
            check=True,
        )

    def build_xtrace(
        self,
        xtrace_path: str | Path,
        db_path: str | Path,
        *,
        backend: str | None = None,
        reset: bool = True,
    ) -> str:
        args: list[str] = []
        if backend:
            args.extend(["--backend", backend])
        args.extend(["build-xtrace", str(xtrace_path), "--db", str(db_path)])
        if reset:
            args.append("--reset")
        return self.run(args).stdout

    def stats(self, db_path: str | Path, *, backend: str | None = None) -> str:
        args: list[str] = []
        if backend:
            args.extend(["--backend", backend])
        args.extend(["stats", str(db_path), "--json"])
        return self.run(args).stdout
