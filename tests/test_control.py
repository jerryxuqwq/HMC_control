import contextlib
import io
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from hmc_control import HMC6300, HMC6301, HMCError, SimulatedTransport, load_profile
from hmc_control.__main__ import main
from hmc_control.device import PROFILE_DIR, channels, frequency_hz
from hmc_control.transport import FtdiTransport, frame_waveform, frame_word


class ProtocolTests(unittest.TestCase):
    def test_golden_frames(self):
        # data 0xA5 | ROW20 << 8 | WRITE << 14 | TX=6 << 15
        self.assertEqual(frame_word(6, 20, 0xA5, write=True), 0x354A5)
        self.assertEqual(frame_word(7, 24), 0x39800)

    def test_write_clock_and_latch_edges(self):
        wave = frame_waveform(6, 20, 0xA5, write=True, auxiliary=0xC0)
        bits = []
        latches = 0
        for before, after in zip(wave, wave[1:]):
            self.assertEqual(after & 0xC0, 0xC0)
            self.assertEqual(after & 0x18, 0)  # readback pins never asserted
            if not before & 1 and after & 1:
                self.assertFalse(after & 4)
                self.assertEqual(before & 2, after & 2)  # setup time
                bits.append((after >> 1) & 1)
            if not before & 4 and after & 4:
                self.assertFalse(before & 1)
                latches += 1
        self.assertEqual(bits, [1, 0, 1, 0, 0, 1, 0, 1, 0, 0, 1, 0, 1, 0, 1, 0, 1, 1])
        self.assertEqual(latches, 1)
        self.assertEqual(wave[-1], 0xC4)

    def test_read_turnaround_and_scanout_selection(self):
        for chip, pin in ((6, 4), (7, 3)):
            bus = FtdiTransport.__new__(FtdiTransport)
            bus.lock, bus.auxiliary = threading.RLock(), 0x40
            output = []
            bus._write = lambda data: output.extend(data)
            samples = iter([((0xA6 >> b) & 1) << pin for b in range(8)])
            bus._pins = lambda: next(samples)
            self.assertEqual(bus.read_register(chip, 24), 0xA6)
            edges = [(before, after) for before, after in zip(output, output[1:])
                     if not before & 1 and after & 1]
            self.assertEqual(len(edges), 27)
            self.assertTrue(edges[18][1] & 4)  # 19th clock ENABLE high
            self.assertTrue(all(not after & 4 for _, after in edges[19:]))
            self.assertEqual(output[-1], 0x44)

    def test_invalid_frame_arguments(self):
        for args in ((5, 0), (6, -1), (7, 32), (6, 0, 256)):
            with self.assertRaises(ValueError):
                frame_word(*args)


class DeviceTests(unittest.TestCase):
    def setUp(self):
        self.bus = SimulatedTransport()
        self.tx, self.rx = HMC6300(self.bus), HMC6301(self.bus)

    def test_profiles_preserved_and_load_muted(self):
        tx_profile = load_profile(PROFILE_DIR / "HMC6300.txt")
        self.assertEqual([tx_profile[r] for r in (1, 2, 9, 20, 22)], [74, 241, 224, 48, 8])
        self.tx.load_profile()
        self.assertEqual(self.tx.read_register(4), 255)
        self.assertEqual(self.bus.writes[0], (6, 4, 255))
        self.assertTrue(all(r not in (0, 13, 14, 15) for _, r, _ in self.bus.writes))
        self.rx.load_profile(enable=True)
        self.assertEqual(self.rx.read_register(0), 0)

    def test_invalid_profile_has_no_side_effects(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "bad.txt"
            for content in ("REGISTER_COUNT 24\nREGISTER0 256", "REGISTER_COUNT 1\nREGISTER0 0",
                            "REGISTER_COUNT 24\nREGISTER0 0\nREGISTER0 1"):
                path.write_text(content)
                with self.assertRaises(ValueError):
                    self.tx.load_profile(path)
                self.assertEqual(self.bus.writes, [])

    def test_profile_round_trip(self):
        self.rx.load_profile()
        self.rx.set_baseband_attenuation(6, 12, 3, 4)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "rx.txt"
            self.rx.save_profile(path)
            saved = load_profile(path)
            self.assertEqual(saved[1], 0x18)
            self.assertEqual(saved[2], 0xC5)

    def test_read_only_and_unused_rows(self):
        for dev, rows in ((self.tx, [0, 13, 14, 15, 24, 27, 31]),
                          (self.rx, [10, 15, 24, 28])):
            for row in rows:
                with self.assertRaises(ValueError):
                    dev.write_register(row, 0)

    def test_bad_readback_is_error(self):
        self.bus.read_register = lambda chip, row: 0
        with self.assertRaisesRegex(HMCError, "wrote 0x4A, read 0x00"):
            self.tx.write_register(1, 74)

    def test_gain_preserves_bias_bits_and_sets_digital(self):
        self.tx.load_profile()
        self.tx.set_if_attenuation(13)
        self.tx.set_rf_attenuation(15)
        self.assertEqual(self.tx.read_register(7), 0xDF)
        self.assertEqual(self.tx.read_register(11), 0xF3)
        self.assertEqual(self.tx.read_register(10) & 16, 16)
        self.rx.load_profile()
        self.rx.set_lna_attenuation(3)
        self.rx.set_if_attenuation(15)
        self.assertEqual(self.rx.read_register(8), 0x98)
        self.assertEqual(self.rx.read_register(5), 0xFF)

    def test_rx_reversed_baseband_codes(self):
        self.rx.load_profile()
        self.rx.set_baseband_attenuation(6, 12, 1, 4)
        self.assertEqual(self.rx.read_register(1), 0x18)
        self.assertEqual(self.rx.read_register(2), 0x85)
        self.rx.set_baseband_filters(500, 350)
        self.assertEqual(self.rx.read_register(3), 0xA3)
        before = list(self.bus.writes)
        with self.assertRaises(ValueError):
            self.rx.set_baseband_attenuation(6, 18, 2, 7)
        self.assertEqual(self.bus.writes, before)

    def test_mode_preserves_other_fields(self):
        self.rx.load_profile()
        self.rx.set_baseband_attenuation(6)
        self.rx.set_demodulation("am-fm")
        self.assertEqual(self.rx.read_register(1), 0x0A)
        self.assertEqual(self.rx.read_register(7), 0x6E)
        self.rx.set_demodulation("iq")
        self.assertEqual(self.rx.read_register(1), 0x18)
        self.assertEqual(self.rx.read_register(7), 0x6D)

    def test_temperature_encoding_and_sensor_enables(self):
        self.tx.load_profile()
        self.rx.load_profile()
        self.tx.set_temperature_sensor(True)
        self.rx.set_temperature_sensor(True)
        self.assertEqual(self.tx.read_register(3), 0xF7)
        self.assertEqual(self.tx.read_register(10), 0x52)
        self.assertEqual(self.rx.read_register(9), 0x20)

    @patch("hmc_control.device.time.sleep")
    def test_tune_selects_middle_and_restores_gate(self, _):
        self.tx.load_profile(enable=True)
        result = self.tx.set_frequency("60")
        self.assertEqual(result["band"], 8)
        self.assertEqual(result["divider"], 0x30)
        self.assertEqual(self.tx.read_register(4), 0)
        self.assertTrue(self.tx.status()["pll_locked"])
        self.assertEqual(self.tx.status()["configured_frequency_ghz"], 60.0)

    @patch("hmc_control.device.time.sleep")
    def test_500mhz_and_540mhz_references(self, _):
        for ref, freq, divider in (("142.8571", "60", 24), ("154.2857", "60.48", 16)):
            dev = HMC6301(self.bus, reference=ref)
            dev.load_profile()
            self.assertEqual(dev.set_frequency(freq)["divider"], divider)
            self.assertEqual(dev.read_register(18) & 0x56, 6)

    @patch("hmc_control.device.time.sleep")
    def test_no_lock_leaves_signal_path_disabled(self, _):
        self.tx.load_profile(enable=True)
        self.bus.lock_bands.clear()
        with self.assertRaisesRegex(HMCError, "did not lock"):
            self.tx.set_frequency("60")
        self.assertEqual(self.tx.read_register(4), 255)

    def test_invalid_frequency_no_writes(self):
        for frequency in ("56.5", "65", "60.1", "nan", "inf"):
            with self.assertRaises(ValueError):
                self.tx.set_frequency(frequency)
        self.assertEqual(self.bus.writes, [])
        self.assertEqual(frequency_hz("60.25"), 60_250_000_000)
        self.assertEqual(channels("71.42857")[64_000_000_000], 0x40)

    def test_independent_chips_on_one_bus(self):
        self.tx.write_register(20, 48)
        self.rx.write_register(20, 50)
        self.assertEqual(self.tx.read_register(20), 48)
        self.assertEqual(self.rx.read_register(20), 50)

    def test_closed_connection_errors(self):
        self.bus.close()
        with self.assertRaises(HMCError):
            self.tx.read_register(20)


class CliTests(unittest.TestCase):
    @patch("hmc_control.device.time.sleep")
    def test_simulated_configure(self, _):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(main(["--simulate", "--chip", "rx", "configure", "60", "--enable"]), 0)
        self.assertIn('"simulated": true', output.getvalue())
        self.assertIn('"locked": true', output.getvalue())

    def test_cli_errors_return_nonzero(self):
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(["--simulate", "--chip", "rx", "gain", "--rf-code", "2"]), 1)
            self.assertEqual(main(["--simulate", "write", "24", "0"]), 1)


if __name__ == "__main__":
    unittest.main()
