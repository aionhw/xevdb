from __future__ import annotations

from xevdb import db


def test_build_xtrace_writes_waveform_rows(tmp_path):
    xtrace = tmp_path / "capture.xtrace"
    xtrace.write_text(
        "\n".join([
            "xtrace.version 1.0",
            "session DBG-1",
            "source chipscopy",
            "timescale sample",
            "",
            "signal awvalid width=1",
            "signal awaddr width=13",
            "",
            "@0 awvalid=0 awaddr=0x0",
            "@5 trigger=1 awvalid=1 awaddr=0x1000",
            "@6 awvalid=1 awaddr=0x1000",
            "@7 awvalid=0 awaddr=0x1000",
            "",
        ]) + "\n",
        encoding="utf-8",
    )
    out = tmp_path / "capture.xevdb"

    result = db.build_xtrace(xtrace, out, reset=True)

    assert result == {"signals": 2, "changes": 5, "t_min": 0, "t_max": 7}
    with db.open_db(out, read_only=True) as con:
        sig = db.resolve_signal(con, "awaddr")
        assert sig is not None
        assert sig.fullname == "top.DBG_1.awaddr"
        assert db.value_at(con, sig.sig_id, 5) == (5, "1000000000000")
