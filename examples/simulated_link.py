"""Offline example: run from the repository with python -m examples.simulated_link."""
from hmc_control import HMC6300, HMC6301, SimulatedTransport


def main():
    with SimulatedTransport() as board:
        tx, rx = HMC6300(board), HMC6301(board)
        for device in (tx, rx):
            device.load_profile()
            print(device.name, device.set_frequency("60.0"))
        tx.set_if_attenuation(4)
        tx.set_rf_attenuation(8)
        rx.set_lna_attenuation(1)
        rx.set_baseband_attenuation(6, 0, 1, 1)
        rx.set_baseband_filters(500, 45)
        for device in (tx, rx):
            device.set_signal_path(True)
            print(device.status())
            device.set_signal_path(False)


if __name__ == "__main__":
    main()
