from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional


@dataclass
class ChipScoPyConfig:
    hw_server_url: str = "TCP:localhost:3121"
    cs_server_url: str = "TCP:localhost:3042"
    pdi_file: Optional[str] = None
    ltx_file: Optional[str] = None
    ila_name: Optional[str] = None
    window_count: int = 1
    window_size: int = 1024


def capture_from_real_board(config: ChipScoPyConfig) -> Dict[str, List[int]]:
    """Real ChipScoPy capture skeleton.

    This is intentionally isolated so the rest of the MVP runs without AMD tools.
    It follows the common pattern:
      create_session -> program/open device -> select ILA -> trigger -> upload -> waveform.get_data().
    """
    try:
        from chipscopy import create_session
    except ImportError as exc:
        raise RuntimeError("chipscopy is not installed. Install AMD/Xilinx ChipScoPy in your board environment.") from exc

    session = create_session(hw_server_url=config.hw_server_url, cs_server_url=config.cs_server_url)
    device = session.devices.get(family="versal")

    if config.pdi_file:
        # API details vary slightly across ChipScoPy versions/project flows.
        # Some examples pass probes_file for .ltx metadata.
        kwargs = {}
        if config.ltx_file:
            kwargs["probes_file"] = config.ltx_file
        device.program(config.pdi_file, **kwargs)

    if config.ila_name:
        ila = device.ila_cores.get(name=config.ila_name)
    else:
        ila = next(iter(device.ila_cores), None)
    if ila is None:
        raise RuntimeError("No ILA core found. Check that the design was built with ILA debug cores and .ltx metadata.")

    ila.run_trigger_immediately(window_count=config.window_count, window_size=config.window_size)
    ila.wait_till_done(max_wait_minutes=1)
    uploaded = ila.upload()
    if not uploaded:
        raise RuntimeError("ILA capture did not upload waveform data.")

    return ila.waveform.get_data(include_trigger=True, include_sample_info=True, include_gap=True)
