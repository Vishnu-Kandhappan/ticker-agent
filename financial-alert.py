"""Backward-compatible entry point for the financial alert application."""

import sys
from pathlib import Path

# Allow ``python financial-alert.py`` to work from a fresh checkout before
# installing the package in editable mode.
sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from financial_alert.cli import main


if __name__ == "__main__":
    raise SystemExit(main())
