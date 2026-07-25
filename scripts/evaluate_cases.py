from __future__ import annotations

import argparse
import json
from pathlib import Path

from dispute_agent.services.evaluation import EvaluationService


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate persisted dispute-agent case artifacts.")
    parser.add_argument("--case-id", action="append", dest="case_ids", help="Only evaluate one or more case IDs.")
    parser.add_argument("--output", type=Path, help="Write the JSON report to this path.")
    args = parser.parse_args()
    report = EvaluationService().evaluate(case_ids=args.case_ids)
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)


if __name__ == "__main__":
    main()
