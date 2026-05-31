# xevdb Agent Design

## Deterministic data plane

1. Capture Agent
   - Real mode: ChipScoPy -> ILA waveform.get_data()
   - Demo mode: synthetic ChipScoPy-shaped JSON

2. XTrace Normalizer
   - Converts probe sample dictionary into XTrace text and xevdb rows.

3. xevdb Store
   - JSON session/event state for dashboard state.
   - Canonical waveform handoff is XTrace -> `xevdb build-xtrace`.
   - OpenSearch goes through `xevdb --backend opensearch`, not app-local indexing.

4. Protocol Agent
   - Deterministic AXI/AXIS valid-ready stall detection.
   - Produces evidence objects for model consumption.

## Reasoning plane

5. Orchestrator Agent
   - Accepts user question.
   - Fetches xevdb summary, events, relevant signal window.
   - Sends compact evidence to model connector.

6. Model Connector (Claude or Codex)
   - `claude -p` or `codex exec`, selected by `XEVDB_AI_MODEL` / `--model`.
   - Shared base builds the compact context and the deterministic fallback used
     when no CLI is available.
   - Rule: never send raw trace by default; only selected event/window/context.

## Dashboard

- Static dashboard using the xevdb AI Debug API.
- Shows sessions, protocol events, metrics, and Codex-style debug console.
