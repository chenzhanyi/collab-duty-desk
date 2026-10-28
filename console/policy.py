"""Small shared policy used by the console and command-line helpers."""
import json
import os
from pathlib import Path


def max_revise_rounds(root: Path) -> int:
    value = 3
    try:
        value = json.loads((Path(root) / "协作" / "settings.json").read_text(encoding="utf-8")).get("max_revise_rounds", value)
    except (OSError, ValueError, AttributeError):
        pass
    value = os.environ.get("COLLAB_MAX_REVISE_ROUNDS", value)
    try:
        number = int(value)
        return number if 1 <= number <= 100 else 3
    except (ValueError, TypeError):
        return 3
