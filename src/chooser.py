"""Humble Choice game selector (not supported on the new Humble site yet)."""

from __future__ import annotations

from typing import Any

from src.utils import print_warning


def humble_chooser_mode(
    humble_session, order_details: list[dict[str, Any]]
) -> None:
    """Humble Choice picking is not available on the new Humble site yet."""
    print_warning(
        "The Humble Choice chooser is not supported on the new Humble site yet. "
        "Pick your games in the browser at https://www.humblebundle.com/membership/home"
    )
