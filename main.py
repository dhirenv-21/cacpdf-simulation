"""Convenience launcher: `python main.py` opens the GUI (same as `python -m cacpdf gui`)."""
import sys

from cacpdf.__main__ import main

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:] or ["gui"]))
