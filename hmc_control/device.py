"""Register-level API using ADI datasheets and installed evaluation profiles."""
from __future__ import annotations

import math
import re
import time
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .transport import HMCError, integer

PROFILE_DIR = Path(__file__).with_name("profiles")
# Nominal channel tables, not a formula using rounded reference-clock values.
REFERENCES = {
    "71.42857": (56_500_000_000, 250_000_000, 0x22, 41, True),
    "142.8571": (56_500_000_000, 500_000_000, 0x11, 21, False),
    "154.2857": (57_240_000_000, 540_000_000, 0x0A, 17, False),
}


def frequency_hz(ghz):
    try:
        number = Decimal(str(ghz)) * 1_000_000_000
        if not number.is_finite() or number != number.to_integral_value():
            raise ValueError
        return int(number)
    except (InvalidOperation, ValueError, OverflowError) as exc:
        raise ValueError("Frequency must be a finite GHz value with integer-Hz precision") from exc


def channels(reference):
    if reference not in REFERENCES:
        raise ValueError(f"Reference must be one of {', '.join(REFERENCES)} MHz")
    start, step, code, count, _ = REFERENCES[reference]
    return {start + i * step: code + i for i in range(count)
            if 57_000_000_000 <= start + i * step <= 64_000_000_000}


def load_profile(path):
    """Read the ADI GUI's decimal REGISTER_COUNT/REGISTERn text format strictly."""
    rows, count = {}, None
    for line_number, line in enumerate(Path(path).read_text(encoding="utf-8-sig").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        match = re.fullmatch(r"(REGISTER_COUNT|REGISTER\d+)\s+(\d+)", line)
        if not match:
            raise ValueError(f"Invalid profile line {line_number}: {line!r}")
        name, value = match[1], int(match[2])
        if name == "REGISTER_COUNT":
            if count is not None:
                raise ValueError("Duplicate REGISTER_COUNT")
            count = value
        else:
            row = int(name[8:])
            integer(row, 0, 23, "Profile register")
            integer(value, 0, 255, "Profile value")
            if row in rows:
                raise ValueError(f"Duplicate REGISTER{row}")
            rows[row] = value
    if count != 24 or set(rows) != set(range(24)):
        raise ValueError("A profile must contain REGISTER_COUNT 24 and exactly REGISTER0..23")
    return rows


class HMCDevice:
    def __init__(self, transport, chip, *, reference="71.42857", verify=True):
        if chip not in (6, 7):
            raise ValueError("Chip must be 6 or 7")
        channels(reference)
        self.transport, self.chip = transport, chip
        self.reference, self.verify = reference, verify
        self.name = "HMC6300" if chip == 6 else "HMC6301"
        self.power_row = 4 if chip == 6 else 0
        self.writable = tuple(list(range(1, 13) if chip == 6 else range(10)) + list(range(16, 24)))

    def read_register(self, row):
        integer(row, 0, 31, "Register")
        with self.transport.lock:
            value = self.transport.read_register(self.chip, row)
            integer(value, 0, 255, "Readback")
            return value

    def write_register(self, row, value):
        integer(row, 0, 31, "Register")
        integer(value, 0, 255, "Value")
        if row not in self.writable:
            raise ValueError(f"{self.name} register {row} is read-only or unused")
        with self.transport.lock:
            self.transport.write_register(self.chip, row, value)
            if self.verify:
                actual = self.read_register(row)
                if actual != value:
                    raise HMCError(f"{self.name} R{row}: wrote 0x{value:02X}, read 0x{actual:02X}. "
                                   "Check board power, chip selection, or reset motherboard buffers.")

    def update_bits(self, row, mask, value):
        integer(mask, 1, 255, "Mask")
        integer(value, 0, 255, "Field value")
        if value & ~mask:
            raise ValueError("Field value contains bits outside its mask")
        with self.transport.lock:
            self.write_register(row, (self.read_register(row) & ~mask) | value)

    def set_signal_path(self, enabled):
        """Gate the RF/IF chain; leave synthesizer and independent sensors alone.

        This sets every bit of TX ROW4 / RX ROW0. It is not full chip power-off.
        """
        if not isinstance(enabled, bool):
            raise ValueError("enabled must be a bool")
        self.write_register(self.power_row, 0 if enabled else 255)

    def load_profile(self, path=None, *, enable=False):
        """Program saved ADI settings; enable RF/IF only if explicitly requested.

        Parse everything before the first write. Skip documented unused rows.
        A write error may leave partial configuration, but the signal path is
        kept disabled until all other writes have been verified.
        """
        rows = load_profile(path or PROFILE_DIR / f"{self.name}.txt")
        with self.transport.lock:
            self.set_signal_path(False)
            for row in self.writable:
                if row != self.power_row:
                    self.write_register(row, rows[row])
            if enable:
                self.write_register(self.power_row, rows[self.power_row])

    def save_profile(self, path):
        with self.transport.lock:
            rows = {r: self.read_register(r) if r in self.writable else 0 for r in range(24)}
        text = "REGISTER_COUNT\t24\n" + "".join(f"REGISTER{r}\t{v}\n" for r, v in rows.items())
        Path(path).write_text(text, encoding="utf-8")

    def set_frequency(self, ghz, *, settle=0.005):
        """Tune internal synthesizer, search bands 0..19, verify center lock.

        Reference selection describes the physical oscillator; software cannot
        change the oscillator installed on the motherboard. Mutes RF/IF during
        tuning, restoring its prior gate on success. Failure leaves RF/IF muted.
        """
        target = frequency_hz(ghz)
        table = channels(self.reference)
        if target not in table:
            raise ValueError(f"{ghz} GHz is not a supported 57..64 GHz channel for "
                             f"the {self.reference} MHz reference")
        if not isinstance(settle, (float, int)) or not math.isfinite(settle) or settle < 0.001:
            raise ValueError("PLL settling time must be finite and at least 0.001 seconds")
        with self.transport.lock:
            old_gate = self.read_register(self.power_row)
            self.set_signal_path(False)
            self.update_bits(16, 0x36, 0x36)
            self.update_bits(17, 0xAB, 0xAB)
            narrow = REFERENCES[self.reference][4]
            self.update_bits(18, 0x56, 0x46 if narrow else 0x06)
            self.update_bits(20, 0x7F, table[target])
            runs, run = [], []
            for band in range(20):
                self.update_bits(22, 0x1F, band)
                time.sleep(settle)
                if self.read_register(24) & 8:
                    run.append(band)
                elif run:
                    runs.append(run)
                    run = []
            if run:
                runs.append(run)
            if not runs:
                raise HMCError(f"PLL did not lock at {ghz} GHz in any band; RF/IF remains disabled. "
                               "Check reference-clock selection and board power.")
            selected = max(runs, key=len)
            band = selected[len(selected) // 2]
            self.update_bits(22, 0x1F, band)
            time.sleep(settle)
            if not self.read_register(24) & 8:
                raise HMCError("PLL lost lock after band selection; RF/IF remains disabled")
            self.write_register(self.power_row, old_gate)
            return dict(frequency_ghz=target / 1e9, divider=table[target],
                        band=band, locked_bands=selected, locked=True)

    def set_if_attenuation(self, code):
        """Digital attenuation code, not calibrated dB; 0 is maximum gain."""
        integer(code, 0, 13 if self.chip == 6 else 15, "IF attenuation code")
        with self.transport.lock:
            if self.chip == 6:
                self.update_bits(10, 0x10, 0x10)
                self.update_bits(7, 0xF0, code << 4)
            else:
                self.update_bits(4, 1, 1)
                self.update_bits(5, 0xF0, code << 4)

    def set_temperature_sensor(self, enabled):
        if not isinstance(enabled, bool):
            raise ValueError("enabled must be a bool")
        with self.transport.lock:
            if self.chip == 6:
                self.update_bits(3, 1, int(enabled))
                self.update_bits(10, 1, int(not enabled))
            else:
                self.update_bits(9, 0x60, 0x20 if enabled else 0x40)

    def status(self):
        with self.transport.lock:
            reg = {r: self.read_register(r) for r in
                   set((self.power_row, 16, 17, 18, 20, 22, 24, 25, 26, 27))}
            mode = reg[18]
            frequency = None
            if not mode & 0x10 and bool(mode & 0x40) == REFERENCES[self.reference][4]:
                frequency = next((f / 1e9 for f, c in channels(self.reference).items()
                                  if c == (reg[20] & 0x7F)), None)
            return dict(chip=self.name, reference_mhz=self.reference,
                        signal_path_power_down=f"0x{reg[self.power_row]:02X}",
                        external_lo=bool(mode & 0x10), configured_frequency_ghz=frequency,
                        pll_locked=bool(reg[24] & 8), vco_band=reg[22] & 31,
                        vtune_positive=reg[25], vtune_negative=reg[26],
                        temperature_raw=reg[27] & 31)


class HMC6300(HMCDevice):
    def __init__(self, transport, **kwargs):
        super().__init__(transport, 6, **kwargs)

    def set_rf_attenuation(self, code):
        integer(code, 0, 15, "RF attenuation code")
        self.update_bits(11, 0xF8, code << 4)  # also select digital gain control

    def set_modulation(self, mode):
        if mode not in ("iq", "fsk"):
            raise ValueError("TX modulation must be iq or fsk (FSK/MSK inputs)")
        self.update_bits(10, 0x80, 0x80 if mode == "fsk" else 0)


class HMC6301(HMCDevice):
    def __init__(self, transport, **kwargs):
        super().__init__(transport, 7, **kwargs)

    def set_lna_attenuation(self, code):
        integer(code, 0, 3, "LNA attenuation code")
        with self.transport.lock:
            self.update_bits(9, 0x80, 0)
            self.update_bits(8, 0x18, code << 3)

    def set_baseband_attenuation(self, coarse1_db=0, coarse2_db=0, fine_i_db=0, fine_q_db=0):
        # RX baseband fields have reversed bit significance (see no-OS enums).
        coarse = {0: 0, 6: 2, 12: 1, 18: 3}
        fine = {0: 0, 1: 4, 2: 2, 3: 6, 4: 1, 5: 5}
        for value in (coarse1_db, coarse2_db):
            integer(value, 0, 18, "Coarse attenuation")
            if value not in coarse:
                raise ValueError("Coarse attenuation must be 0, 6, 12, or 18 dB")
        for value in (fine_i_db, fine_q_db):
            integer(value, 0, 5, "Fine attenuation")
        with self.transport.lock:
            self.update_bits(1, 0x0C, coarse[coarse1_db] << 2)
            self.write_register(2, (fine[fine_i_db] << 5) | (fine[fine_q_db] << 2) |
                                coarse[coarse2_db])

    def set_baseband_filters(self, lowpass_mhz=1400, highpass_khz=45):
        lowpass = {1400: 0, 500: 2, 300: 1, 200: 3}
        highpass = {45: 0, 350: 2, 1600: 1}
        if lowpass_mhz not in lowpass or highpass_khz not in highpass:
            raise ValueError("Low-pass: 1400/500/300/200 MHz; high-pass: 45/350/1600 kHz")
        self.update_bits(3, 0xF0, (lowpass[lowpass_mhz] << 6) | (highpass[highpass_khz] << 4))

    def set_demodulation(self, mode):
        if mode not in ("iq", "am", "fm", "am-fm"):
            raise ValueError("RX demodulation must be iq, am, fm, or am-fm")
        am, fm = mode in ("am", "am-fm"), mode in ("fm", "am-fm")
        with self.transport.lock:
            self.update_bits(1, 0x12, 2 if am else 0x10)
            self.update_bits(7, 3, 2 if fm else 1)
