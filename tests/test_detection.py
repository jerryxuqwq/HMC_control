import unittest

from hmc_control import HMC6300, HMC6301, HMCError, SimulatedTransport
from hmc_control.detection import detect_modules


class DetectionTests(unittest.TestCase):
    def test_zero_opposite_ff_identifies_likely_module_without_writes(self):
        for zero_address, expected in ((6, "tx"), (7, "rx")):
            with self.subTest(expected=expected):
                bus = SimulatedTransport()
                bus.read_register = lambda chip, row: 0 if chip == zero_address else 255
                result = detect_modules(bus)
                self.assertEqual(result["kind"], expected)
                self.assertIn("contrast", result["method"])
                self.assertTrue(result["probes"][expected]["likely"])
                self.assertEqual(bus.writes, [])

    def test_unstable_zero_ff_contrast_is_inconclusive(self):
        bus = SimulatedTransport()
        seen = set()
        def read(chip, row):
            key = chip, row
            value = (0 if key not in seen else 1) if chip == 7 else 255
            seen.add(key)
            return value
        bus.read_register = read
        self.assertEqual(detect_modules(bus)["kind"], "unknown")

    def test_tx_rx_both_and_no_writes(self):
        for chips, expected in (((HMC6300,), "tx"), ((HMC6301,), "rx"),
                                ((HMC6300, HMC6301), "both")):
            with self.subTest(expected=expected):
                bus = SimulatedTransport()
                for cls in chips:
                    cls(bus).load_profile()
                before = list(bus.writes)
                self.assertEqual(detect_modules(bus)["kind"], expected)
                self.assertEqual(bus.writes, before)

    def test_constant_readback_is_not_identification(self):
        for value in (0, 255, 0xBF):
            bus = SimulatedTransport()
            bus.read_register = lambda *args: value
            self.assertEqual(detect_modules(bus)["kind"], "unknown")

    def test_gain_and_frequency_changes_do_not_change_identity(self):
        bus = SimulatedTransport()
        tx = HMC6300(bus)
        tx.load_profile()
        tx.set_if_attenuation(13)
        tx.set_rf_attenuation(15)
        tx.write_register(20, 0x40)
        tx.write_register(22, 19)
        self.assertEqual(detect_modules(bus)["kind"], "tx")

    def test_unstable_fingerprint_is_rejected(self):
        bus = SimulatedTransport()
        HMC6300(bus).load_profile()
        original = bus.read_register
        seen = set()
        def read(chip, row):
            key = (chip, row)
            value = original(chip, row)
            if key in seen:
                value ^= 1
            seen.add(key)
            return value
        bus.read_register = read
        result = detect_modules(bus)
        self.assertEqual(result["kind"], "unknown")
        self.assertEqual(result["probes"]["tx"]["reason"], "Unstable readback")

    def test_read_failure_does_not_select_other_chip(self):
        bus = SimulatedTransport()
        HMC6300(bus).load_profile()
        original = bus.read_register
        def read(chip, row):
            if chip == 7:
                raise HMCError("Disconnected")
            return original(chip, row)
        bus.read_register = read
        result = detect_modules(bus)
        self.assertEqual(result["kind"], "unknown")
        self.assertIn("error", result["probes"]["rx"])
