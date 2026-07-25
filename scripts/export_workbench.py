from __future__ import annotations

import argparse
import json
from pathlib import Path

from dispute_agent.services.workbench import WorkbenchService


def main() -> None:
    parser = argparse.ArgumentParser(description="Export a reviewer-facing case workbench projection.")
    parser.add_argument("case_id")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = WorkbenchService().get(args.case_id)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"已导出 {args.case_id} -> {args.output}")


if __name__ == "__main__":
    main()
