"""Tkinter panel; all USB I/O runs on one worker, never on the UI thread."""
import json
import queue
from concurrent.futures import ThreadPoolExecutor
import tkinter as tk
from tkinter import filedialog, ttk

from .device import HMC6300, HMC6301, REFERENCES
from .transport import D2XX, FtdiTransport, SimulatedTransport
from .detection import detect_modules


def launch(args):
    root = tk.Tk()
    root.title("HMC6300 / HMC6301 Control")
    root.geometry("900x700")
    panel = ControlPanel(root, args)
    root.protocol("WM_DELETE_WINDOW", panel.close)
    root.mainloop()


class ControlPanel:
    def __init__(self, root, args):
        self.root, self.args = root, args
        self.bus = None
        self.busy = False
        self.closing = False
        self.executor = ThreadPoolExecutor(max_workers=1)
        self.results = queue.Queue()
        self.completion = None
        self.simulate = tk.BooleanVar(value=args.simulate)
        self.serial = tk.StringVar(value=args.serial or "")
        self.chip = tk.StringVar(value=args.chip)
        self.reference = tk.StringVar(value=args.reference)
        self.frequency = tk.StringVar(value="60.0")
        self.if_code, self.rf_code, self.lna_code = (tk.StringVar(value="0") for _ in range(3))
        self.row, self.value = tk.StringVar(value="20"), tk.StringVar(value="0x30")
        self.message = tk.StringVar(value="Disconnected. Choose simulation or a USB serial number.")
        frame = ttk.Frame(root, padding=16)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="60 GHz radio control", font=("Segoe UI", 19, "bold")).pack(anchor="w")
        connection = ttk.LabelFrame(frame, text="Connection", padding=10)
        connection.pack(fill="x", pady=10)
        self.simulation_control = ttk.Checkbutton(connection, text="Simulation", variable=self.simulate)
        self.simulation_control.grid(row=0, column=0)
        ttk.Label(connection, text="FTDI serial").grid(row=0, column=1, padx=8)
        self.serial_control = ttk.Combobox(connection, textvariable=self.serial, width=22)
        self.serial_control.grid(row=0, column=2)
        ttk.Button(connection, text="List USB", command=self.list_usb).grid(row=0, column=3, padx=5)
        ttk.Button(connection, text="Connect", command=self.connect).grid(row=0, column=4)
        ttk.Button(connection, text="Disconnect", command=self.disconnect).grid(row=0, column=5, padx=5)
        self.session = tk.StringVar(value="No active connection")
        ttk.Label(connection, textvariable=self.session).grid(row=1, column=0, columnspan=6, sticky="w", pady=(8, 0))
        settings = ttk.Frame(frame)
        settings.pack(fill="x")
        ttk.Label(settings, text="Chip").pack(side="left")
        ttk.Combobox(settings, textvariable=self.chip, values=("tx", "rx"), state="readonly", width=5).pack(side="left", padx=8)
        ttk.Label(settings, text="Physical reference (MHz)").pack(side="left")
        ttk.Combobox(settings, textvariable=self.reference, values=tuple(REFERENCES), state="readonly", width=13).pack(side="left", padx=8)
        ttk.Button(settings, text="Read status", command=lambda: self.action("status")).pack(side="left")
        ttk.Button(settings, text="Detect TX/RX", command=self.detect).pack(side="left", padx=8)
        self.detection = tk.StringVar(value="Module detection: not run")
        ttk.Label(frame, textvariable=self.detection, wraplength=850).pack(anchor="w", pady=(5, 0))
        radio = ttk.LabelFrame(frame, text="Initialize and tune", padding=10)
        radio.pack(fill="x", pady=10)
        ttk.Button(radio, text="Load bundled profile (RF/IF off)", command=lambda: self.action("load_profile")).grid(row=0, column=0, padx=4)
        ttk.Entry(radio, textvariable=self.frequency, width=10).grid(row=0, column=1, padx=5)
        ttk.Label(radio, text="GHz").grid(row=0, column=2)
        ttk.Button(radio, text="Tune + check lock", command=lambda: self.action("set_frequency", self.frequency.get())).grid(row=0, column=3, padx=5)
        ttk.Button(radio, text="RF/IF on", command=lambda: self.action("set_signal_path", True)).grid(row=1, column=0, pady=8)
        ttk.Button(radio, text="RF/IF off", command=lambda: self.action("set_signal_path", False)).grid(row=1, column=1, columnspan=2)
        ttk.Button(radio, text="Reset board buffers", command=self.reset_buffers).grid(row=1, column=3)
        gains = ttk.LabelFrame(frame, text="Digital attenuation codes (0 = highest gain)", padding=10)
        gains.pack(fill="x")
        for col, (label, variable, method) in enumerate((
            ("IF", self.if_code, "set_if_attenuation"),
            ("TX RF", self.rf_code, "set_rf_attenuation"),
            ("RX LNA", self.lna_code, "set_lna_attenuation"))):
            ttk.Label(gains, text=label).grid(row=0, column=col * 3)
            ttk.Entry(gains, textvariable=variable, width=6).grid(row=0, column=col * 3 + 1, padx=5)
            ttk.Button(gains, text="Set", command=lambda v=variable, m=method: self.numeric_action(m, v)).grid(row=0, column=col * 3 + 2, padx=8)
        registers = ttk.LabelFrame(frame, text="Registers and profile files", padding=10)
        registers.pack(fill="x", pady=10)
        ttk.Label(registers, text="Row").pack(side="left")
        ttk.Entry(registers, textvariable=self.row, width=5).pack(side="left", padx=5)
        ttk.Label(registers, text="Value").pack(side="left")
        ttk.Entry(registers, textvariable=self.value, width=8).pack(side="left", padx=5)
        ttk.Button(registers, text="Read", command=lambda: self.register(False)).pack(side="left", padx=4)
        ttk.Button(registers, text="Write + verify", command=lambda: self.register(True)).pack(side="left", padx=4)
        ttk.Button(registers, text="Load file", command=self.load_file).pack(side="left", padx=4)
        ttk.Button(registers, text="Save file", command=self.save_file).pack(side="left", padx=4)
        ttk.Button(registers, text="All registers…", command=self.open_registers).pack(side="left", padx=4)
        ttk.Label(frame, textvariable=self.message, wraplength=850).pack(anchor="w", pady=(0, 8))
        self.log = tk.Text(frame, height=14, wrap="word", state="disabled")
        self.log.pack(fill="both", expand=True)
        self.root.after(100, self.poll)

    def submit(self, label, fn, on_complete=None):
        if self.busy or self.closing:
            self.message.set("An operation is running; wait for it to finish.")
            return False
        self.busy = True
        self.completion = on_complete
        self.message.set(label + "...")
        def work():
            try:
                result = fn()
                self.results.put((label, result, None))
            except Exception as exc:
                self.results.put((label, None, str(exc)))
        self.executor.submit(work)
        return True

    def poll(self):
        try:
            label, result, error = self.results.get_nowait()
            self.busy = False
            text = f"{label}: " + (f"ERROR: {error}" if error else json.dumps(result, indent=2))
            self.message.set(text.splitlines()[0])
            self.log.configure(state="normal")
            self.log.insert("end", text + "\n")
            self.log.see("end")
            self.log.configure(state="disabled")
            active = self.bus is not None
            for widget in (self.simulation_control, self.serial_control):
                widget.state(["disabled"] if active else ["!disabled"])
            self.session.set("SIMULATION — no USB access" if isinstance(self.bus, SimulatedTransport)
                             else "LIVE USB — actions control hardware" if active else "No active connection")
            callback, self.completion = self.completion, None
            if callback:
                callback(result, error)
        except queue.Empty:
            pass
        self.root.after(100, self.poll)

    def list_usb(self):
        def populate(devices, error):
            if error:
                return
            serials = tuple(dict.fromkeys(device["serial"] for device in devices if device.get("serial")))
            self.serial_control.configure(values=serials)
            if len(serials) == 1 and not self.serial.get().strip() and self.bus is None:
                self.serial.set(serials[0])
            self.message.set(f"Found {len(serials)} FTDI serial number(s). Select a board from the dropdown."
                             if serials else "No FTDI serial numbers found. Check USB and board power, then click List USB again.")
        self.submit("USB interfaces", lambda: D2XX(self.args.dll, self.args.install_dir).devices(), populate)

    def open_registers(self):
        from .register_editor import RegisterEditor
        if self.bus is None:
            self.message.set("Connect first, then open All registers.")
            return
        return RegisterEditor(self, self.chip.get())

    def connect(self):
        simulate, serial = self.simulate.get(), self.serial.get().strip()
        def work():
            if self.bus is not None:
                raise ValueError("Disconnect before changing the connection")
            if not simulate and not serial and self.args.index is None:
                raise ValueError("List USB interfaces and enter the board's FTDI serial number")
            self.bus = (SimulatedTransport() if simulate else FtdiTransport(
                serial=serial or None, index=None if serial else self.args.index,
                dll_path=self.args.dll, install_dir=self.args.install_dir))
            return detect_modules(self.bus)
        self.submit("Connect and detect TX/RX", work, self.show_detection)

    def show_detection(self, result, error):
        if error:
            self.detection.set("Module detection failed; select TX/RX manually.")
            return
        kind = result["kind"]
        prefix = "Simulation: " if isinstance(self.bus, SimulatedTransport) else ""
        if kind in ("tx", "rx"):
            self.chip.set(kind)
            evidence = ("zero/0xFF contrast" if "contrast" in result["method"] else "register fingerprint")
            text = f"Likely {kind.upper()} detected — selected automatically ({evidence})."
        elif kind == "both":
            text = "Both TX and RX fingerprints detected — choose the module to control."
        else:
            text = "Detection inconclusive — select TX/RX manually. Check power/buffers or retry after initialization."
        self.detection.set(prefix + text)
        self.message.set(prefix + text)

    def detect(self):
        def work():
            if self.bus is None:
                raise ValueError("Connect first")
            return detect_modules(self.bus)
        self.submit("Detect TX/RX", work, self.show_detection)

    def disconnect(self):
        def work():
            if self.bus is not None:
                self.bus.close()
                self.bus = None
            return "Disconnected; hardware retains its register settings"
        self.submit("Disconnect", work, lambda result, error: self.detection.set("Module detection: not run"))

    def action(self, method, *values):
        chip, reference = self.chip.get(), self.reference.get()
        def work():
            if self.bus is None:
                raise ValueError("Connect first")
            device = (HMC6300 if chip == "tx" else HMC6301)(self.bus, reference=reference)
            if not hasattr(device, method):
                raise ValueError(f"{method} is not available on {device.name}")
            result = getattr(device, method)(*values)
            return result if result is not None else "Completed and register writes verified"
        self.submit(f"{chip.upper()} {method}", work)

    def numeric_action(self, method, variable):
        try:
            value = int(variable.get())
        except ValueError:
            self.message.set("Enter an integer attenuation code")
            return
        self.action(method, value)

    def register(self, write):
        from .__main__ import number
        try:
            row = number(self.row.get())
            value = number(self.value.get()) if write else None
        except ValueError:
            self.message.set("Use decimal integers or 0x-prefixed hexadecimal")
            return
        self.action("write_register", row, value) if write else self.action("read_register", row)

    def load_file(self):
        path = filedialog.askopenfilename(filetypes=[("ADI register profiles", "*.txt")])
        if path:
            self.action("load_profile", path)

    def save_file(self):
        path = filedialog.asksaveasfilename(defaultextension=".txt", filetypes=[("ADI register profiles", "*.txt")])
        if path:
            self.action("save_profile", path)

    def reset_buffers(self):
        def work():
            if self.bus is None:
                raise ValueError("Connect first")
            self.bus.reset_buffers()
            return "Motherboard buffers reset (affects TX and RX)"
        self.submit("Reset buffers", work)

    def close(self):
        if self.closing:
            return
        self.closing = True
        self.message.set("Finishing the current operation and closing USB...")
        def cleanup():
            if self.bus is not None:
                self.bus.close()
                self.bus = None
        future = self.executor.submit(cleanup)
        def finish():
            if not future.done():
                self.root.after(100, finish)
                return
            self.executor.shutdown(wait=False)
            self.root.destroy()
        finish()
