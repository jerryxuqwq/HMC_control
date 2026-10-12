"""Double-click or run: python hmc_gui.py [--simulate]."""
import sys
from hmc_control.__main__ import main

if __name__ == "__main__":
    raise SystemExit(main([*sys.argv[1:], "gui"]))
