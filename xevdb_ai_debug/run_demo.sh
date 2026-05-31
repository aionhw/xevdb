#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
python scripts/generate_synthetic_capture.py --out examples/failing_axi_capture.json
python scripts/ingest_capture.py \
  --capture examples/failing_axi_capture.json \
  --state examples/debug_state.json \
  --xtrace-out examples/failing_axi_capture.xtrace \
  --xevdb-db examples/failing_axi_capture.xevdb \
  --session DBG-AXI-001 \
  --project "AXI DMA Bring-up" \
  --board VCK190
python scripts/ask_agent.py \
  --state examples/debug_state.json \
  --session DBG-AXI-001 \
  --question "Why did AXI write hang?"
echo
echo "Run API:       uvicorn api:app --reload --port 8080"
echo "Run dashboard: python -m http.server 8081 -d dashboard"
echo "Open:          http://localhost:8081"
