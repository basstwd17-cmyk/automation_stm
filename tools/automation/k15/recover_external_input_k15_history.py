"""Export an already-captured RTM3004 K15 external-input history.

Use after a capture reached its requested segment count but the first History
read raised a transient RTM execution error.  This tool never arms, stops, or
reconfigures the scope; it only reads the retained C1/C2/C3 History records.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path


AUTOMATION_DIR = Path(__file__).resolve().parents[1]
if str(AUTOMATION_DIR) not in sys.path:
    sys.path.insert(0, str(AUTOMATION_DIR))

from common.rtm3004 import RTM3004
from k15.external_input_k15 import CHANNELS, HOST, write_results


def main() -> int:
    parser = argparse.ArgumentParser(description="Export retained K15 C1/C2/C3 history")
    parser.add_argument("--segments", type=int, required=True)
    args = parser.parse_args()
    if args.segments <= 0:
        parser.error("--segments must be positive")

    scope = RTM3004(HOST, timeout_s=20.0)
    try:
        print(f"Connected: {scope.identity()}")
        timestamps = scope.history_timestamps(CHANNELS[0])
        if len(timestamps) < args.segments:
            raise RuntimeError(
                f"Only {len(timestamps)} retained history records; "
                f"cannot export {args.segments}"
            )
        # Prove the oldest requested entry is accessible before creating the
        # output directory and spending time on the full export.
        scope.read_history_segment_channels(
            -args.segments + 1,
            "MAX",
            CHANNELS,
            verify_selection=False,
            check_errors=True,
        )
        output_dir = (
            AUTOMATION_DIR.parents[1] / "output"
            / "k15_external_input_recovered"
            / datetime.now().strftime("%Y%m%d_%H%M%S")
        )
        write_results(
            scope,
            args.segments,
            output_dir,
            history_ready=True,
            fast_history_read=True,
        )
        print(f"Saved: {output_dir}")
        return 0
    except Exception as error:
        print(f"RECOVERY FAILED: {error}")
        return 1
    finally:
        scope.close()


if __name__ == "__main__":
    raise SystemExit(main())
