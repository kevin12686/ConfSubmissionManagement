#!/usr/bin/env python3
"""Update conferences and local compute prerequisites through one workflow."""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import deploy


def main(argv=None):
    return deploy.main(["update", *(sys.argv[1:] if argv is None else argv)])


if __name__ == "__main__":
    raise SystemExit(main())
