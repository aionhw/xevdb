from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# Real ChipScoPy `waveform.get_data(..., include_sample_info=True)` returns
# columns named `trigger` / `sample_index` / `window_index` /
# `window_sample_index`; the rest of this app keys meta columns with a `__`
# prefix (see storage.META_KEYS), so adapt them here.
_META_MAP = {
    "trigger": "__TRIGGER",
    "sample_index": "__SAMPLE_INDEX",
    "window_index": "__WINDOW_INDEX",
    "window_sample_index": "__WINDOW_SAMPLE_INDEX",
    "gap": "__GAP",
}


def normalize_chipscopy_data(raw: Dict[str, Any]) -> Dict[str, List[int]]:
    """Adapt a ChipScoPy `get_data` result to this app's `__`-prefixed shape."""
    out: Dict[str, List[Any]] = {}
    for key, values in dict(raw).items():
        out[_META_MAP.get(key, key)] = list(values)
    if "__TRIGGER" in out:
        out["__TRIGGER"] = [int(bool(x)) for x in out["__TRIGGER"]]
    if "__SAMPLE_INDEX" in out:
        out["__SAMPLE_INDEX"] = [int(x) for x in out["__SAMPLE_INDEX"]]
    return out


@dataclass
class ChipScoPyConfig:
    hw_server_url: str = "TCP:localhost:3121"
    cs_server_url: str = "TCP:localhost:3042"
    family: str = "versal"
    pdi_file: Optional[str] = None       # programming (.pdi)
    ltx_file: Optional[str] = None       # probe metadata (.ltx)
    ila_name: Optional[str] = None
    probes: List[str] = field(default_factory=list)   # probes to fetch (empty = all)
    window_count: int = 1
    window_size: int = 1024
    trigger_position: int = 512
    trigger_values: Dict[str, List[str]] = field(default_factory=dict)
    max_wait_minutes: float = 1.0


def capture_from_real_board(config: ChipScoPyConfig) -> Dict[str, List[int]]:
    """Real ChipScoPy capture, following chipscopy/examples/ila_and_vio.

    Isolated so the rest of the MVP runs without AMD tools. Returns data already
    normalized to this app's `__`-prefixed sample-dict shape.

    Flow (per the official examples):
      create_session(cs_server_url=, hw_server_url=)
      -> devices.filter_by(family=).get()
      -> program(pdi) -> discover_and_setup_cores(ltx_file=ltx)
      -> ila_cores.get(name=) -> reset_probes()/set_probe_trigger_value()
      -> run_basic_trigger(window_count, window_size, trigger_position)
      -> wait_till_done() -> upload()
      -> waveform.get_data([probes], include_trigger=True, include_sample_info=True)
    """
    try:
        from chipscopy import create_session
    except ImportError as exc:
        raise RuntimeError("chipscopy is not installed. Install AMD/Xilinx "
                           "ChipScoPy in your board environment.") from exc

    session = create_session(cs_server_url=config.cs_server_url,
                             hw_server_url=config.hw_server_url)
    device = session.devices.filter_by(family=config.family).get()
    if config.pdi_file:
        device.program(config.pdi_file)
    # LTX probe metadata loads via discover_and_setup_cores, NOT program(...).
    device.discover_and_setup_cores(ltx_file=config.ltx_file)

    if config.ila_name:
        ila = device.ila_cores.get(name=config.ila_name)
    else:
        ila = next(iter(device.ila_cores), None)
    if ila is None:
        raise RuntimeError("No ILA core found. Check the design was built with "
                           "ILA debug cores and .ltx metadata.")

    ila.reset_probes()
    for probe, condition in config.trigger_values.items():
        ila.set_probe_trigger_value(probe, condition)
    ila.run_basic_trigger(window_count=config.window_count,
                          window_size=config.window_size,
                          trigger_position=config.trigger_position)
    ila.wait_till_done(max_wait_minutes=config.max_wait_minutes)
    if not ila.upload():
        raise RuntimeError("ILA capture did not upload waveform data.")

    raw = ila.waveform.get_data(
        config.probes or None,
        include_trigger=True,
        include_sample_info=True,
    )
    return normalize_chipscopy_data(raw)
