"""Windows D2XX backend for the original ADI EK1HMC6350 motherboard.

Pin assignment and asynchronous bit-bang mode were recovered from the installed
Terbium_TX.exe routines; see docs/implementation.md. No external Python packages.
"""
from __future__ import annotations

import ctypes as ct
import os
import threading
import time
from pathlib import Path

ADI_DIRECTORY = Path(r"C:\Program Files (x86)\AnalogDevices 60GHz SoC")


class HMCError(RuntimeError):
    """Hardware, transport, readback, or PLL-lock failure."""


def integer(value, low, high, name):
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ValueError(f"{name} must be an integer from {low} to {high}")
    return value


def frame_word(chip, row, value=0, *, write=False):
    if chip not in (6, 7):
        raise ValueError("Chip address must be 6 (TX) or 7 (RX)")
    integer(row, 0, 31, "Register")
    integer(value, 0, 255, "Value")
    return value | (row << 8) | (int(write) << 14) | (chip << 15)


def frame_waveform(chip, row, value=0, *, write=False, auxiliary=0):
    """18 LSB-first clocks; D0=CLK, D1=DATA, D2=ENABLE, idle CLK low.

    Preserve motherboard control outputs D5..D7, never drive SCANOUT D3/D4.
    """
    word = frame_word(chip, row, value, write=write)
    base = auxiliary & 0xE0
    wave = bytearray([base | 4, base])
    for bit in range(18):
        level = base | (((word >> bit) & 1) << 1)
        wave.extend((level, level | 1, level))
    wave.extend((base, base | 4))
    return bytes(wave)


class D2XX:
    """Small typed ctypes wrapper. Loading/enumerating does not open a board."""

    def __init__(self, dll_path=None, install_dir=ADI_DIRECTORY):
        if os.name != "nt":
            raise HMCError("The ADI USB backend requires Windows; use simulation elsewhere")
        bits = ct.sizeof(ct.c_void_p) * 8
        arch, name = ("amd64", "ftd2xx64.dll") if bits == 64 else ("i386", "ftd2xx.dll")
        candidates = [Path(dll_path)] if dll_path else [
            Path(install_dir) / "FTD_USBDrivers" / arch / name,
            Path(os.environ.get("SystemRoot", r"C:\Windows")) /
            ("System32" if bits == 64 else "SysWOW64") / "ftd2xx.dll",
        ]
        errors = []
        for path in candidates:
            try:
                self.dll = ct.WinDLL(str(path.resolve()))
                self.path = path
                break
            except OSError as exc:
                errors.append(f"{path}: {exc}")
        else:
            raise HMCError(f"Cannot load a {bits}-bit FTDI D2XX DLL. " + "; ".join(errors))
        U, B, H, P = ct.c_uint32, ct.c_ubyte, ct.c_void_p, ct.POINTER
        specs = {
            "FT_CreateDeviceInfoList": [P(U)],
            "FT_GetDeviceInfoDetail": [U, P(U), P(U), P(U), P(U), H, H, P(H)],
            "FT_OpenEx": [H, U, P(H)], "FT_Open": [ct.c_int, P(H)],
            "FT_Close": [H], "FT_SetBitMode": [H, B, B],
            "FT_SetBaudRate": [H, U], "FT_SetTimeouts": [H, U, U],
            "FT_GetBitMode": [H, P(B)], "FT_Write": [H, H, U, P(U)],
            "FT_GetStatus": [H, P(U), P(U), P(U)], "FT_Purge": [H, U],
        }
        for name, args in specs.items():
            function = getattr(self.dll, name)
            function.argtypes, function.restype = args, U

    def call(self, name, *args):
        status = getattr(self.dll, name)(*args)
        if status:
            raise HMCError(f"{name} failed (FT_STATUS={status}); check USB, power, "
                           "device selection, and close the ADI GUI")

    def devices(self):
        count = ct.c_uint32()
        self.call("FT_CreateDeviceInfoList", ct.byref(count))
        result = []
        for index in range(count.value):
            flags, kind, ident, location = (ct.c_uint32() for _ in range(4))
            serial, description = ct.create_string_buffer(16), ct.create_string_buffer(64)
            handle = ct.c_void_p()
            self.call("FT_GetDeviceInfoDetail", index, ct.byref(flags), ct.byref(kind),
                      ct.byref(ident), ct.byref(location), serial, description, ct.byref(handle))
            result.append(dict(index=index, serial=serial.value.decode("ascii", "replace"),
                               description=description.value.decode("ascii", "replace"),
                               type=kind.value, id=f"0x{ident.value:08X}",
                               opened=bool(flags.value & 1)))
        return result


class FtdiTransport:
    """Conservative asynchronous bit-bang transport for the ADI motherboard.

    Explicit serial or index selection is required. Opening does not program IC
    registers. Use one shared instance for both chips on a single motherboard.
    """
    def __init__(self, *, serial=None, index=None, dll_path=None,
                 install_dir=ADI_DIRECTORY):
        if (serial is None) == (index is None):
            raise ValueError("Specify exactly one FTDI serial number or device index")
        if serial is not None and (not serial or "\0" in serial):
            raise ValueError("Serial number must be nonempty and contain no NUL")
        if index is not None:
            integer(index, 0, 2**31 - 1, "Device index")
        self.api = D2XX(dll_path, install_dir)
        self.handle = ct.c_void_p()
        self.lock = threading.RLock()
        self.closed = True
        if serial is not None:
            serial_buffer = ct.create_string_buffer(serial.encode("ascii"))
            self.api.call("FT_OpenEx", serial_buffer, 1, ct.byref(self.handle))
        else:
            self.api.call("FT_Open", index, ct.byref(self.handle))
        self.closed = False
        try:
            self.api.call("FT_SetTimeouts", self.handle, 1000, 1000)
            # D3=RX SCANOUT and D4=TX SCANOUT are inputs; other pins outputs.
            self.api.call("FT_SetBitMode", self.handle, 0xE7, 1)
            self.api.call("FT_SetBaudRate", self.handle, 28800)
            self.auxiliary = self._pins() & 0xE0
            self._write(bytes([self.auxiliary | 4]))
        except BaseException:
            self.close()
            raise

    def _check_open(self):
        if self.closed:
            raise HMCError("USB connection is closed")

    def _pins(self):
        self._check_open()
        value = ct.c_ubyte()
        self.api.call("FT_GetBitMode", self.handle, ct.byref(value))
        return value.value

    def _write(self, data):
        self._check_open()
        buffer = ct.create_string_buffer(data)
        count = ct.c_uint32()
        self.api.call("FT_Write", self.handle, buffer, len(data), ct.byref(count))
        if count.value != len(data):
            raise HMCError(f"Short USB write: {count.value}/{len(data)} bytes")
        deadline = time.monotonic() + 1.0
        while True:
            rx, tx, event = ct.c_uint32(), ct.c_uint32(), ct.c_uint32()
            self.api.call("FT_GetStatus", self.handle, ct.byref(rx), ct.byref(tx), ct.byref(event))
            if not tx.value:
                break
            if time.monotonic() >= deadline:
                raise HMCError("USB output queue did not drain within one second")
            time.sleep(0.001)
        # Queue-empty is not a pin-level completion guarantee. Allow the on-chip
        # FIFO to finish before another control transfer samples the pins.
        time.sleep(max(0.002, len(data) / 28800))
        self.api.call("FT_Purge", self.handle, 1)  # discard async input stream only

    def write_register(self, chip, row, value):
        with self.lock:
            self._write(frame_waveform(chip, row, value, write=True, auxiliary=self.auxiliary))

    def read_register(self, chip, row):
        with self.lock:
            self._write(frame_waveform(chip, row, auxiliary=self.auxiliary))
            base = self.auxiliary
            # Clock 19 while ENABLE is high, then 8 read clocks with ENABLE low.
            self._write(bytes([base | 5, base | 4, base]))
            result = 0
            try:
                for bit in range(8):
                    self._write(bytes([base | 1, base]))
                    result |= ((self._pins() >> (4 if chip == 6 else 3)) & 1) << bit
            finally:
                self._write(bytes([base | 4]))
            return result

    def reset_buffers(self):
        """Explicit motherboard buffer reset, matching Terb_ResetBuffers.

        Affects the whole motherboard (both chips). Call only when requested.
        """
        with self.lock:
            self.api.call("FT_SetBitMode", self.handle, 0xFF, 1)
            try:
                wave = [0x20, 0, 0x20]
                for bit in range(8):
                    if bit != 5:
                        wave.extend((0x20 | (1 << bit), 0x20))
                wave.append(0)
                self._write(bytes(wave))
            finally:
                self.api.call("FT_SetBitMode", self.handle, 0xE7, 1)
                self.auxiliary = 0
                self._write(b"\x04")

    def close(self):
        with self.lock:
            if not self.closed:
                try:
                    self.api.call("FT_Close", self.handle)
                finally:
                    self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


class SimulatedTransport:
    """Deterministic register model, not a model of RF performance."""
    def __init__(self, lock_bands=range(6, 11)):
        self.registers = {6: [0] * 32, 7: [0] * 32}
        self.lock_bands = set(lock_bands)
        self.writes = []
        self.closed = False
        self.lock = threading.RLock()

    def write_register(self, chip, row, value):
        frame_word(chip, row, value, write=True)
        if self.closed:
            raise HMCError("Simulation connection is closed")
        self.registers[chip][row] = value
        self.writes.append((chip, row, value))

    def read_register(self, chip, row):
        frame_word(chip, row)
        if self.closed:
            raise HMCError("Simulation connection is closed")
        regs = self.registers[chip]
        if row == 24:
            synth_on = (regs[16] & 0x36) == 0x36 and (regs[18] & 0x16) == 6
            return 9 if synth_on and (regs[22] & 31) in self.lock_bands else 0
        return regs[row]

    def reset_buffers(self):
        pass

    def close(self):
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
