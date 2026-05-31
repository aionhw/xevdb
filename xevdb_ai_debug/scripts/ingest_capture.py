#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

import sys
sys.path.append(str(Path(__file__).resolve().parents[1]))

from agents.protocol_agent import detect_axi_events
from connectors.xevdb_connector import XevdbCli
from models import DebugSession
from storage import XevdbStore
from xtrace_writer import write_xtrace


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--capture", required=True)
    ap.add_argument("--state", required=True,
                    help="JSON state file for demo sessions/events.")
    ap.add_argument("--session", required=True)
    ap.add_argument("--project", default="FPGA Debug")
    ap.add_argument("--board", default="VCK190")
    ap.add_argument("--xtrace-out", default=None)
    ap.add_argument("--xevdb-db", default=None,
                    help="Build a real xevdb dataset from the exported XTrace capture.")
    ap.add_argument("--xevdb-backend", default=None,
                    help="Backend passed to `xevdb --backend` (for example: opensearch).")
    ap.add_argument("--xevdb-bin", default=None,
                    help="Path to xevdb CLI. Defaults to $XEVDB_BIN, PATH, or ../xevdb/.venv/bin/xevdb.")
    ap.add_argument("--no-xevdb-reset", action="store_true",
                    help="Do not pass --reset to xevdb build.")
    args = ap.parse_args()

    data = json.loads(Path(args.capture).read_text())
    events = detect_axi_events(args.session, data, interface="s_axi")

    store = XevdbStore(args.state)
    session = DebugSession(session_id=args.session, project=args.project, board=args.board)
    store.upsert_session(session)
    store.clear_capture(args.session)
    n_samples = store.ingest_samples(args.session, data)
    n_events = store.ingest_events(events)

    if args.xtrace_out:
        Path(args.xtrace_out).write_text(write_xtrace(data, events, session_id=args.session))

    xevdb_result = None
    if args.xevdb_db:
        xtrace_out = Path(args.xtrace_out) if args.xtrace_out else Path(args.xevdb_db).with_suffix(".xtrace")
        if not args.xtrace_out:
            xtrace_out.write_text(write_xtrace(data, events, session_id=args.session))
        xevdb_out = XevdbCli(binary=args.xevdb_bin).build_xtrace(
            xtrace_out,
            args.xevdb_db,
            backend=args.xevdb_backend,
            reset=not args.no_xevdb_reset,
        )
        xevdb_result = {
            "db": args.xevdb_db,
            "xtrace": str(xtrace_out),
            "backend": args.xevdb_backend or "local",
            "stdout": xevdb_out.strip(),
        }

    print(json.dumps({"rows": n_samples, "events": n_events, "xevdb": xevdb_result}, indent=2))
    store.close()


if __name__ == "__main__":
    main()
