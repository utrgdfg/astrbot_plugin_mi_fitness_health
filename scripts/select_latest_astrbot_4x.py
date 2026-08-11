"""Select the newest stable AstrBot v4 tag from GitHub Releases JSON."""

from __future__ import annotations

import json
import sys
from collections.abc import Iterable, Mapping


def select_latest_stable_v4(releases: Iterable[object]) -> str:
    """Return the first non-draft, non-prerelease v4 tag in API order."""
    for item in releases:
        if not isinstance(item, Mapping):
            continue
        tag = item.get("tag_name")
        if (
            isinstance(tag, str)
            and tag.startswith("v4.")
            and item.get("draft") is False
            and item.get("prerelease") is False
        ):
            return tag
    raise ValueError("AstrBot releases did not contain a stable v4.x tag")


def main() -> int:
    payload = json.load(sys.stdin)
    if not isinstance(payload, list):
        raise ValueError("GitHub releases response is not a list")
    print(select_latest_stable_v4(payload))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
