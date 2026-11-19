"""Humble Bundle key reveal helper.

Humble's old JSON API and password login no longer work; all Humble access now
goes through :class:`src.humble_browser.HumbleBrowser`. This module keeps the
``redeem_humble_key`` entry point used by the redeemer and exporter.
"""

from __future__ import annotations

from typing import Any


def redeem_humble_key(session, tpk: dict[str, Any]) -> str:
    """Reveal a key via the Humble browser session. Returns the key ("" on failure)."""
    return session.reveal_key(tpk) or ""
