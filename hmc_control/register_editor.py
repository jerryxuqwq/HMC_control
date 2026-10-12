"""Full register grid. Edits are staged; only explicit Write actions send bytes."""
import tkinter as tk
from tkinter import ttk

from .device import HMC6300, HMC6301


COMMON_NAMES = {
    16: "Synthesizer / charge pump enables", 17: "Reference / feedback / lock detector",
    18: "VCO / external LO / channel spacing", 19: "Synthesizer reference selection",
    20: "PLL feedback divider", 21: "VCO bias trim", 22: "VCO band selection",
    23: "Charge pump / VCO swing", 24: "PLL lock / VCO monitor",
    25: "Vtune positive monitor", 26: "Vtune negative monitor", 27: "Temperature code",
}
TX_NAMES = {
    1: "PA regulator / bias", 2: "PA interface / power detector",
    3: "Driver bias / IF mixer / temperature", 4: "RF/IF power-down gates",
    5: "Tripler bias (high)", 6: "Tripler bias (low)", 7: "IF attenuation / tuning",
    8: "IF bias / upmixer tuning", 9: "IF filter Q", 10: "Modulation / digital gain / references",
    11: "RF attenuation / analog control / bias", 12: "Upmixer calibration",
}
RX_NAMES = {
    0: "RF/IF power-down gates", 1: "Baseband coarse 1 / AM / power controls",
    2: "Baseband I/Q fine / coarse 2", 3: "Baseband filter corners / reference",
    4: "IF bias / filter / digital gain", 5: "IF attenuation / mixer tuning",
    6: "Tripler bias (high)", 7: "Tripler bias (low) / FM",
    8: "LNA bias / gain / IF filter Q", 9: "LNA analog control / temperature",
}


def parse_byte(text):
    """Grid uses hex consistently; optional 0x prefix accepted."""
    text = text.strip()
    if text.lower().startswith("0x"):
        text = text[2:]
    if not 1 <= len(text) <= 2 or any(c not in "0123456789abcdefABCDEF" for c in text):
        raise ValueError("Enter one or two hexadecimal digits (00–FF)")
    return int(text, 16)


class RegisterEditor:
    def __init__(self, panel, chip):
        self.panel, self.bus, self.chip = panel, panel.bus, chip
        self.device = (HMC6300 if chip == "tx" else HMC6301)(self.bus)
        self.window = tk.Toplevel(panel.root)
        self.window.title(f"{self.device.name} — All registers")
        self.window.geometry("1160x780")
        self.window.minsize(1000, 450)
        self.values, self.bits, self.readbacks, self.states, self.decimals = {}, {}, {}, {}, {}
        self.last_read = {}
        self.edit_controls, self.buttons = [], []
        self.frozen = False
        self.notice = tk.StringVar(value="Read all to inspect the chip. Edit hex bytes or bits, then Write. Values are not sent automatically.")
        top = ttk.Frame(self.window, padding=12)
        top.pack(fill="x")
        ttk.Label(top, text=f"{self.device.name} ({chip.upper()})", font=("Segoe UI", 15, "bold")).pack(side="left")
        for label, command in (("Read all (replace edits)", self.read_all),
                               ("Write changed + verify", self.write_changed),
                               ("Discard edits", self.discard)):
            button = ttk.Button(top, text=label, command=command)
            button.pack(side="left", padx=8)
            self.buttons.append(button)
        ttk.Label(self.window, textvariable=self.notice, wraplength=1100, padding=(12, 0, 12, 8)).pack(fill="x")
        body = ttk.Frame(self.window)
        body.pack(fill="both", expand=True, padx=12, pady=4)
        canvas = tk.Canvas(body, highlightthickness=0)
        scroll = ttk.Scrollbar(body, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        grid = ttk.Frame(canvas)
        item = canvas.create_window((0, 0), window=grid, anchor="nw")
        grid.bind("<Configure>", lambda _: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda event: canvas.itemconfigure(item, width=event.width))
        self.window.bind("<MouseWheel>", lambda event: canvas.yview_scroll(-int(event.delta / 120), "units"))
        headers = ["Row", "Function", "Access", "Last read", "Edit hex", "Dec"] + [f"b{b}" for b in range(7, -1, -1)] + ["State", "Actions"]
        for col, title in enumerate(headers):
            ttk.Label(grid, text=title, font=("Segoe UI", 9, "bold")).grid(row=0, column=col, padx=4, pady=6)
        grid.columnconfigure(1, weight=1)
        names = {**COMMON_NAMES, **(TX_NAMES if chip == "tx" else RX_NAMES)}
        for row in range(32):
            writable = row in self.device.writable
            access = "R/W" if writable else "RO" if row in range(24, 28) else "Unused"
            line = row + 1
            ttk.Label(grid, text=f"{row:02d} / {row:02X}").grid(row=line, column=0, padx=4, pady=5)
            ttk.Label(grid, text=names.get(row, "Unused"), wraplength=255).grid(row=line, column=1, sticky="w", padx=4)
            ttk.Label(grid, text=access).grid(row=line, column=2)
            self.values[row] = tk.StringVar(value="")
            self.readbacks[row] = tk.StringVar(value="—")
            self.decimals[row] = tk.StringVar(value="—")
            self.states[row] = tk.StringVar(value="Not read")
            ttk.Label(grid, textvariable=self.readbacks[row], width=7).grid(row=line, column=3)
            entry = ttk.Entry(grid, textvariable=self.values[row], width=5)
            entry.grid(row=line, column=4, padx=4)
            ttk.Label(grid, textvariable=self.decimals[row], width=4).grid(row=line, column=5)
            self.bits[row] = [tk.IntVar(value=0) for _ in range(8)]
            controls = [entry]
            for col, bit in enumerate(range(7, -1, -1), 6):
                check = ttk.Checkbutton(grid, variable=self.bits[row][bit],
                                        command=lambda r=row, b=bit: self.toggle_bit(r, b))
                check.grid(row=line, column=col, padx=1)
                controls.append(check)
            if writable:
                self.edit_controls.extend(controls)
            else:
                for control in controls:
                    control.state(["disabled"])
            ttk.Label(grid, textvariable=self.states[row], width=10).grid(row=line, column=14, padx=4)
            actions = ttk.Frame(grid)
            actions.grid(row=line, column=15, padx=4)
            read = ttk.Button(actions, text="Read", width=5, command=lambda r=row: self.read_rows([r]))
            read.pack(side="left")
            self.buttons.append(read)
            write = ttk.Button(actions, text="Write", width=5, command=lambda r=row: self.write_rows([r]))
            write.pack(side="left", padx=2)
            if writable:
                self.buttons.append(write)
            else:
                write.state(["disabled"])
            self.values[row].trace_add("write", lambda *_, r=row: self.value_changed(r))
        ttk.Label(self.window, text="Row addresses: decimal / hex. Bytes: hexadecimal. Bit order: b7 (MSB) → b0 (LSB). RO and unused rows cannot be written.",
                  padding=12).pack(fill="x")

    def value_changed(self, row):
        text = self.values[row].get()
        try:
            value = parse_byte(text)
        except ValueError:
            self.decimals[row].set("—")
            self.states[row].set("Invalid" if text else "Not read")
            return
        for bit, variable in enumerate(self.bits[row]):
            variable.set((value >> bit) & 1)
        self.decimals[row].set(str(value))
        self.states[row].set("Unchanged" if self.last_read.get(row) == value else "Changed")

    def toggle_bit(self, row, bit):
        if self.frozen or row not in self.device.writable:
            return
        try:
            current = parse_byte(self.values[row].get())
        except ValueError:
            self.bits[row][bit].set(0)
            self.notice.set("Read this register or enter a valid hex byte before toggling its bits.")
            return
        value = (current & ~(1 << bit)) | (self.bits[row][bit].get() << bit)
        self.values[row].set(f"{value:02X}")

    def freeze(self, frozen):
        self.frozen = frozen
        for widget in self.edit_controls + self.buttons:
            widget.state(["disabled"] if frozen else ["!disabled"])

    def run(self, label, worker):
        if self.panel.busy or self.panel.closing:
            self.notice.set("Wait for the current operation to finish.")
            return
        if self.panel.bus is not self.bus or self.bus.closed:
            self.notice.set("This connection was closed. Reopen All registers after connecting.")
            return
        self.freeze(True)
        self.notice.set(label + "…")
        def complete(result, error):
            if not self.window.winfo_exists():
                return
            self.freeze(False)
            if error:
                self.notice.set(f"Error: {error}. A batch may be partially applied; read back before retrying.")
                return
            for row, value in result.items():
                self.last_read[row] = value
                self.readbacks[row].set(f"0x{value:02X}")
                self.values[row].set(f"{value:02X}")
            self.notice.set(f"{label}: {len(result)} register(s) completed.")
        self.panel.submit(f"{self.device.name} {label}", worker, complete)

    def read_rows(self, rows):
        rows = tuple(rows)
        self.run("Read registers", lambda: {r: self.device.read_register(r) for r in rows})

    def read_all(self):
        self.read_rows(range(32))

    def write_rows(self, rows):
        try:
            values = {}
            for row in rows:
                if row not in self.device.writable:
                    raise ValueError(f"R{row} is read-only or unused")
                values[row] = parse_byte(self.values[row].get())
        except ValueError as exc:
            self.notice.set(f"R{row}: {exc}")
            return
        if not values:
            self.notice.set("No changed writable registers.")
            return
        def write():
            for row, value in values.items():
                self.device.write_register(row, value)
            return values
        self.run("Write and verify", write)

    def write_changed(self):
        # Invalid nonempty edits are included, so batch validation rejects all
        # writes before touching hardware instead of silently skipping mistakes.
        rows = [r for r in self.device.writable if self.values[r].get().strip()
                and self.states[r].get() != "Unchanged"]
        self.write_rows(rows)

    def discard(self):
        if self.frozen:
            return
        for row in range(32):
            value = self.last_read.get(row)
            self.values[row].set("" if value is None else f"{value:02X}")
        self.notice.set("Edits discarded. Last-read values restored; no writes sent.")
