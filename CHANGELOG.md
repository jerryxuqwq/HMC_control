# Release notes

## 0.1.0

- HMC6300 TX and HMC6301 RX control using the installed ADI FTDI driver.
- Desktop panel, command-line interface, and Python API.
- USB serial dropdown and read-only automatic module detection.
- Configuration fingerprints plus the observed zero/0xFF contrast heuristic.
- Full 32-row register editor with staged byte/bit edits and verified writes.
- Frequency tuning with PLL band search, gain controls, RX baseband controls,
  profile import/export, and simulation mode.

Validation: 28 unit tests and the GUI simulation smoke test pass. Physical-board
USB timing, register writes, PLL lock and RF behavior are not yet validated.
The user supplied a real detection trace with TX=0xFF and RX=0x00; software
tests cover that pattern and its reverse. Detection remains a likely identity,
not a guaranteed presence test.

Run `Launch HMC Control.cmd` after extracting the ZIP, or run `python hmc_gui.py`.
Python 3.10+ with Tkinter and the ADI FTDI driver are required for hardware use.
The ZIP does not bundle Python or proprietary driver DLLs.
