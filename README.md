# HMC6300 / HMC6301 Python control

Windows control for the **Analog Devices EK1HMC6350 evaluation motherboard**, using
the FTDI D2XX driver included in `C:\Program Files (x86)\AnalogDevices 60GHz SoC`.
Includes a desktop panel, command-line tools, and an importable Python API.

The implementation uses the HMC6300 Rev. C / HMC6301 Rev. A datasheets, ADI's
public no-OS driver, the installed Terbium application USB routines, and the
installed register profiles. See [implementation details and sources](docs/implementation.md).

**Validation:** 28 unit tests and a hidden-window GUI simulation smoke test pass.
D2XX loads on this PC; enumeration returned
zero interfaces during development. Actual USB-to-chip communication, PLL lock,
and RF performance still require testing on your powered evaluation boards.

## Start the desktop panel

Requires Python 3.10 or newer. No pip packages are needed to run from this folder.
Tkinter is included with standard python.org Windows installations.

```powershell
cd C:\Users\wx321\Documents\ChatGPT\HMC_control
python hmc_gui.py --simulate
```

For the real motherboard:

```powershell
python hmc_gui.py
```

1. Power the board, connect USB, and close the ADI TX/RX applications.
2. Click **List USB** and select the correct FTDI serial number from the dropdown
   (manual entry is also supported). The list includes
   all FTDI interfaces, so identify your evaluation board before connecting.
3. Click **Connect**. The program probes TX and RX and automatically selects the
   likely module when one configuration fingerprint is recognized. If detection
   is inconclusive or both modules are recognized, choose TX/RX manually. Select
   the **physical reference clock**.
4. If register access fails on a freshly powered board, use **Reset board buffers**.
   This is the installed application's interface-reset sequence and affects both
   chips on that motherboard.
5. Click **Load bundled profile (RF/IF off)**, enter a valid frequency, then
   **Tune + check lock**. At the usual 71.42857 MHz reference, 60.0 GHz is valid.
6. Set attenuation codes and click **RF/IF on** to enable the signal chain.

Connecting does not load a profile. Loading a profile keeps the signal chain off
by default. Closing the application releases USB; the chip retains its last
settings. Click **RF/IF off** before closing when you want the signal chain off.
This gate does not power down the entire chip or the PLL.

**Automatic module detection:** Connect performs two read-only passes at both
chip addresses using their respective readback pins. **Detect TX/RX** repeats the
probe. Stable bias/configuration fields are compared with ADI profiles and nominal
datasheet values. The result is a *likely* identification, not a chip-ID check:
there is no documented dedicated ID register. Uninitialized chips, custom bias
settings, missing power or uninitialized board buffers can produce an inconclusive
result. As a fallback based on observed motherboard behavior, stable all-zero
reads on one side opposite stable all-`0xFF` reads select the zero-reading side
as likely present. This is a board-specific heuristic, not proof of presence.
Matching constants on both sides and unstable readback remain inconclusive.
Detection does not program
registers, load profiles, reset buffers, or enable RF. It also supports a motherboard
with both modules fitted. Manual chip selection remains available.

For command-line detection, use `python -m hmc_control --serial YOUR_SERIAL detect`.
The log includes the register evidence for each probe. Fresh simulations contain
zeroed registers, so they are inconclusive until a profile is loaded.

### Edit all registers in the GUI

After connecting and choosing TX or RX, click **All registers…**. A separate
editor for that chip shows all 32 rows, their functions and access types, the
last-read byte, an editable hex byte, decimal value, and bit toggles b7 through b0.

- **Read all (replace edits)** reads all rows and refreshes the grid.
- Enter hex `00`–`FF` or toggle bits to stage a writable row. Nothing is sent yet.
- **Write** sends one row and verifies readback. **Write changed + verify** sends
  all staged writable rows after validating every edited byte.
- **Read** refreshes one row. **Discard edits** restores the last-read values
  without touching hardware.
- Read-only status and unused rows are displayed but cannot be written.

Each window stays tied to its selected chip and connection. Reopen it after
reconnecting. The grid is a manual snapshot: use Read to refresh after changing
settings elsewhere. Raw register writes apply exactly the byte you enter;
writing power or PLL registers can immediately change the operating state.
Batch writes are sequential, so an I/O failure can leave a partially applied batch.

## Command line

Global options (`--serial`, `--chip`, `--reference`, `--simulate`) go **before**
the command. Substitute your board serial for `TX_SERIAL` / `RX_SERIAL`.

```powershell
# Enumerate without opening or programming a board.
python -m hmc_control devices

# Offline demonstration; each invocation starts a fresh simulation.
python -m hmc_control --simulate --chip tx configure 60 --enable

# List valid RF channels for the selected oscillator.
python -m hmc_control --reference 142.8571 channels

# Load the installed TX profile and find a locked band; keep the RF/IF path off.
python -m hmc_control --serial TX_SERIAL --chip tx configure 60
python -m hmc_control --serial TX_SERIAL --chip tx gain --if-code 4 --rf-code 8
python -m hmc_control --serial TX_SERIAL --chip tx path on

# Configure and enable the receiver on its own motherboard.
python -m hmc_control --serial RX_SERIAL --chip rx configure 60 --enable
python -m hmc_control --serial RX_SERIAL --chip rx gain --if-code 2 --lna-code 1
python -m hmc_control --serial RX_SERIAL --chip rx baseband --coarse1 6 --fine-i 1 --fine-q 1
python -m hmc_control --serial RX_SERIAL --chip rx filters --lowpass 500 --highpass 45
python -m hmc_control --serial RX_SERIAL --chip rx mode iq

# Inspect and save settings. Register values may be decimal or hexadecimal.
python -m hmc_control --serial TX_SERIAL --chip tx status
python -m hmc_control --serial TX_SERIAL --chip tx read 24
python -m hmc_control --serial TX_SERIAL --chip tx write 11 0x83
python -m hmc_control --serial TX_SERIAL --chip tx save tx_saved.txt
python -m hmc_control --serial TX_SERIAL --chip tx load tx_saved.txt
python -m hmc_control --serial TX_SERIAL --chip tx path off

python -m hmc_control --help
```

Use `--index N` instead of `--serial` if necessary; USB enumeration indices may
change when boards are reconnected. For a nonstandard installation, supply
`--install-dir PATH` or `--dll PATH`. The DLL must match Python's 32/64-bit architecture;
the normal installation is detected automatically.

`configure` loads a profile and tunes. `tune` changes an already initialized chip,
temporarily gates RF/IF, and restores its previous gate only after finding lock.
If no band locks, RF/IF remains disabled and the command returns a nonzero exit
status. No analog RF waveform is generated by this software: supply your I/Q or
modulation inputs through the board's physical connectors.

## Python API

```python
from hmc_control import FtdiTransport, HMC6300, HMC6301

with FtdiTransport(serial="YOUR_BOARD_SERIAL") as board:
    tx = HMC6300(board, reference="71.42857")
    tx.load_profile()                   # RF/IF disabled
    tx.set_if_attenuation(4)            # hardware code, not dB
    tx.set_rf_attenuation(8)
    print(tx.set_frequency("60.0"))     # scans VCO bands and verifies lock
    tx.set_signal_path(True)
    print(tx.status())
    tx.set_signal_path(False)

    # If BOTH modules are fitted to this same motherboard, share the transport:
    # rx = HMC6301(board, reference="71.42857")
    # rx.load_profile()
    # rx.set_frequency("60.0")
    # rx.set_baseband_attenuation(6, 12, 1, 1)
    # rx.set_baseband_filters(500, 45)
```

For two motherboards, open two `FtdiTransport` instances with different serials.
For offline scripts, replace `FtdiTransport(...)` with `SimulatedTransport()`.
One transport serializes register transactions and read-modify-write operations
using a reentrant lock. Do not open the same USB interface from two processes.

### Supported controls

| Control | HMC6300 TX | HMC6301 RX |
|---|---|---|
| Register read / verified write | Yes | Yes |
| ADI profile load / save | Yes | Yes |
| Internal PLL frequency + band search | Yes | Yes |
| RF/IF path gate | ROW4 | ROW0 |
| Digital IF attenuation code | 0..13 | 0..15 |
| Digital RF/LNA attenuation code | RF 0..15 | LNA 0..3 |
| Baseband attenuation | — | Two coarse stages, I/Q fine |
| Baseband filter corners | — | LP 1400/500/300/200 MHz; HP 45/350/1600 kHz |
| Modulation/demodulation routing | IQ or FSK/MSK inputs | IQ, AM, FM, AM+FM |
| Temperature sensor enable + raw code | Yes | Yes |

Frequency choices are limited to **57–64 GHz** and the valid channel grid of
71.42857, 142.8571, or 154.2857 MHz reference oscillators. Selecting a reference in
software does not change the physical oscillator. Temperature is reported as a
raw thermometer code, not an uncalibrated Celsius estimate. RF/IF attenuation
codes are not calibrated output-power settings.

External-LO setup and custom motherboard wiring are not high-level presets in
this version. Documented writable rows remain accessible for advanced setup.
This transport is intended for ADI's motherboard with its interface circuitry;
bare-chip serial pins use 1.2 V logic and must not be wired directly to FTDI outputs.

## Tests

```powershell
python -m unittest discover -s tests -v
python tests\gui_smoke.py
```

Tests cover golden serial frames, 27-clock read transactions, scanout selection,
profile parsing, readback mismatch handling, register preservation, reversed RX
baseband bitfields, frequency limits, PLL lock/failure, and TX/RX independence.
They do not substitute for an oscilloscope or physical-board verification.

## Troubleshooting

- **No devices:** check board power, USB cable, and the FTDI D2XX driver in Windows
  Device Manager. This program uses D2XX, not a COM-port connection.
- **Open failure:** close ADI GUI instances and other programs using the interface.
- **Readback all zero/all one:** check module selection, board power and buffer
  initialization. Use the explicit `reset-buffers` command, then retry profile load.
- **PLL won't lock:** check the selected reference against the fitted oscillator,
  load the profile before tuning, and verify the reference signal and supply rails.
- **Tkinter missing:** use a Python installation with Tcl/Tk, or use the CLI/API.

The `research/` directory contains local investigation material and is ignored
by Git. It is not needed to run the application. No installed ADI files were modified.
