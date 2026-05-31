#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path
import sys
sys.path.append(str(Path(__file__).resolve().parents[1]))

from agents.orchestrator import DebugOrchestrator
from connectors import select_connector
from storage import XevdbStore


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--state", required=True,
                    help="JSON state file for demo sessions/events.")
    ap.add_argument("--session", required=True)
    ap.add_argument("--question", required=True)
    ap.add_argument("--model", default=None,
                    choices=["claude", "codex", "none"],
                    help="Reasoning backend (default: $XEVDB_AI_MODEL, then legacy "
                         "XEVDB_AI_USE_* flags, else deterministic local fallback).")
    args = ap.parse_args()

    store = XevdbStore(args.state)
    orch = DebugOrchestrator(store, model_connector=select_connector(args.model))
    result = orch.answer(args.session, args.question)
    print(result["answer"])
    store.close()


if __name__ == "__main__":
    main()
