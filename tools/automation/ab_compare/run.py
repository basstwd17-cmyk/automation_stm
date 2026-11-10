"""Command-line entry point for the C1-C2 statistics measurement."""

from __future__ import annotations

import sys
from pathlib import Path


AUTOMATION_DIR = Path(__file__).resolve().parents[1]
if str(AUTOMATION_DIR) not in sys.path:
    sys.path.insert(0, str(AUTOMATION_DIR))

from common.statistics_runner import main


if __name__ == "__main__":
    main()
