"""Hidden-window GUI smoke test; never opens a USB interface."""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import tkinter as tk
from hmc_control.__main__ import make_parser
from hmc_control.gui import ControlPanel


def main():
    root = tk.Tk()
    root.withdraw()
    panel = ControlPanel(root, make_parser().parse_args(["--simulate", "gui"]))

    def wait():
        deadline = time.monotonic() + 5
        while panel.busy:
            root.update()
            if time.monotonic() > deadline:
                raise AssertionError("GUI worker timed out")
            time.sleep(0.01)
        assert "ERROR:" not in panel.message.get(), panel.message.get()

    try:
        panel.connect()
        wait()
        assert "SIMULATION" in panel.session.get()
        assert panel.simulation_control.instate(["disabled"])
        assert "inconclusive" in panel.detection.get()
        from hmc_control import HMC6301
        HMC6301(panel.bus).load_profile()
        panel.detect()
        wait()
        assert panel.chip.get() == "rx"
        assert "Likely RX" in panel.detection.get()
        for chip in ("tx", "rx"):
            panel.chip.set(chip)
            panel.action("load_profile")
            wait()
            panel.action("set_frequency", "60")
            wait()
            panel.action("status")
            wait()
            editor = panel.open_registers()
            editor.window.withdraw()
            assert len(editor.values) == 32
            editor.read_all()
            wait()
            assert len(editor.last_read) == 32
            assert editor.values[20].get() == "30"
            # A byte edit and a bit toggle remain staged until Write.
            count = len(panel.bus.writes)
            editor.values[20].set("31")
            editor.bits[20][1].set(1)
            editor.toggle_bit(20, 1)
            assert editor.values[20].get() == "33"
            assert len(panel.bus.writes) == count
            editor.write_changed()
            wait()
            assert editor.device.read_register(20) == 0x33
            assert editor.readbacks[20].get() == "0x33"
            assert editor.states[20].get() == "Unchanged"
            # Validate the entire batch before any writes.
            editor.values[20].set("32")
            editor.values[22].set("GG")
            count = len(panel.bus.writes)
            editor.write_changed()
            assert len(panel.bus.writes) == count
            assert "R22" in editor.notice.get()
            editor.discard()
            assert editor.values[20].get() == "33"
            editor.values[24].set("00")
            editor.write_rows([24])
            assert len(panel.bus.writes) == count
            assert "read-only" in editor.notice.get()
            # The window remains tied to its chip even if the main selection changes.
            panel.chip.set("rx" if chip == "tx" else "tx")
            editor.values[20].set("34")
            editor.write_rows([20])
            wait()
            assert editor.device.read_register(20) == 0x34
            editor.window.destroy()
        panel.detect()
        wait()
        assert "Both TX and RX" in panel.detection.get()
        panel.disconnect()
        wait()
        assert panel.bus is None
        assert panel.simulation_control.instate(["!disabled"])
        print("GUI smoke passed: TX/RX controls, 32-row editors, staged byte/bit writes, batch validation, RO protection, chip isolation, disconnect")
    finally:
        if panel.bus is not None:
            panel.bus.close()
        panel.executor.shutdown(wait=True)
        root.destroy()


if __name__ == "__main__":
    main()
