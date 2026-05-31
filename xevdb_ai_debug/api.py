from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict

from fastapi import FastAPI, HTTPException, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from agents.orchestrator import DebugOrchestrator
from agents.protocol_agent import detect_axi_events
from connectors.xevdb_connector import XevdbCli
from models import DebugSession
from storage import XevdbStore
from xtrace_writer import write_xtrace

STATE_PATH = os.environ.get("XEVDB_AI_STATE", "examples/debug_state.json")
XEVDB_DB_PATH = os.environ.get("XEVDB_DB")
XEVDB_BACKEND = os.environ.get("XEVDB_BACKEND")

app = FastAPI(title="xevdb AI Debug API", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class AskRequest(BaseModel):
    question: str


@app.get("/health")
def health() -> Dict[str, Any]:
    return {
        "ok": True,
        "state": STATE_PATH,
        "xevdb_db": XEVDB_DB_PATH,
        "xevdb_backend": XEVDB_BACKEND or "local",
    }


@app.get("/sessions")
def sessions() -> Any:
    store = XevdbStore(STATE_PATH)
    try:
        return store.list_sessions()
    finally:
        store.close()


@app.get("/sessions/{session_id}/summary")
def summary(session_id: str) -> Any:
    store = XevdbStore(STATE_PATH)
    try:
        s = store.summarize(session_id)
        if not s["session"]:
            raise HTTPException(404, "session not found")
        return s
    finally:
        store.close()


@app.get("/sessions/{session_id}/events")
def events(session_id: str) -> Any:
    store = XevdbStore(STATE_PATH)
    try:
        return store.get_events(session_id)
    finally:
        store.close()


@app.get("/sessions/{session_id}/signals")
def signals(session_id: str) -> Any:
    store = XevdbStore(STATE_PATH)
    try:
        return store.list_signals(session_id)
    finally:
        store.close()


@app.get("/sessions/{session_id}/window")
def window(session_id: str, start: int, end: int) -> Any:
    store = XevdbStore(STATE_PATH)
    try:
        return store.get_window(session_id, start, end)
    finally:
        store.close()


@app.post("/sessions/{session_id}/ask")
def ask(session_id: str, body: AskRequest) -> Any:
    store = XevdbStore(STATE_PATH)
    try:
        return DebugOrchestrator(store).answer(session_id, body.question)
    finally:
        store.close()


@app.post("/ingest/chipscopy-json")
async def ingest_chipscopy_json(
    file: UploadFile = File(...),
    session_id: str = Form(...),
    project: str = Form("FPGA Debug"),
    board: str = Form("VCK190"),
) -> Any:
    content = await file.read()
    data = json.loads(content.decode("utf-8"))
    events = detect_axi_events(session_id, data, interface="s_axi")

    Path(STATE_PATH).parent.mkdir(parents=True, exist_ok=True)
    store = XevdbStore(STATE_PATH)
    try:
        session = DebugSession(session_id=session_id, project=project, board=board)
        store.upsert_session(session)
        store.clear_capture(session_id)
        row_count = store.ingest_samples(session_id, data)
        event_count = store.ingest_events(events)

        xevdb_result = None
        if XEVDB_DB_PATH:
            xtrace_path = Path(XEVDB_DB_PATH).with_suffix(".xtrace")
            xtrace_path.write_text(write_xtrace(data, events, session_id=session_id))
            xevdb_result = XevdbCli().build_xtrace(
                xtrace_path,
                XEVDB_DB_PATH,
                backend=XEVDB_BACKEND,
                reset=True,
            ).strip()

        return {
            "session_id": session_id,
            "rows": row_count,
            "events": event_count,
            "xevdb": {
                "db": XEVDB_DB_PATH,
                "backend": XEVDB_BACKEND or "local",
                "stdout": xevdb_result,
            } if XEVDB_DB_PATH else None,
        }
    finally:
        store.close()
