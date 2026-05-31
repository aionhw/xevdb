"""xevdb — VCD + SystemVerilog → single SQLite file.

A .xevdb file holds waveform data, parsed RTL module/port/signal/instance
tables, the full source text of every ingested .sv/.v file, a library of
parameterized SQL "prompts", and a per-DB result cache. One self-contained
artifact, no AI, all SQLite.
"""
from .parser import VCD, Signal, Change, parse, parse_file
from .db import (
    build,
    ingest_rtl,
    ingest_sim,
    open_db,
    resolve_signal,
    value_at,
    window,
    find_signals,
    stats,
)
from .prompts import run_prompt, list_prompts, add_prompt, remove_prompt, show_prompt
from .show import show_code
from .bugs import add_bug, get_bug, list_bugs, remove_bug, search_bugs

__version__ = "0.1.0"
__all__ = [
    "VCD", "Signal", "Change", "parse", "parse_file",
    "build", "ingest_rtl", "ingest_sim",
    "open_db", "resolve_signal", "value_at", "window",
    "find_signals", "stats",
    "run_prompt", "list_prompts", "add_prompt", "remove_prompt", "show_prompt",
    "show_code",
    "add_bug", "get_bug", "list_bugs", "remove_bug", "search_bugs",
]
