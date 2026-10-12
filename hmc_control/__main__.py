"""Run with python -m hmc_control --help."""
import argparse
import json
import sys

from .device import HMC6300, HMC6301, REFERENCES, channels
from .transport import ADI_DIRECTORY, D2XX, FtdiTransport, HMCError, SimulatedTransport


def number(text):
    return int(text, 16) if text.lower().startswith("0x") else int(text, 10)


def make_parser():
    parser = argparse.ArgumentParser(description="HMC6300 TX / HMC6301 RX control for ADI EK1HMC6350")
    parser.add_argument("--simulate", action="store_true", help="Use an in-memory board; no USB access")
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--serial", help="FTDI serial number (recommended)")
    selection.add_argument("--index", type=int, help="Explicit FTDI index from devices command")
    parser.add_argument("--dll", help="Override D2XX DLL path, matching Python bitness")
    parser.add_argument("--install-dir", default=str(ADI_DIRECTORY))
    parser.add_argument("--chip", choices=("tx", "rx"), default="tx")
    parser.add_argument("--reference", choices=tuple(REFERENCES), default="71.42857",
                        help="Physical reference oscillator in MHz")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("devices", help="List FTDI interfaces without opening them")
    commands.add_parser("channels", help="List supported RF frequencies for this reference")
    commands.add_parser("status", help="Read PLL state and configuration; does not initialize")
    commands.add_parser("detect", help="Read-only best-effort TX/RX module detection")
    commands.add_parser("dump", help="Read documented registers")
    commands.add_parser("gui", help="Open the desktop control panel")
    read = commands.add_parser("read", help="Read one register")
    read.add_argument("row", type=number)
    write = commands.add_parser("write", help="Write one documented writable register and verify")
    write.add_argument("row", type=number)
    write.add_argument("value", type=number)
    load = commands.add_parser("load", help="Load an ADI register file (or bundled profile)")
    load.add_argument("path", nargs="?")
    load.add_argument("--enable", action="store_true", help="Apply profile's RF/IF gate after loading")
    save = commands.add_parser("save", help="Save current registers in ADI GUI format")
    save.add_argument("path")
    tune = commands.add_parser("tune", help="Tune initialized board and find PLL lock")
    tune.add_argument("ghz")
    gate = commands.add_parser("path", help="Enable/disable RF/IF signal path, leaving PLL unchanged")
    gate.add_argument("state", choices=("on", "off"))
    gain = commands.add_parser("gain", help="Set digital attenuation codes (0=highest gain)")
    gain.add_argument("--if-code", type=int)
    gain.add_argument("--rf-code", type=int, help="TX only, 0..15")
    gain.add_argument("--lna-code", type=int, help="RX only, 0..3")
    bb = commands.add_parser("baseband", help="RX coarse/fine attenuation in dB")
    bb.add_argument("--coarse1", type=int, default=0)
    bb.add_argument("--coarse2", type=int, default=0)
    bb.add_argument("--fine-i", type=int, default=0)
    bb.add_argument("--fine-q", type=int, default=0)
    filt = commands.add_parser("filters", help="RX baseband corner frequencies")
    filt.add_argument("--lowpass", type=int, choices=(1400, 500, 300, 200), default=1400)
    filt.add_argument("--highpass", type=int, choices=(45, 350, 1600), default=45)
    mode = commands.add_parser("mode", help="TX: iq/fsk; RX: iq/am/fm/am-fm")
    mode.add_argument("mode", choices=("iq", "fsk", "am", "fm", "am-fm"))
    temp = commands.add_parser("temperature", help="Enable/disable temperature sensor")
    temp.add_argument("state", choices=("on", "off"))
    commands.add_parser("reset-buffers", help="Reset motherboard interface buffers; affects both chips")
    configure = commands.add_parser("configure", help="Load profile, tune, optionally enable signal path")
    configure.add_argument("ghz")
    configure.add_argument("--profile")
    configure.add_argument("--enable", action="store_true")
    return parser


def execute(args, device):
    cmd = args.command
    if cmd == "detect":
        from .detection import detect_modules
        return detect_modules(device.transport)
    if cmd == "status":
        return device.status()
    if cmd == "dump":
        return {f"R{r:02d}": f"0x{device.read_register(r):02X}"
                for r in (*device.writable, 24, 25, 26, 27)}
    if cmd == "read":
        value = device.read_register(args.row)
        return dict(register=args.row, value=value, hex=f"0x{value:02X}")
    if cmd == "write":
        device.write_register(args.row, args.value)
    elif cmd == "load":
        device.load_profile(args.path, enable=args.enable)
    elif cmd == "save":
        device.save_profile(args.path)
    elif cmd == "tune":
        return device.set_frequency(args.ghz)
    elif cmd == "configure":
        # Validate frequency before applying a profile.
        from .device import frequency_hz
        if frequency_hz(args.ghz) not in channels(device.reference):
            raise ValueError("Unsupported frequency; use the channels command")
        device.load_profile(args.profile)
        result = device.set_frequency(args.ghz)
        if args.enable:
            device.set_signal_path(True)
        return dict(**result, signal_path_enabled=args.enable)
    elif cmd == "path":
        device.set_signal_path(args.state == "on")
    elif cmd == "gain":
        from .transport import integer
        if args.rf_code is not None and args.chip != "tx":
            raise ValueError("--rf-code requires --chip tx")
        if args.lna_code is not None and args.chip != "rx":
            raise ValueError("--lna-code requires --chip rx")
        if all(v is None for v in (args.if_code, args.rf_code, args.lna_code)):
            raise ValueError("Specify at least one attenuation code")
        for value, high, name in ((args.if_code, 13 if args.chip == "tx" else 15, "IF"),
                                  (args.rf_code, 15, "RF"), (args.lna_code, 3, "LNA")):
            if value is not None:
                integer(value, 0, high, name)
        if args.if_code is not None:
            device.set_if_attenuation(args.if_code)
        if args.rf_code is not None:
            device.set_rf_attenuation(args.rf_code)
        if args.lna_code is not None:
            device.set_lna_attenuation(args.lna_code)
    elif cmd in ("baseband", "filters"):
        if args.chip != "rx":
            raise ValueError(f"{cmd} requires --chip rx")
        if cmd == "baseband":
            device.set_baseband_attenuation(args.coarse1, args.coarse2, args.fine_i, args.fine_q)
        else:
            device.set_baseband_filters(args.lowpass, args.highpass)
    elif cmd == "mode":
        if args.chip == "tx":
            device.set_modulation(args.mode)
        else:
            device.set_demodulation(args.mode)
    elif cmd == "temperature":
        device.set_temperature_sensor(args.state == "on")
    elif cmd == "reset-buffers":
        device.transport.reset_buffers()
    return dict(chip=device.name, operation=cmd, completed=True)


def main(argv=None):
    parser = make_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "gui":
            from .gui import launch
            launch(args)
            return 0
        if args.command == "devices":
            result = ([dict(index=0, serial="SIMULATED", description="In-memory board")]
                      if args.simulate else D2XX(args.dll, args.install_dir).devices())
        elif args.command == "channels":
            result = [f / 1e9 for f in channels(args.reference)]
        else:
            if not args.simulate and args.serial is None and args.index is None:
                parser.error("Hardware access requires --serial or --index; run devices first")
            bus = (SimulatedTransport() if args.simulate else FtdiTransport(
                serial=args.serial, index=args.index, dll_path=args.dll, install_dir=args.install_dir))
            with bus:
                cls = HMC6300 if args.chip == "tx" else HMC6301
                result = execute(args, cls(bus, reference=args.reference))
        print(json.dumps(dict(simulated=args.simulate, result=result), indent=2))
        return 0
    except (HMCError, OSError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

