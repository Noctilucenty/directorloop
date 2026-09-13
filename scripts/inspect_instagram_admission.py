"""Produce new offline admission diagnostics without opening private outcomes."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from directorloop.backtest.admission import inspect_admission, intake_markdown


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--intake-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = inspect_admission(args.intake_root)
    args.output.mkdir(parents=True, exist_ok=False)
    (args.output / "admission.json").write_text(json.dumps(report, indent=2) + "\n")
    (args.output / "NEEDED_ASSETS.md").write_text(intake_markdown(report))
    print(json.dumps({"output": str(args.output), "admitted_posts": report["admitted_posts"],
                      "selected_posts": report["selected_posts"], "outcome_values_opened": False, "provider_calls": 0}))


if __name__ == "__main__":
    main()
