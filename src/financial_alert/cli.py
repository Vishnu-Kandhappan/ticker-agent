"""Command-line interface."""

from __future__ import annotations

import argparse
import logging

from financial_alert.config import Settings
from financial_alert.telemetry import initialize_telemetry, shutdown_telemetry
from financial_alert.workflow import run


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate a portfolio news briefing.")
    parser.add_argument("--portfolio", help="Path to the portfolio JSON file.")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        settings = Settings.from_env(args.portfolio)
        initialize_telemetry()
        run(settings)
    except (RuntimeError, ValueError) as exc:
        logging.getLogger(__name__).error("Briefing workflow failed: %s", exc)
        return 1
    finally:
        shutdown_telemetry()
    return 0
