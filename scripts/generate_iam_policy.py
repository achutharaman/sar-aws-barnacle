#!/usr/bin/env python3
"""Regenerate docs/iam-policy.json from the check registry.

Run after adding or changing a check. CI verifies the committed file matches.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aws_barnacle.iam import build_policy  # noqa: E402

OUTPUT = ROOT / "docs" / "iam-policy.json"


def main() -> int:
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(build_policy(), indent=2) + "\n", encoding="utf-8")
    print(f"wrote {OUTPUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
