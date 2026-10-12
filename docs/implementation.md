# Implementation evidence and limits

## Sources

- [HMC6300 Rev. C datasheet](https://www.analog.com/media/en/technical-documentation/data-sheets/hmc6300.pdf):
  serial interface pp. 13–14, registers pp. 15–19, synthesizer tables pp. 20–21.
- [HMC6301 Rev. A datasheet](https://www.analog.com/media/en/technical-documentation/data-sheets/hmc6301.pdf):
  serial interface pp. 12–13, registers pp. 13–18, synthesizer tables pp. 19–20.
- [EK1HMC6350 user guide UG-1031](https://www.analog.com/media/en/technical-documentation/user-guides/EK1HMC6350-UG-1031.pdf):
  evaluation-board context and GUI workflow.
- [ADI no-OS HMC630x driver](https://github.com/analogdevicesinc/no-OS/tree/main/drivers/rf-transceiver/hmc630x):
  `hmc630x_read_row`, `hmc630x_set_vco`, and RX baseband enum encodings.
  Public source retrieved on 2026-10-12; local research copies retained.
- Installed `Terbium_TX.exe` (2016-10-21): named functions inspected read-only to
  recover the evaluation motherboard's D2XX setup and pin assignment.
- Installed `FTD_USBDrivers/ftd2xx.h`: ctypes function signatures and status codes.
- Installed `TX_REG_FILES/HMC6300.txt`, `RX_REG_FILES/HMC6301.txt`:
  copied byte-for-byte as the bundled profiles. These are vendor evaluation
  presets, not claimed silicon reset values. They remain ADI-supplied data.
- Installed `TX_REG_FILES/71MHZ.txt` and `142MHZ.txt`: divider-code cross-check.

## Serial protocol

The transmitted word is `data | row << 8 | write << 14 | chip << 15`.
Exactly 18 bits are shifted LSB-first while ENABLE is low. CLK idles low and
the chip captures DATA on rising edges. The rising ENABLE edge latches the frame.
TX chip address is 6; RX chip address is 7. The row field occupies six bits,
with this implementation accepting only array indices 0..31.

For a read, send the same 18-bit header with write=0, raise ENABLE, issue one
additional clock, lower ENABLE, and collect eight SCANOUT bits LSB-first after
the rising clock edges. Sampling after CLK falls follows ADI's no-OS code.
Raise ENABLE again to end the read. A test verifies all 27 clock edges and the
ENABLE state during each phase.

The datasheets contain inconsistent prose about the number of address bits and
rows; the diagrams and ADI driver agree on the actual frame layout. RX ROW0,
not ROW4, is its RF/IF power-down gate, as the register table specifies.

## Evaluation motherboard USB mapping

| FTDI bit | Function |
|---|---|
| D0 | CLK output |
| D1 | DATA output |
| D2 | ENABLE output |
| D3 | RX SCANOUT input |
| D4 | TX SCANOUT input |
| D5 | Motherboard buffer-control output |
| D6–D7 | Motherboard mux/control outputs |

Evidence from the installed TX executable:

- `_Terb_USB_Init` at `0x407C8F` calls `_FTDI_Init` with direction mask `0xE7`.
- `_FTDI_Init` at `0x40A5F0` sets asynchronous bit-bang mode 1 and baud 28800.
- `_Terb_GetSerialWord` at `0x407DD1` packs data, row, R/W and chip address.
- `_Terb_Write1RegisterUsb` at `0x409598` selects the 18-bit serial transaction
  with CLK=0, DATA=1, ENABLE=2.
- `_Terb_Read1RegisterUsb` at `0x409624` selects scanout bit 4 for TX (type 0)
  and bit 3 for RX (type 1).
- `_FTDI_TerbiumRead` at `0x40C0AC` performs the extra clock and eight read clocks.
- `_Terb_ResetBuffers` at `0x4099DB` toggles D5 and the buffer control lines;
  the Python reset method reproduces that sequence as an explicit action.

Python uses `ctypes.WinDLL`, typed 32-bit DWORD values, and pointer-sized handles.
The DLL is selected to match Python's bitness. No dependency on the 32-bit CVI
runtime or the ADI GUI process is needed. DLLs and executables are not copied
into this project.

The transport deliberately uses conservative host timing: wait for the transmit
queue to drain and allow additional settling before sampling pins. This makes
configuration slow compared with hardware SPI, especially during verified band
search. It is for setup and control, not fast hopping or real-time TDD switching.
The remaining hardware validation is actual pin timing, level-shifter behavior,
register readback, and lock detection on your board revision.

## Best-effort module detection

The GUI probes both serial addresses on connection; it does not infer module type
from the FTDI serial number. The register maps above contain no dedicated ID register.
`detection.py` compares two read passes over static configuration fields against
the installed profiles and nominal bias recommendations. At least 75% of fields,
including three chip-specific fields below row 16, must match, and readbacks must
be stable and contain at least three distinct byte values. These thresholds are
software heuristics, not manufacturer identification guarantees. As a fallback,
the user's observed RX=all-zero / disconnected TX=all-FF pattern selects likely
RX; the reverse selects likely TX. Both passes must be stable. This board-specific
zero/FF contrast does not prove presence (a stuck input can mimic it). Matching
constants, other unrecognized configurations, and read errors yield no selection.
Both recognized fingerprints yield a both-modules result. No detection operation
writes registers or resets the board. Physical hardware validation is outstanding.

## Register policy

Writable rows are TX 1..12 and 16..23, RX 0..9 and 16..23. Status rows 24..27
are read-only; unused rows cannot be written through the device API. Masked
updates read actual hardware before changing a field; there is no assumed
register cache. Profile files are fully validated before any programming starts.

Startup uses the installed profiles exactly, except the signal-path gate is
forced off unless the caller explicitly enables it. Several TX preset bytes
differ from the datasheet's nominal recommendations and the newer no-OS default
map (notably rows 1, 2, 5, 6, 9 and 12, depending on source). These differences
are preserved rather than silently combined into an unvalidated new bias profile.
Users can load another ADI-format profile for their own board.

RX coarse/fine attenuation and baseband corner fields have reversed bit
significance. For example, fine 1 dB encodes as field value 4, and the 500 MHz
low-pass setting is field value 2. These are cross-checked against ADI's no-OS
enums and tested explicitly.

The frequency operation selects the proper channel-spacing bit, enables the
internal synthesizer, programs the divider, and searches legal bands 0..19.
It selects the middle of the longest contiguous lock window and verifies lock
again. The settling delay is a conservative 5 ms default, not a datasheet timing
guarantee. On failure, the prior RF/IF gate is not re-enabled. There is no claim
that a lock bit proves the measured RF frequency or RF performance.

The simulated backend provides a deterministic synthetic lock window 6..10.
It verifies software behavior, not analog circuitry, calibration, temperature,
or USB timing. It starts fresh for every CLI invocation and persists within
one GUI/API connection.
